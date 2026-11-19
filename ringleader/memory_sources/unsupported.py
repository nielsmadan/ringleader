from __future__ import annotations

from dataclasses import dataclass

from ..memory_types import MemoryScan, MemorySourceStatus

REASONS = {
    "cursor": "No supported local auto-memory interface verified for Cursor CLI 2026.11.13-eacade1; IDE/cloud memories are outside CLI scope.",
    "droid": "Factory documents manual memory files; scheduled automation state is separate.",
    "opencode": "No native auto-memory store verified in OpenCode core 2.3.0; plugins are outside scope.",
    "pi": "No native auto-memory store verified in Pi core 1.5.2; extensions are outside scope.",
}


@dataclass(frozen=True)
class UnsupportedMemorySource:
    name: str
    installed: bool

    def scan(self) -> MemoryScan:
        status = MemorySourceStatus(
            self.name, "not-supported" if self.installed else "not-installed", REASONS[self.name]
        )
        return MemoryScan((), (status,), ())
