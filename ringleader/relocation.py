from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from . import gitinfo, registry, scanner
from .agents import source_of
from .sources.base import IndexUpdate, WorkspaceRebindingSource, WorkspaceRelocatingSource
from .stateio import publish_lock


@dataclass(frozen=True)
class StorageMove:
    moved_to: str | None
    ok: bool
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Relocated:
    ok: bool
    where: str = ""
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


type _Storage = (
    tuple[Literal["rebind"], WorkspaceRebindingSource]
    | tuple[Literal["relocate"], WorkspaceRelocatingSource]
    | tuple[Literal["registry"], None]
)


def _storage(source: str) -> _Storage:
    active = next((s for s in scanner.active_sources() if s.name == source), None)
    if scanner.supports(active, "rebinds_workspace", WorkspaceRebindingSource):
        return "rebind", cast(WorkspaceRebindingSource, active)
    if scanner.supports(active, "relocates_workspace", WorkspaceRelocatingSource):
        return "relocate", cast(WorkspaceRelocatingSource, active)
    return "registry", None


def storage_follows(source: str) -> bool:
    return _storage(source)[0] != "registry"


def _rebind(source: WorkspaceRebindingSource, thread: dict[str, Any], new_path: str) -> StorageMove:
    try:
        outcome = source.rebind_workspace(thread["sessionId"], thread["file"], new_path)
    except (RuntimeError, OSError) as e:
        return StorageMove(None, False, errors=(str(e),))
    if outcome is IndexUpdate.NO_INDEX:
        # Refuse rather than pin: the index is usually missing only temporarily,
        # and a pinned thread is one `rl workspace retarget` can never find again.
        return StorageMove(None, False, errors=(f"{source.name} has no session index here",))
    if outcome is IndexUpdate.NO_ENTRY:
        note = f"no {source.name} index entry for {thread['file']} — it stays resumable by id"
        return StorageMove(None, True, warnings=(note,))
    return StorageMove(None, True)


def _relocate(
    source: WorkspaceRelocatingSource, thread: dict[str, Any], new_path: str
) -> StorageMove:
    # Already there: re-running after a partial failure is the recovery path, and
    # relocate_session refuses a destination that exists — including its own result.
    if source.storage_dir(thread["file"]) == source.storage_for(new_path):
        return StorageMove(None, True)
    try:
        return StorageMove(source.relocate_session(thread["file"], new_path), True)
    except (RuntimeError, OSError) as e:
        return StorageMove(None, False, errors=(str(e),))


def follow_storage(thread: dict[str, Any], new_path: str) -> StorageMove:
    source = source_of(thread)
    if not thread.get("file"):
        sid = thread["sessionId"]
        return StorageMove(
            None, True, notes=(f"registry only — no transcript recorded for {sid[:8]}",)
        )
    match _storage(source):
        case "rebind", rebinding:
            return _rebind(rebinding, thread, new_path)
        case "relocate", relocating:
            return _relocate(relocating, thread, new_path)
        case _:
            # the registry record is still worth re-pointing
            return StorageMove(
                None, True, notes=(f"registry only — {source} storage cannot follow a session",)
            )


def planned_storage(thread: dict[str, Any], new_path: str) -> str:
    source = source_of(thread)
    file = thread.get("file")
    if not file:
        return "registry record only — no transcript recorded"
    match _storage(source):
        case "rebind", _:
            return f"rebind {source}'s recorded cwd to {new_path}"
        case "relocate", relocating:
            if relocating.storage_dir(file) == relocating.storage_for(new_path):
                return f"{file} is already filed under {new_path}"
            return f"{file} → {relocating.storage_for(new_path) / Path(file).name}"
        case _:
            return "registry record only — its storage cannot follow a session"


def relocate(
    registry_path: str, session: str, new_path: str, *, registry_only: bool = False
) -> Relocated:
    # Resolved before the lock and before any storage move: it shells out to git, and a
    # failure there must not land between a moved transcript and the record naming it.
    git = gitinfo.project_record(new_path)
    where = f"{git['label']} ({new_path})" if git else new_path
    with publish_lock():
        reg = registry.load_registry(registry_path)
        thread = registry.find_session(reg, session)
        if thread is None:
            return Relocated(False, errors=(f"unknown session {session}",))
        was = thread.get("project") or ""
        move = StorageMove(None, True) if registry_only else follow_storage(thread, new_path)
        if not move.ok:
            return Relocated(False, notes=move.notes, errors=move.errors)
        # pinned, or the next scan reverts it from the transcript's baked-in cwd
        registry.pin_thread_location(thread, new_path, git)
        registry.remap_cached_git_root(thread, was, new_path)
        if move.moved_to is not None:
            thread["file"] = move.moved_to
        registry.commit_registry(registry_path, reg)
    return Relocated(True, where, notes=move.notes, warnings=move.warnings)
