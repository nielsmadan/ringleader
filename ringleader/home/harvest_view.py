from __future__ import annotations

import os
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..gitinfo import lookup_git_project, resolve_many
from ..harvests import Harvest, HarvestSnapshot, pending_snapshot
from ..memory_types import MemoryOrigin

NO_REPOSITORY = "no repository"


@dataclass(frozen=True)
class HarvestGroup:
    repo_key: str | None
    label: str
    folder: str | None
    items: tuple[Harvest, ...]


@dataclass(frozen=True)
class HarvestQueueRead:
    started: int
    snapshot: HarvestSnapshot | None
    routed: frozenset[str] = frozenset()
    error: str | None = None


def routed_harvests(items: tuple[Harvest, ...]) -> frozenset[str]:
    memory_items = [
        item for item in items if isinstance(item.origin, MemoryOrigin) and item.folder_path
    ]
    projects = resolve_many(
        (item.folder_path for item in memory_items if item.folder_path), lookup_git_project
    )
    return frozenset(
        item.token
        for item in memory_items
        if item.folder_path
        and (project := projects.get(item.folder_path))
        and project.root == item.folder_path
        and project.key == item.repo_key
    )


def read_harvest_queue(
    path: str | Path, previous: HarvestSnapshot | None = None
) -> HarvestQueueRead:
    started = time.monotonic_ns()
    try:
        snapshot = pending_snapshot(path, previous)
        return HarvestQueueRead(started, snapshot, routed_harvests(snapshot.items))
    except Exception as error:  # noqa: BLE001 -- report a failed queue read without losing the scan
        return HarvestQueueRead(started, None, error=str(error))


def needs_checkout(item: Harvest, routed: frozenset[str]) -> bool:
    return isinstance(item.origin, MemoryOrigin) and item.token not in routed


def _existing_folders(items: tuple[Harvest, ...]) -> Iterator[str]:
    return (
        item.folder_path for item in items if item.folder_path and os.path.isdir(item.folder_path)
    )


def _label(repo_key: str | None, items: tuple[Harvest, ...], destination: str | None) -> str:
    if repo_key is None:
        return (
            "unassigned"
            if any(isinstance(item.origin, MemoryOrigin) for item in items)
            else NO_REPOSITORY
        )
    folder = destination or next((item.folder_path for item in items if item.folder_path), None)
    if folder:
        return Path(folder).name or folder
    _, _, path = repo_key.partition("/")
    return path or repo_key


def _group(
    repo_key: str | None, items: tuple[Harvest, ...], destination: str | None
) -> HarvestGroup:
    if destination is None:
        folder = next(_existing_folders(items), None)
    else:
        folder = destination if os.path.isdir(destination) else None
    return HarvestGroup(
        repo_key=repo_key,
        label=_label(repo_key, items, destination),
        folder=folder,
        items=items,
    )


def harvest_groups(
    items: tuple[Harvest, ...], *, routed: frozenset[str] | None = None
) -> tuple[HarvestGroup, ...]:
    def unassigned(item: Harvest) -> bool:
        return routed is not None and needs_checkout(item, routed)

    destinations: dict[str | None, set[str]] = defaultdict(set)
    for item in items:
        if isinstance(item.origin, MemoryOrigin) and item.folder_path and not unassigned(item):
            destinations[item.repo_key].add(item.folder_path)

    buckets: dict[tuple[str | None, str | None, bool], list[Harvest]] = defaultdict(list)
    for item in items:
        if unassigned(item):
            buckets[None, None, True].append(item)
            continue
        repo_key = item.repo_key
        if isinstance(item.origin, MemoryOrigin):
            destination = item.folder_path
            unassigned_memory = destination is None
        else:
            candidates = destinations[repo_key]
            destination = next(iter(candidates)) if len(candidates) == 1 else None
            if item.folder_path in candidates:
                destination = item.folder_path
            unassigned_memory = False
        buckets[repo_key, destination, unassigned_memory].append(item)
    groups = [
        _group(key, tuple(members), destination)
        for (key, destination, _), members in buckets.items()
    ]
    return tuple(sorted(groups, key=lambda group: (-len(group.items), group.label.lower())))


def publish_all_cwd(items: tuple[Harvest, ...], home: Path) -> str:
    folders = list(_existing_folders(items))
    if not folders:
        return str(home)
    common = os.path.commonpath(folders)
    return str(home) if common == os.sep else common


def preview_line(item: Harvest) -> str:
    lines = (" ".join(line.split()) for line in item.content.splitlines())
    return next((line for line in lines if line and not line.startswith("#")), "")
