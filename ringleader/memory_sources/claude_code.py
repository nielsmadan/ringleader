from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any, ClassVar, Literal

from ..memory import INDEX_NAME, detect_obsolete, parse_frontmatter, repo_doc_path
from ..memory_types import (
    MemoryObservation,
    MemoryRecord,
    MemoryRef,
    MemoryScan,
    MemoryStoreStatus,
    NativeMemoryTransaction,
)
from ..paths import AGENT_CONFIG_DIRS, AGENT_INSTRUCTION_DIRS
from ..slug import slugify
from ..sources.base import iso_mtime
from . import claude_mutation
from .base import NativePaths, check_config_depth, file_identity, harness_status, memory_id
from .claude_index import inline_content

_EXCLUDED = {"agents.md", "claude.md", "claude.local.md", "agents.override.md", "skill.md"}
_PROJECT_ENTRY_LIMIT = 256
_TRANSCRIPT_LIMIT = 64
_TRANSCRIPT_PREFIX = 256 * 1024


def _settings(path: Path, paths: NativePaths, *, missing_ok: bool = True) -> dict[str, Any]:
    try:
        data = json.loads(paths.read(path)[0])
    except RecursionError as exc:
        raise ValueError(f"Excessively nested Claude settings: {path}") from exc
    except FileNotFoundError:
        if not missing_ok:
            raise
        return {}
    if not isinstance(data, dict):
        raise TypeError(f"expected settings object: {path}")
    check_config_depth(data, path)
    return data


def _same_native_path(path: Path, native: Path, paths: NativePaths) -> bool:
    if path == native:
        return True
    if paths.boundary and not native.is_relative_to(paths.boundary):
        return False
    try:
        return os.path.samestat(path.lstat(), native.lstat())
    except FileNotFoundError:
        return False


def is_instruction_area(path: Path, config: Path | None, paths: NativePaths) -> bool:
    for directory in (path, *path.parents):
        name = directory.name.casefold()
        if name != ".git" and name not in AGENT_INSTRUCTION_DIRS:
            continue
        if not _same_native_path(directory, directory.with_name(name), paths):
            continue
        parent = directory.parent
        parent_name = parent.name.casefold()
        if (
            name == ".git"
            or (config is not None and _same_native_path(parent, config, paths))
            or (
                parent_name in AGENT_CONFIG_DIRS
                and _same_native_path(parent, parent.with_name(parent_name), paths)
            )
        ):
            return True
    return False


@dataclass(frozen=True)
class ClaudeRoots:
    config: Path
    projects: Path
    home: Path
    known_projects: tuple[Path, ...] = ()
    trusted_projects: tuple[Path, ...] = ()
    trust_file: Path | None = None
    managed_settings: tuple[Path, ...] = ()
    explicit_settings: tuple[Path, ...] = ()
    named_project_dir: str | None = None
    prior_stores: tuple[Path, ...] = ()

    @property
    def trust_status_id(self) -> str:
        return f"project-trust:{self.config}"


@dataclass(frozen=True)
class _Store:
    path: Path
    project: str | None
    scope: Literal["global", "project"] = "project"


@dataclass
class _Discovery:
    stores: dict[Path, _Store] = field(default_factory=dict)
    statuses: list[MemoryStoreStatus] = field(default_factory=list)
    directories: dict[tuple[int, int], Path] = field(default_factory=dict)
    protected: set[Path] = field(default_factory=set)

    def add(self, store: _Store) -> None:
        try:
            info = store.path.lstat()
        except OSError:
            pass
        else:
            canonical = self.directories.setdefault((info.st_dev, info.st_ino), store.path)
            store = _Store(canonical, store.project, store.scope)
        old = self.stores.get(store.path)
        if old is not None and old != store:
            store = _Store(store.path, None)
        self.stores[store.path] = store

    def status(
        self,
        store_id: Path | str,
        state: Literal["ready", "absent", "unreadable", "unsupported"],
        reason: str | None = None,
    ) -> None:
        self.statuses.append(
            MemoryStoreStatus(
                "claude-code",
                str(store_id),
                state,
                "complete" if state == "ready" else "unavailable",
                None,
                reason,
            )
        )


