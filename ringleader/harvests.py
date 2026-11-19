from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import publish_state, registry
from .gitinfo import normalize_remote, public_routing
from .memory import parse_frontmatter
from .memory_types import HarvestOrigin, MemoryOrigin, SessionOrigin, origin_fields, parse_origin
from .paths import default_registry, state_dir
from .stateio import atomic_write_bytes, publish_lock

if TYPE_CHECKING:
    from .memory_operations import Operation


_FRONTMATTER_UNSAFE = frozenset(
    ("\r", "\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\x00")
)


def harvests_dir() -> Path:
    return state_dir() / "harvests"


@dataclass(frozen=True)
class Harvest:
    path: Path
    harvest_id: str
    digest: str
    origin: HarvestOrigin
    name: str
    folder_path: str | None
    repo_url: str | None
    clone_url: str | None
    harvested_at: str | None
    content: str
    registry_owner: str | None = None
    legacy_owner: bool = False

    def _matches_legacy_owner(self, owner: str) -> bool:
        return self.registry_owner is not None and any(
            registry.owner_matches(candidate, self.registry_owner)
            for candidate in (owner, owner.rstrip())
        )

    def lifecycle_owner(
        self,
        operation: Operation | None,
        *,
        approved_token: str | None = None,
        checkpoint_owner: str | None = None,
    ) -> str | None:
        if operation is None:
            if self.legacy_owner:
                if checkpoint_owner and self._matches_legacy_owner(checkpoint_owner):
                    return checkpoint_owner
                raise ValueError("Legacy registry owner has no matching lifecycle approval")
            return self.registry_owner
        if self.origin != operation.origin:
            raise ValueError("Memory harvest differs from its lifecycle origin")
        if self.legacy_owner and (
            (self.body_digest != operation.approval_hash and approved_token != self.token)
            or not self._matches_legacy_owner(operation.registry_path)
        ):
            raise ValueError(
                "Legacy memory harvest has conflicting lifecycle ownership or approval"
            )
        if (
            not self.legacy_owner
            and self.registry_owner
            and not registry.owner_matches(self.registry_owner, operation.registry_path)
        ):
            raise ValueError("Memory harvest has conflicting registry ownership")
        return operation.registry_path

    @property
    def session_id(self) -> str | None:
        return self.origin.session_id if isinstance(self.origin, SessionOrigin) else None

    @property
    def origin_id(self) -> str:
        return (
            self.origin.session_id
            if isinstance(self.origin, SessionOrigin)
            else self.origin.memory_id
        )

    def destination_fields(self, *, include_clone: bool = True) -> dict[str, str | None]:
        if isinstance(self.origin, MemoryOrigin):
            fields = {"destinationPath": self.folder_path, "destinationRepoUrl": self.repo_url}
            if include_clone:
                fields["destinationCloneUrl"] = self.clone_url
            return fields
        fields = {"folderPath": self.folder_path, "repoUrl": self.repo_url}
        if include_clone:
            fields["cloneUrl"] = self.clone_url
        return fields

    @property
    def token(self) -> str:
        return f"{self.harvest_id}:{self.digest}"

    @property
    def content_bytes(self) -> int:
        return len(self.content.encode("utf-8"))

    @property
    def body_digest(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def repo_key(self) -> str | None:
        if self.repo_url:
            return normalize_remote(self.repo_url)
        return str(Path(self.folder_path).resolve()) if self.folder_path else None


@dataclass(frozen=True, kw_only=True)
class _OversizedHarvest(Harvest):
    byte_length: int
    approved_digest: str

    def cacheable(self) -> bool:
        fields = (self.harvest_id, self.registry_owner, *origin_fields(self.origin).values())
        return sum(len(value or "") for value in fields) <= publish_state.MAX_BYTES

    @property
    def content_bytes(self) -> int:
        return self.byte_length

    @property
    def body_digest(self) -> str:
        return self.approved_digest


def _compact_pending(item: Harvest) -> Harvest:
    if (
        not isinstance(item.origin, MemoryOrigin)
        or len(item.content) <= publish_state.MAX_BYTES // 4
    ):
        return item
    body = item.content.encode("utf-8")
    if len(body) <= publish_state.MAX_BYTES:
        return item
    return _OversizedHarvest(
        path=item.path,
        harvest_id=item.harvest_id,
        digest=item.digest,
        origin=item.origin,
        name="",
        folder_path=None,
        repo_url=None,
        clone_url=None,
        harvested_at=None,
        content="",
        registry_owner=item.registry_owner,
        legacy_owner=item.legacy_owner,
        byte_length=len(body),
        approved_digest=hashlib.sha256(body).hexdigest(),
    )


@dataclass(frozen=True)
class HarvestProblem:
    path: Path
    message: str


class HarvestAlreadyPublished(ValueError):
    def __init__(self, harvest_id: str, evidence: Mapping[str, object]) -> None:
        super().__init__(f"memory harvest already published: {harvest_id}")
        self.evidence = evidence


type Stamp = tuple[int, int]


@dataclass(frozen=True)
class HarvestSnapshot:
    items: tuple[Harvest, ...]
    problems: tuple[HarvestProblem, ...]
    _observations: Mapping[Path, tuple[Harvest, Stamp]] = field(
        default_factory=dict, compare=False, repr=False
    )
    ambiguous_ids: frozenset[str] = frozenset()
    _compact: bool = field(default=False, compare=False, repr=False)


def _slug(name: str, session_id: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", (name or session_id).lower()).strip("-") or "harvest"
    return f"{base}-{session_id[:8].replace('/', '-')}"


def _legacy_id(path: Path) -> str:
    relative_path = path.relative_to(harvests_dir()).as_posix().encode("utf-8")
    return f"legacy-{hashlib.sha256(relative_path).hexdigest()}"


def _validate_frontmatter_value(field: str, value: str | None) -> None:
    if value is not None and any(character in value for character in _FRONTMATTER_UNSAFE):
        raise ValueError(f"unsafe {field}")


def _parse_harvest(path: Path, raw: bytes) -> Harvest:
    text = raw.decode("utf-8")
    meta, body = parse_frontmatter(text)
    if meta.get("sourceProject") == {}:
        meta["sourceProject"] = None
    origin = parse_origin(meta)
    memory = isinstance(origin, MemoryOrigin)
    route_keys = (
        ("destinationPath", "destinationRepoUrl", "destinationCloneUrl")
        if memory
        else ("folderPath", "repoUrl", "cloneUrl")
    )
    if memory and (
        "harvestId" not in meta or any(key in meta for key in ("folderPath", "repoUrl", "cloneUrl"))
    ):
        raise ValueError("invalid memory harvest metadata")
    if not memory and any(
        key in meta for key in ("destinationPath", "destinationRepoUrl", "destinationCloneUrl")
    ):
        raise ValueError("invalid session harvest metadata")
    folder, repo, clone = (meta.get(key) or None for key in route_keys)
    if any(value is not None and not isinstance(value, str) for value in (folder, repo, clone)) or (
        memory and not folder and (repo or clone)
    ):
        raise ValueError("invalid destination")
    harvest_id = meta["harvestId"] if "harvestId" in meta else _legacy_id(path)
    if not isinstance(harvest_id, str) or not harvest_id.strip():
        raise ValueError("invalid harvestId")
    name = meta.get("name") or ""
    harvested_at = meta.get("harvestedAt") or None
    for key, value in (("name", name), ("harvestedAt", harvested_at)):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"invalid {key}")
    owner = None
    owner_lines = [
        line
        for line in text.partition("\n---\n")[0].splitlines()
        if line.partition(":")[0].strip() in ("registryOwner", "registryOwnerJson")
    ]
    legacy_owner = "registryOwner" in meta
    if owner_lines:
        field_name = "registryOwner" if legacy_owner else "registryOwnerJson"
        if not memory or len(owner_lines) != 1 or not owner_lines[0].startswith(field_name + ": "):
            raise ValueError("invalid memory harvest registry owner")
        raw_owner = owner_lines[0].removeprefix(field_name + ": ")
        if not legacy_owner and not raw_owner.startswith('"'):
            raise ValueError("invalid memory harvest registry owner")
        owner = raw_owner if legacy_owner else json.loads(raw_owner)
        if not isinstance(owner, str) or not Path(owner).is_absolute() or "\x00" in owner:
            raise ValueError("invalid memory harvest registry owner")
    repo, clone = public_routing(repo, clone)
    return Harvest(
        registry_owner=owner,
        legacy_owner=bool(legacy_owner and owner and owner == owner.rstrip()),
        path=path,
        harvest_id=harvest_id,
        digest=hashlib.sha256(raw).hexdigest(),
        origin=origin,
        name=name,
        folder_path=folder,
        repo_url=repo,
        clone_url=clone,
        harvested_at=harvested_at,
        content=body.removeprefix("\n") if memory else body.strip(),
    )


def _load_stamped(path: Path) -> tuple[Harvest, Stamp]:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("harvest is not a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return _parse_harvest(path, handle.read()), (info.st_mtime_ns, info.st_size)
    finally:
        if fd >= 0:
            os.close(fd)


def load_harvest_path(path: Path) -> Harvest:
    return _load_stamped(path)[0]


def _unchanged(
    path: Path, observations: Mapping[Path, tuple[Harvest, Stamp]]
) -> tuple[Harvest, Stamp] | None:
    cached = observations.get(path)
    if cached is None:
        return None
    item, stamp = cached
    try:
        info = os.lstat(path)
    except OSError:
        return None
    current = (info.st_mtime_ns, info.st_size)
    return (item, current) if stat.S_ISREG(info.st_mode) and current == stamp else None


def pending_count() -> int:
    d = harvests_dir()
    return sum(1 for _ in d.glob("*.md")) if d.exists() else 0


def snapshot_harvests(previous: HarvestSnapshot | None = None) -> HarvestSnapshot:
    return _scan_harvests(previous, compact=False)


def _scan_harvests(previous: HarvestSnapshot | None, *, compact: bool) -> HarvestSnapshot:
    d = harvests_dir()
    if not d.exists():
        return HarvestSnapshot(items=(), problems=())
    known = previous._observations if previous is not None else {}
    items: list[Harvest] = []
    problems: list[HarvestProblem] = []
    observations: dict[Path, tuple[Harvest, Stamp]] = {}
    for path in sorted(d.glob("*.md")):
        try:
            cached = _unchanged(path, known)
            if cached and not compact and isinstance(cached[0], _OversizedHarvest):
                cached = None
            item, stamp = cached or _load_stamped(path)
            if compact and (cached is None or previous is None or not previous._compact):
                item = _compact_pending(item)
        except (OSError, UnicodeDecodeError, ValueError) as error:
            problems.append(HarvestProblem(path=path, message=str(error)))
            continue
        items.append(item)
        if not isinstance(item, _OversizedHarvest) or item.cacheable():
            observations[path] = item, stamp
    counts = Counter(item.harvest_id for item in items)
    ambiguous_ids = frozenset(
        item.harvest_id
        for item in items
        if isinstance(item.origin, MemoryOrigin) and counts[item.harvest_id] > 1
    )
    problems.extend(
        HarvestProblem(item.path, f"duplicate harvest ID: {item.harvest_id}")
        for item in items
        if item.harvest_id in ambiguous_ids
    )
    items = [item for item in items if item.harvest_id not in ambiguous_ids]
    items.sort(key=lambda harvest: harvest.harvested_at or "", reverse=True)
    return HarvestSnapshot(
        items=tuple(items),
        problems=tuple(problems),
        _observations=observations,
        ambiguous_ids=ambiguous_ids,
        _compact=compact,
    )


def owned_snapshot(snapshot: HarvestSnapshot, path: str | Path) -> HarvestSnapshot:
    canonical = str(Path(path).resolve())
    legacy_ids = {
        item.harvest_id
        for item in snapshot.items
        if isinstance(item.origin, MemoryOrigin)
        and (item.legacy_owner or item.registry_owner is None)
    }
    records: dict[str, Operation] = {}
    failure = None
    if legacy_ids:
        from . import memory_operations  # noqa: PLC0415 -- legacy lifecycle ownership

        try:
            records = memory_operations.harvest_operations(legacy_ids)
        except (OSError, ValueError) as exc:
            failure = str(exc)
    items = []
    problems = list(snapshot.problems)
    for item in snapshot.items:
        owner = item.registry_owner
        if isinstance(item.origin, MemoryOrigin) and item.harvest_id in legacy_ids:
            try:
                if failure is not None:
                    raise ValueError(failure)
                owner = item.lifecycle_owner(records.get(item.harvest_id))
            except (OSError, ValueError) as exc:
                problems.append(
                    HarvestProblem(item.path, f"Cannot establish memory harvest owner: {exc}")
                )
                continue
        if owner is None or registry.owner_matches(owner, canonical):
            items.append(
                replace(item, registry_owner=owner, legacy_owner=False)
                if item.legacy_owner
                else item
            )
    return replace(snapshot, items=tuple(items), problems=tuple(problems))


def pending_snapshot(
    registry_path: str | Path, previous: HarvestSnapshot | None = None
) -> HarvestSnapshot:
    snapshot = _scan_harvests(previous, compact=True)
    if not any(isinstance(item.origin, MemoryOrigin) for item in snapshot.items):
        return snapshot
    snapshot = owned_snapshot(snapshot, registry_path)
    publications = registry.load_registry(registry_path).get("memoryPublications", {})
    pending: list[Harvest] = []
    problems = list(snapshot.problems)
    for item in snapshot.items:
        if isinstance(item.origin, MemoryOrigin) and item.harvest_id in publications:
            problems.append(
                HarvestProblem(item.path, f"memory harvest already published: {item.harvest_id}")
            )
        elif isinstance(item.origin, MemoryOrigin) and item.content_bytes > publish_state.MAX_BYTES:
            problems.append(
                HarvestProblem(
                    item.path, f"Memory harvest exceeds {publish_state.MAX_BYTES} UTF-8 bytes"
                )
            )
        else:
            pending.append(item)
    return replace(snapshot, items=tuple(pending), problems=tuple(problems))


def _harvest_bytes(  # noqa: PLR0913
    *,
    harvest_id: str,
    origin: HarvestOrigin,
    name: str,
    folder_path: str | None,
    repo_url: str | None,
    harvested_at: str | None,
    content: str,
    clone_url: str | None,
    registry_owner: str | None = None,
) -> bytes:
    _validate_frontmatter_value("repo_url", repo_url)
    _validate_frontmatter_value("clone_url", clone_url)
    repo_url, clone_url = public_routing(repo_url, clone_url)
    memory = isinstance(origin, MemoryOrigin)
    route = (
        {"destinationPath": folder_path, "destinationRepoUrl": repo_url}
        if memory
        else {"folderPath": folder_path, "repoUrl": repo_url}
    )
    if clone_url:
        route["destinationCloneUrl" if memory else "cloneUrl"] = clone_url
    fields = {
        "harvestId": harvest_id,
        **origin_fields(origin),
        **(
            {"registryOwnerJson": json.dumps(registry_owner)}
            if memory and registry_owner is not None
            else {}
        ),
        "name": name,
        **route,
        "harvestedAt": harvested_at,
    }
    for key, value in (
        ("folder_path", folder_path),
        ("harvested_at", harvested_at),
    ):
        _validate_frontmatter_value(key, value)
    for key, value in fields.items():
        _validate_frontmatter_value(key, value)
    parse_origin(fields)
    header = "\n".join(f"{key}: {value or ''}" for key, value in fields.items())
    body = content if memory else content.rstrip() + "\n"
    return f"---\n{header}\n---\n\n{body}".encode()


def write_harvest_unlocked(  # noqa: PLR0913
    *,
    session_id: str | None = None,
    origin: HarvestOrigin | None = None,
    harvest_id: str | None = None,
    name: str,
    folder_path: str | None,
    repo_url: str | None,
    harvested_at: str,
    content: str,
    clone_url: str | None = None,
    registry_path: Path | None = None,
    snapshot: HarvestSnapshot | None = None,
    registry_owner: str | None = None,
) -> Path:
    if (session_id is None) == (origin is None):
        raise ValueError("supply exactly one harvest origin")
    if session_id is not None:
        _validate_frontmatter_value("session_id", session_id)
        origin = SessionOrigin(session_id)
    assert origin is not None
    if harvest_id is not None and not re.fullmatch("[0-9a-f]{32}", harvest_id):
        raise ValueError("invalid preallocated harvestId")
    preallocated = harvest_id is not None
    harvest_id = harvest_id or uuid.uuid4().hex
    origin_id = origin.session_id if isinstance(origin, SessionOrigin) else origin.memory_id
    filename = (
        f"{harvest_id}.md"
        if isinstance(origin, MemoryOrigin)
        else f"{_slug(name, origin_id)}-{harvest_id}.md"
    )
    path = harvests_dir() / filename
    data = _harvest_bytes(
        harvest_id=harvest_id,
        origin=origin,
        name=name,
        folder_path=folder_path,
        repo_url=repo_url,
        harvested_at=harvested_at,
        content=content,
        clone_url=clone_url,
        registry_owner=registry_owner,
    )
    if preallocated:
        existing = _resolve_preallocated_harvest(harvest_id, origin, data, registry_path, snapshot)
        if existing is not None:
            return existing
        if path.exists() or path.is_symlink():
            raise ValueError("preallocated harvest path already exists")
    atomic_write_bytes(path, data)
    return path


def _resolve_preallocated_harvest(
    harvest_id: str,
    origin: HarvestOrigin,
    data: bytes,
    registry_path: Path | None,
    snapshot: HarvestSnapshot | None,
) -> Path | None:
    if isinstance(origin, MemoryOrigin):
        reg = registry.load_registry(registry_path or default_registry())
        evidence = reg.get("memoryPublications", {}).get(harvest_id)
        if evidence is not None:
            expected = {"harvestId": harvest_id, **origin_fields(origin)}
            if evidence.get("outcome") in ("changed", "no-change") and all(
                key in evidence and evidence[key] == value for key, value in expected.items()
            ):
                raise HarvestAlreadyPublished(harvest_id, evidence)
            raise ValueError("conflicting memory publication evidence")
    snapshot = snapshot if snapshot is not None else snapshot_harvests()
    existing = [item for item in snapshot.items if item.harvest_id == harvest_id]
    if harvest_id in snapshot.ambiguous_ids or len(existing) > 1:
        raise ValueError(f"duplicate harvest ID: {harvest_id}")
    if existing:
        item = existing[0]
        if item.digest == hashlib.sha256(data).hexdigest():
            return item.path
        raise ValueError("preallocated harvestId already exists")
    if isinstance(origin, MemoryOrigin):
        publish_state.require_unclaimed_memory(harvest_id)
    return None


def rewrite_memory_harvest_unlocked(
    item: Harvest,
    *,
    folder_path: str | None,
    repo_url: str | None,
    clone_url: str | None,
    content: str,
) -> Harvest:
    if (
        not isinstance(item.origin, MemoryOrigin)
        or load_harvest_path(item.path).token != item.token
    ):
        raise ValueError("memory harvest changed")
    data = _harvest_bytes(
        harvest_id=item.harvest_id,
        origin=item.origin,
        registry_owner=item.registry_owner,
        name=item.name,
        folder_path=folder_path,
        repo_url=repo_url,
        harvested_at=item.harvested_at,
        content=content,
        clone_url=clone_url,
    )
    atomic_write_bytes(item.path, data)
    return load_harvest_path(item.path)


def write_harvest(  # noqa: PLR0913
    *,
    session_id: str | None = None,
    origin: HarvestOrigin | None = None,
    harvest_id: str | None = None,
    name: str,
    folder_path: str | None,
    repo_url: str | None,
    harvested_at: str,
    content: str,
    clone_url: str | None = None,
    registry_path: Path | None = None,
) -> Path:
    with publish_lock():
        return write_harvest_unlocked(
            session_id=session_id,
            origin=origin,
            harvest_id=harvest_id,
            name=name,
            folder_path=folder_path,
            repo_url=repo_url,
            harvested_at=harvested_at,
            content=content,
            clone_url=clone_url,
            registry_path=registry_path,
        )


def load_harvests() -> list[Harvest]:
    return list(snapshot_harvests().items)
