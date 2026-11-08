from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from . import registry, resume, scanner
from .agents import source_of
from .codes import OutcomeCode
from .presence import SETUP_COMMAND, PresenceUnavailable

type Identity = tuple[str, str]
type PresenceProbe = Callable[[], frozenset[Identity] | None]
type UnknownPresence = Literal["refuse", "allow"]

_FINGERPRINT_FIELDS = 5
CHANGED = "changed since you looked"
NO_STAMP = f"{CHANGED}: no fingerprint, rescan"
GONE = "no longer in the registry"
TRANSCRIPT_MISSING = "transcript is missing"


@dataclass(frozen=True)
class Refusal:
    code: OutcomeCode
    text: str


@dataclass(frozen=True)
class Seen:
    identity: Identity
    label: str
    mtime: str
    fingerprint: str | None


def seen(thread: dict[str, Any]) -> Seen:
    return Seen(
        registry.identity(thread),
        resume.label(thread),
        thread.get("mtime") or "",
        thread.get("fileFingerprint"),
    )


def probe(presence: Callable[[], frozenset[Identity]]) -> PresenceProbe:
    def checked() -> frozenset[Identity] | None:
        try:
            return presence()
        except (PresenceUnavailable, OSError):
            return None

    return checked


def content_stamp(fingerprint: str | None) -> tuple[int, int] | None:
    parts = fingerprint.split(":") if isinstance(fingerprint, str) else []
    if len(parts) != _FINGERPRINT_FIELDS or not all(part.isdigit() for part in parts):
        return None
    return int(parts[2]), int(parts[3])


def _disk_refusal(thread: dict[str, Any], stamp: tuple[int, int]) -> Refusal | None:
    try:
        st = scanner.filesystem_path(thread, "revalidate").stat()
    except FileNotFoundError:
        return Refusal("transcript_missing", TRANSCRIPT_MISSING)
    except (RuntimeError, OSError) as error:
        return Refusal("other", str(error))
    return None if (st.st_size, st.st_mtime_ns) == stamp else Refusal("changed", CHANGED)


def changed_refusal(
    thread: dict[str, Any] | None, expected: Seen, *, require_stamp: bool = False
) -> Refusal | None:
    if thread is None:
        return Refusal("gone", GONE)
    if (thread.get("mtime") or "") != expected.mtime:
        return Refusal("changed", CHANGED)
    stamp = content_stamp(expected.fingerprint)
    if stamp is None:
        return Refusal("no_fingerprint", NO_STAMP) if require_stamp else None
    return _disk_refusal(thread, stamp)


def changed_reason(
    thread: dict[str, Any] | None, expected: Seen, *, require_stamp: bool = False
) -> str | None:
    refusal = changed_refusal(thread, expected, require_stamp=require_stamp)
    return None if refusal is None else refusal.text


def running_refusal(
    thread: dict[str, Any], live: frozenset[Identity] | None, unknown: UnknownPresence
) -> Refusal | None:
    label = resume.label(thread)
    if live is None:
        if unknown == "refuse":
            return Refusal(
                "presence_unknown",
                f"can't tell whether {label} is running — install presence with {SETUP_COMMAND}",
            )
        return None
    key = (resume.agent_name(source_of(thread)), thread.get("sessionId") or "")
    if key in live:
        return Refusal(
            "running",
            f"{label} is running — finish it from inside with the ringleader-finish skill",
        )
    return None


def running_reason(
    thread: dict[str, Any], live: frozenset[Identity] | None, unknown: UnknownPresence
) -> str | None:
    refusal = running_refusal(thread, live, unknown)
    return None if refusal is None else refusal.text


def quiet_reason(thread: dict[str, Any], now_epoch: float, grace_seconds: int) -> str | None:
    try:
        written = scanner.filesystem_path(thread, "sweep").stat().st_mtime
    except (RuntimeError, OSError):
        return None
    return "still being written" if written > now_epoch - grace_seconds else None