@dataclass(frozen=True)
class ClaudeMemorySource:
    name: ClassVar[str] = "claude-code"
    roots: ClaudeRoots
    installed: bool = True
    paths: NativePaths = field(default_factory=NativePaths)

    def _children(self, path: Path, found: _Discovery) -> list[Path]:
        try:
            self.paths.check(path)
            result = sorted(
                child for child in path.iterdir() if not child.is_symlink() and child.is_dir()
            )
        except FileNotFoundError:
            found.status(path, "absent")
            return []
        except (OSError, ValueError, TypeError) as exc:
            found.status(path, "unreadable", str(exc))
            return []
        found.status(path, "ready")
        return result

    def _trusted(self, found: _Discovery) -> set[Path]:
        trusted = set(self.roots.trusted_projects)
        custom_config = self.roots.config != self.roots.home / ".claude"
        status_id = self.roots.trust_status_id
        guidance = (
            "Claude project settings require native trust evidence. "
            "Specify memory.claude_trust_file for this custom CLAUDE_CONFIG_DIR."
        )
        if self.roots.trust_file:
            try:
                projects = _settings(
                    self.roots.trust_file, self.paths, missing_ok=not custom_config
                ).get("projects", {})
                if not isinstance(projects, dict):
                    raise TypeError("Invalid Claude project trust metadata")
                trusted.update(
                    Path(key)
                    for key, info in projects.items()
                    if isinstance(info, dict) and info.get("hasTrustDialogAccepted") is True
                )
            except (OSError, ValueError, TypeError) as exc:
                found.status(
                    status_id if custom_config else self.roots.trust_file,
                    "unreadable",
                    f"{exc}. {guidance}" if custom_config else str(exc),
                )
            else:
                if custom_config:
                    found.status(status_id, "ready")
        elif custom_config:
            if set(self.roots.known_projects) - trusted:
                found.status(status_id, "unsupported", guidance)
            else:
                found.status(status_id, "ready")
        return trusted

    def _project_dirs(self, project: Path) -> tuple[Path, Path]:
        for parent in (project, *project.parents):
            if self.paths.boundary is not None and not parent.is_relative_to(self.paths.boundary):
                break
            marker = parent / ".git"
            self.paths.check(marker)
            if marker.is_dir():
                return parent, parent
            if marker.is_file():
                prefix, sep, value = self.paths.read(marker)[0].decode().strip().partition(": ")
                if prefix == "gitdir" and sep:
                    gitdir = parent / value
                    self.paths.check(gitdir)
                    try:
                        common = self.paths.read(gitdir / "commondir")[0].decode().strip()
                    except FileNotFoundError:
                        return parent, parent
                    main = Path(os.path.abspath(gitdir / common)).parent
                    self.paths.check(main)
                    return parent, main
        return project, project

    def _configured(
        self, settings: Iterable[Path], *, project: Path | None, found: _Discovery
    ) -> None:
        settings_path: Path | None = None
        try:
            data: dict[str, Any] = {}
            for settings_path in settings:
                data.update(_settings(settings_path, self.paths))
            raw = data.get("autoMemoryDirectory")
            if raw is None:
                return
            if not isinstance(raw, str) or not (raw.startswith("~/") or Path(raw).is_absolute()):
                raise ValueError("autoMemoryDirectory must be absolute or start with ~/")
            path = self.roots.home / raw[2:] if raw.startswith("~/") else Path(raw)
            self.paths.check(path)
            if (
                any(_same_native_path(path, protected, self.paths) for protected in found.protected)
                or (
                    path.name.casefold() in AGENT_CONFIG_DIRS
                    and _same_native_path(path, path.with_name(path.name.casefold()), self.paths)
                )
                or is_instruction_area(path, self.roots.config, self.paths)
            ):
                raise ValueError(f"unsafe autoMemoryDirectory: {path}")
            found.add(_Store(path, str(project) if project else None))
        except (ValueError, TypeError, OSError) as exc:
            if settings_path is not None:
                found.status(settings_path, "unreadable", str(exc))

    def _transcript_cwd(self, directory: Path) -> str | None:
        candidates: set[str] = set()
        try:
            self.paths.check(directory)
            if not (directory / "memory").is_dir(follow_symlinks=False):
                return None
            with os.scandir(directory) as entries:
                names = [entry.name for entry in islice(entries, _PROJECT_ENTRY_LIMIT + 1)]
            transcripts = [directory / name for name in names if name.endswith(".jsonl")]
            if len(names) > _PROJECT_ENTRY_LIMIT or len(transcripts) > _TRANSCRIPT_LIMIT:
                return None
            for transcript in transcripts:
                with self.paths.open_file(transcript) as stream:
                    lines = stream.read(_TRANSCRIPT_PREFIX).splitlines()
                for line in lines:
                    try:
                        event = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    if not isinstance(event, dict) or not isinstance(event.get("cwd"), str):
                        continue
                    cwd = event["cwd"]
                    if not cwd:
                        continue
                    project = Path(cwd)
                    self.paths.check(project)
                    if (
                        "\x00" in cwd
                        or len(cwd.splitlines()) != 1
                        or str(project) != cwd
                        or slugify(project) != directory.name
                    ):
                        return None
                    candidates.add(cwd)
                    if len(candidates) > 1:
                        return None
                    break
        except (OSError, ValueError):
            return None
        return next(iter(candidates), None)

    def _discover(self, *, transcript_provenance: bool = False) -> _Discovery:
        found = _Discovery()
        roots = self.roots
        trusted = self._trusted(found)
        known = set(roots.known_projects) | trusted
        found.protected.update(
            (roots.config, roots.projects, roots.home, *roots.home.parents, *known)
        )
        slugs: dict[str, set[str]] = {}
        admitted_trusted = []
        for project in sorted(known):
            if self.paths.boundary is not None and not project.is_relative_to(self.paths.boundary):
                continue
            try:
                self.paths.check(project)
                checkout, main = self._project_dirs(project)
            except (ValueError, TypeError, OSError) as exc:
                found.status(project, "unreadable", str(exc))
                continue
            if project in trusted:
                admitted_trusted.append(project)
            found.protected.update((checkout, main))
            for root in (project, checkout, main):
                slugs.setdefault(slugify(root), set()).add(str(main))
            if roots.named_project_dir:
                slugs.setdefault(roots.named_project_dir, set()).add(str(main))
            for directory in ("agent-memory", "agent-memory-local"):
                for child in self._children(checkout / ".claude" / directory, found):
                    found.add(_Store(child, str(checkout)))
        for project in admitted_trusted:
            self._project_settings(project, found)
        for child in self._children(roots.projects, found):
            memory = child / "memory"
            candidates = slugs.get(child.name, set())
            project_name = next(iter(candidates)) if len(candidates) == 1 else None
            if transcript_provenance and not candidates:
                project_name = self._transcript_cwd(child)
            found.add(_Store(memory, project_name))
        for child in self._children(roots.config / "agent-memory", found):
            found.add(_Store(child, None, "global"))
        enumerated = {
            Path(status.store_id)
            for status in found.statuses
            if status.state in {"ready", "absent"}
        }
        for prior in roots.prior_stores:
            if prior not in found.stores and (
                (
                    prior.name == "memory"
                    and prior.parent.parent == roots.projects
                    and roots.projects in enumerated
                )
                or (
                    prior.parent in enumerated
                    and prior.parent.name in {"agent-memory", "agent-memory-local"}
                )
            ):
                found.add(_Store(prior, None))
        self._configured(
            (roots.config / "settings.json", *roots.explicit_settings, *roots.managed_settings),
            project=None,
            found=found,
        )
        return found

    def _project_settings(self, project: Path, found: _Discovery) -> None:
        files = (project / ".claude" / "settings.json", project / ".claude" / "settings.local.json")
        try:
            project_data = [_settings(path, self.paths) for path in files]
            if not any("autoMemoryDirectory" in data for data in project_data):
                return
            for path in (
                self.roots.config / "settings.json",
                *files,
                *self.roots.explicit_settings,
                *self.roots.managed_settings,
            ):
                perms = _settings(path, self.paths).get("permissions")
                if (
                    isinstance(perms, dict)
                    and perms.get("blockReadsOutsideWorkingDirectories") is True
                ):
                    return
        except (ValueError, TypeError, OSError) as exc:
            found.status(project / ".claude", "unreadable", str(exc))
            return
        self._configured(
            (*files, *self.roots.explicit_settings, *self.roots.managed_settings),
            project=project,
            found=found,
        )

    def _advisory_context(self, store: _Store) -> tuple[bool, str | None]:
        if store.project is None:
            return False, None
        project = Path(store.project)
        try:
            self.paths.check(project)
            project.stat()
            doc = repo_doc_path(store.project)
        except FileNotFoundError:
            return True, None
        except (OSError, ValueError):
            return False, None
        if doc is None:
            return False, None
        try:
            body, _ = self.paths.read(doc)
        except (OSError, ValueError):
            return False, None
        return False, body.decode("utf-8", errors="ignore")

    def _note(
        self, store: _Store, path: Path, *, repo_gone: bool, doc: str | None
    ) -> MemoryRecord | None:
        raw, info = self.paths.read(path)
        text = raw.decode("utf-8-sig")
        is_index = _same_native_path(path, store.path / INDEX_NAME, self.paths)
        inline, reason = None, None
        if is_index:
            path = store.path / INDEX_NAME
            inline, reason = inline_content(text)
            if not inline:
                return None
        meta, content = parse_frontmatter(text.replace("\r\n", "\n"))
        if inline is not None:
            content = inline.replace("\r\n", "\n")
        legacy = meta.get("metadata")
        kind = meta.get("type") or (legacy.get("type") if isinstance(legacy, dict) else None)
        store_id = str(store.path)
        ref = MemoryRef(
            self.name,
            memory_id(self.name, store_id, path.name),
            store_id,
            str(path),
            hashlib.sha256(raw).hexdigest(),
            file_identity(info),
        )
        name = meta.get("name")
        description = meta.get("description")
        return MemoryRecord(
            ref,
            name if isinstance(name, str) else ("Inline memory" if is_index else path.stem),
            description if isinstance(description, str) else None,
            store.project,
            store.scope,
            iso_mtime(info),
            kind if isinstance(kind, str) else None,
            reason,
            tuple(detect_obsolete(content, repo_gone=repo_gone, repo_doc_text=doc)),
            is_index,
            content,
        )

    def _store(self, store: _Store) -> tuple[list[MemoryObservation], MemoryStoreStatus]:
        errors: list[str] = []
        records: dict[str, MemoryObservation] = {}
        root = store.path
        try:
            self.paths.check(root)
            if is_instruction_area(root, self.roots.config, self.paths):
                raise ValueError(f"unsafe Claude memory store: {root}")
            context = None
            for path in sorted(root.iterdir()):
                if (
                    (
                        path.suffix != ".md"
                        and not _same_native_path(path, root / INDEX_NAME, self.paths)
                    )
                    or path.name.casefold() in _EXCLUDED
                    or path.name.startswith(".")
                ):
                    continue
                try:
                    if not stat.S_ISREG(path.lstat().st_mode):
                        continue
                    if context is None:
                        context = self._advisory_context(store)
                    record = self._note(store, path, repo_gone=context[0], doc=context[1])
                    if record:
                        records[record.ref.memory_id] = record.observation()
                except (OSError, ValueError, TypeError) as exc:
                    errors.append(f"{path.name}: {exc}")
        except FileNotFoundError:
            return [], MemoryStoreStatus(self.name, str(root), "absent", "complete", None, None)
        except (ValueError, TypeError, OSError) as exc:
            return [], MemoryStoreStatus(
                self.name, str(root), "unreadable", "unavailable", None, str(exc)
            )
        return list(records.values()), MemoryStoreStatus(
            self.name,
            str(root),
            "unreadable" if errors else "ready",
            "partial" if errors else "complete",
            "markdown",
            "; ".join(errors) or None,
        )

    def selected_store(self, ref: MemoryRef) -> _Store:
        path, root = Path(ref.source_ref), Path(ref.store_id)
        self.paths.check(path)
        store = self._discover().stores.get(root)
        if (
            store is None
            or ref.source != self.name
            or str(path) != ref.source_ref
            or path.parent != root
            or path.name.startswith(".")
            or path.suffix != ".md"
            or path.name.casefold() in _EXCLUDED
            or ref.memory_id != memory_id(self.name, str(root), path.name)
            or is_instruction_area(root, self.roots.config, self.paths)
        ):
            raise ValueError("Unrecognized Claude memory target")
        if path.name != INDEX_NAME and _same_native_path(path, root / INDEX_NAME, self.paths):
            raise ValueError(
                "Selected topic aliases the Claude index. Refresh and review the index."
            )
        return store

    def read_selected(self, ref: MemoryRef) -> MemoryRecord:
        store = self.selected_store(ref)
        try:
            record = self._note(store, Path(ref.source_ref), repo_gone=False, doc=None)
        except FileNotFoundError:
            record = None
        if record is None or record.ref != ref:
            raise ValueError(
                "Native memory disappeared or changed since review. Run `rl mem refresh`."
            )
        return record

    def verify_workspace(self, ref: MemoryRef) -> None:
        self.selected_store(ref)

    @property
    def hook_path(self) -> Path:
        return self.roots.config / "settings.json"

    def protected_locations(self, ref: MemoryRef | None = None) -> tuple[Path, ...]:
        return (
            *self._discover().stores,
            *((Path(ref.source_ref),) if ref and ref.source == self.name else ()),
        )

    def instruction_config_roots(self) -> tuple[Path, ...]:
        return (
            self.roots.config,
            self.roots.home / ".agents",
            *(path.parent for path in self.roots.managed_settings),
        )

    def session_may_use_store(self, ref: MemoryRef, project: str | None, cwd: str | None) -> bool:
        if project and cwd:
            store = Path(ref.store_id)
            if store.parent.parent == self.roots.projects or store.parent.name in {
                "agent-memory",
                "agent-memory-local",
            }:
                try:
                    directory = Path(cwd)
                    owner = self._discover().stores.get(store)
                    if owner is not None and owner.project == project and directory.is_dir():
                        return (
                            directory.is_relative_to(project)
                            or self._project_dirs(directory)[1]
                            == self._project_dirs(Path(project))[1]
                        )
                except (OSError, ValueError):
                    pass
        return True

    def mutation(self, ref: MemoryRef) -> AbstractContextManager[NativeMemoryTransaction]:
        return claude_mutation.mutation(self, ref)

    def cleanup_status(self, receipt: bytes) -> Literal["unknown"]:
        return "unknown"

    def scan(self) -> MemoryScan:
        found = self._discover(transcript_provenance=True)
        inventoried = all(status.state in {"ready", "absent"} for status in found.statuses)
        records = []
        for store in found.stores.values():
            entries, status = self._store(store)
            records.extend(entries)
            found.statuses.append(status)
        statuses = {status.store_id: status for status in found.statuses}
        stores = [status for key, status in statuses.items() if key != self.roots.trust_status_id]
        return MemoryScan(
            tuple(records),
            (harness_status(self.name, self.installed, stores),),
            tuple(statuses.values()),
            inventoried_sources=frozenset({self.name}) if inventoried else frozenset(),
        )
