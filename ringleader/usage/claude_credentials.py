from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from . import claude_keychain
from .provider_environment import CLAUDE_TOKEN_VAR as TOKEN_VAR
from .remote_http import MAX_HEADER_VALUE, printable_ascii
from .remote_types import (
    Credential,
    Diagnostic,
    InvalidPayload,
    fingerprint,
    number,
    object_dict,
    optional_string,
)

PROFILE_SCOPE = "user:profile"


class CredentialError(Exception):
    def __init__(
        self, code: str, message: str, *, status: Literal["not_configured", "error"] = "error"
    ) -> None:
        self.diagnostic = Diagnostic(code, message)
        self.status = status
        super().__init__(message)


def _invalid_credentials() -> CredentialError:
    return CredentialError(
        "credentials_invalid", "Claude credentials are invalid. Renew the sign-in in Claude Code."
    )


def _access_token(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid_credentials()
    token = value.strip()
    if (
        not token
        or len(token) > MAX_HEADER_VALUE - len("Bearer ")
        or not printable_ascii(token)
        or any(char.isspace() for char in token)
    ):
        raise _invalid_credentials()
    return token


def credential_path(environment: Mapping[str, str], *, home: Path, cwd: Path) -> Path:
    secure, config = claude_keychain.PROFILE_OVERRIDES
    default = str(home / ".claude")
    root = environment.get(secure, environment.get(config, default)) or default
    return Path(os.path.normpath(cwd / root)) / ".credentials.json"


def parse_credentials(
    contents: bytes, *, profile: str, now: datetime, storage: str = "file"
) -> tuple[Credential, str | None]:
    try:
        raw = object_dict(json.loads(contents))
        if raw.get("claudeAiOauth") is None:
            raise CredentialError(
                "credentials_missing",
                "No Claude subscription credentials in this profile. Sign in to Claude Code.",
                status="not_configured",
            )
        oauth = object_dict(raw["claudeAiOauth"])
        token = _access_token(oauth.get("accessToken"))
        expiry = oauth.get("expiresAt")
        expires_at = (
            None if expiry is None else datetime.fromtimestamp(number(expiry) / 1000, tz=UTC)
        )
        scopes = oauth.get("scopes")
        if scopes is not None and (
            not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes)
        ):
            raise InvalidPayload
        if expires_at is None or expires_at <= now:
            raise CredentialError(
                "credentials_expired", "Claude credentials need renewal. Sign in to Claude Code."
            )
        if not scopes or PROFILE_SCOPE not in scopes:
            raise CredentialError(
                "scope_denied", "Claude subscription usage requires the user:profile OAuth scope."
            )
        subscription = optional_string(oauth.get("subscriptionType"), secrets=(token,))
        tier = optional_string(oauth.get("rateLimitTier"), secrets=(token,))
        return Credential(
            token,
            fingerprint("claude", "agent", storage, profile, token),
            expires_at,
            frozenset(scopes),
        ), subscription or tier
    except (ValueError, TypeError, OverflowError, OSError, RecursionError):
        raise _invalid_credentials() from None


def _read_file(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise _invalid_credentials()
            contents = handle.read(claude_keychain.MAX_CREDENTIAL_BYTES + 1)
    except FileNotFoundError:
        raise CredentialError(
            "credentials_missing",
            "No Claude credentials file in this profile. Sign in or use usage.claude_auth env.",
            status="not_configured",
        ) from None
    except (OSError, ValueError):
        raise CredentialError(
            "credentials_read", "Could not read the selected Claude credentials file."
        ) from None
    if len(contents) > claude_keychain.MAX_CREDENTIAL_BYTES:
        raise _invalid_credentials()
    return contents


def _read_keychain() -> bytes:
    try:
        return claude_keychain.read_credentials()
    except claude_keychain.KeychainError as exc:
        if exc.code == "keychain_missing":
            raise CredentialError(
                exc.code,
                "No Claude subscription credentials in Keychain. Sign in or use usage.claude_auth env.",
                status="not_configured",
            ) from None
        message = {
            "keychain_timeout": "Claude Keychain lookup exceeded its deadline.",
            "keychain_ambiguous": "Multiple Claude credentials matched in Keychain.",
        }.get(exc.code, "Claude Keychain credentials are unavailable without an access prompt.")
        raise CredentialError(
            exc.code, f"{message} Use a profile credentials file or usage.claude_auth env."
        ) from None


def resolve_credential(
    mode: str, environment: Mapping[str, str], *, platform: str, home: Path, now: datetime
) -> tuple[Credential, str | None]:
    if mode == "env":
        raw = environment.get(TOKEN_VAR, "")
        if not raw.strip():
            raise CredentialError(
                "not_configured",
                f"Set {TOKEN_VAR} with the user:profile OAuth scope or enable Claude agent auth.",
                status="not_configured",
            )
        token = _access_token(raw)
        scope_key = fingerprint(
            "claude", "env", str(credential_path(environment, home=home, cwd=Path.cwd())), token
        )
        return Credential(token, scope_key, None, None), None
    path = credential_path(environment, home=home, cwd=Path.cwd())
    if claude_keychain.native_profile_allowed(environment, platform):
        return parse_credentials(_read_keychain(), profile=str(path), now=now, storage="keychain")
    return parse_credentials(_read_file(path), profile=str(path), now=now)
