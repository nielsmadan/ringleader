from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from . import registry, resume, scanner
from .agents import source_of
from .presence import SETUP_COMMAND, PresenceUnavailable

type Identity = tuple[str, str]
type PresenceProbe = Callable[[], frozenset[Identity] | None]
type UnknownPresence = Literal["refuse", "allow"]

_FINGERPRINT_FIELDS = 5
NO_STAMP = "changed since you looked: no fingerprint, rescan"


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


def _disk_reason(thread: dict[str, Any], stamp: tuple[int, int]) -> str | None:
    try:
        st = scanner.filesystem_path(thread, "revalidate").stat()
    except RuntimeError as error:
        return str(error)
    except FileNotFoundError:
        return "transcript is missing"
    except OSError as error:
        return str(error)
    return None if (st.st_size, st.st_mtime_ns) == stamp else "changed since you looked"


def changed_reason(
    thread: dict[str, Any] | None, expected: Seen, *, require_stamp: bool = False
) -> str | None:
    if thread is None:
        return "no longer in the registry"
    if (thread.get("mtime") or "") != expected.mtime:
        return "changed since you looked"
    stamp = content_stamp(expected.fingerprint)
    if stamp is None:
        return NO_STAMP if require_stamp else None
    return _disk_reason(thread, stamp)


def running_reason(
    thread: dict[str, Any], live: frozenset[Identity] | None, unknown: UnknownPresence
) -> str | None:
    label = resume.label(thread)
    if live is None:
        if unknown == "refuse":
            return f"can't tell whether {label} is running — install presence with {SETUP_COMMAND}"
        return None
    key = (resume.agent_name(source_of(thread)), thread.get("sessionId") or "")
    if key in live:
        return f"{label} is running — finish it from inside with the ringleader-finish skill"
    return None


def quiet_reason(thread: dict[str, Any], now_epoch: float, grace_seconds: int) -> str | None:
    try:
        written = scanner.filesystem_path(thread, "sweep").stat().st_mtime
    except (RuntimeError, OSError):
        return None
    return "still being written" if written > now_epoch - grace_seconds else None
