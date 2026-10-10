from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .. import overview, registry
from ..agents import canonical_agent
from ..presence import SETUP_COMMAND, PresenceClient
from .capabilities import AdapterRegistry, NativeRoots, SourceRequest
from .model import Identity, PreservationError
from .operations import StoredRoots


@dataclass(frozen=True)
class SelectedSession:
    identity: Identity
    lifecycle: str
    record: dict[str, Any]

    @property
    def thread(self) -> dict[str, Any]:
        result = self.record
        while result.get("thread"):
            result = result["thread"]
        return result

    @property
    def cwd(self) -> Path:
        value = self.thread.get("project") or self.record.get("project")
        if not value or not Path(value).is_absolute():
            raise PreservationError(
                f"{self.identity.agent}:{self.identity.native_id}: missing absolute workspace"
            )
        return Path(value).resolve()

    @property
    def source_ref(self) -> str | None:
        if self.lifecycle == "deleted":
            raise PreservationError("Source session is deleted")
        value = (
            self.record.get("archivePath") if self.lifecycle == "archived" else None
        ) or self.thread.get("file")
        return str(Path(value).resolve()) if value else None


def sessions(reg: dict[str, Any]) -> tuple[SelectedSession, ...]:
    return tuple(
        SelectedSession(Identity(*registry.identity(record)), lifecycle, record)
        for bucket, lifecycle in (
            ("threads", "active"),
            ("archived", "archived"),
            ("deleted", "deleted"),
        )
        for record in reg[bucket]
    )


def resolve(candidates: tuple[SelectedSession, ...], selector: str) -> SelectedSession:
    agent, separator, native_id = selector.partition(":")
    if separator:
        agent = canonical_agent(agent)
        candidates = tuple(item for item in candidates if item.identity.agent == agent)
    else:
        native_id = selector
    if not native_id:
        raise PreservationError("Session ID must not be empty")
    exact = tuple(item for item in candidates if item.identity.native_id == native_id)
    matches = exact or tuple(
        item for item in candidates if item.identity.native_id.startswith(native_id)
    )
    if len(matches) != 1:
        if matches:
            options = ", ".join(
                f"{item.identity.agent}:{item.identity.native_id}" for item in matches
            )
            raise PreservationError(
                f"Ambiguous session {selector}: {options}; use a qualified exact ID"
            )
        raise PreservationError(f"Session {selector} not found in registry; run rl session refresh")
    return matches[0]


def open_sessions(candidates: tuple[SelectedSession, ...], now: str) -> tuple[SelectedSession, ...]:
    return tuple(
        item
        for item in candidates
        if item.lifecycle == "active"
        and overview.effective_status(item.thread, now) in overview.LIVE_STATUSES
        and not overview.is_filtered(item.thread)
        and not overview.snooze_active(item.thread, now)
    )


def running_selectors(client: PresenceClient) -> tuple[str, ...]:
    result = client.sessions()
    if not result.available or result.problems:
        raise PreservationError(
            "Running selection unavailable or incomplete: "
            + "; ".join(result.problems)
            + f". Check {SETUP_COMMAND} and rl data presence status"
        )
    return tuple(
        sorted(
            {
                f"{canonical_agent(item.agent)}:{item.session_id}"
                for item in result.sessions
                if item.running
            }
        )
    )


def native_roots(
    adapters: AdapterRegistry, selected: SelectedSession, *, launch: bool = False
) -> NativeRoots:
    stored = selected.thread.get("nativeRoots")
    if stored:
        roots = replace(StoredRoots(**stored).native(), cwd=selected.cwd)
    elif launch:
        roots = adapters.roots(selected.identity.agent, selected.cwd)
    else:
        resolver = adapters.get(selected.identity.agent).roots
        if resolver is None:
            raise PreservationError("Native source location is unavailable")
        roots, _ = resolver.resolve_source(selected.cwd, selected.source_ref)
    if launch and not Path(source_ref(adapters, selected, roots)).is_relative_to(roots.store):
        raise PreservationError(
            "Recorded session file is outside the native launch store; check native root overrides"
        )
    return roots


def source_ref(adapters: AdapterRegistry, selected: SelectedSession, roots: NativeRoots) -> str:
    resolver = adapters.get(selected.identity.agent).roots
    if resolver is None:
        raise PreservationError("Native source location is unavailable")
    if not selected.thread.get("nativeRoots"):
        return resolver.resolve_source(selected.cwd, selected.source_ref)[1]
    return resolver.source_ref(roots, selected.source_ref)


def source_request(adapters: AdapterRegistry, selected: SelectedSession) -> SourceRequest:
    roots = native_roots(adapters, selected)
    return SourceRequest(selected.identity, roots, source_ref(adapters, selected, roots))
