from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..harvests import Harvest

NO_REPOSITORY = "no repository"


@dataclass(frozen=True)
class HarvestGroup:
    repo_key: str | None
    label: str
    folder: str | None
    items: tuple[Harvest, ...]


def _existing_folders(items: tuple[Harvest, ...]) -> Iterator[str]:
    return (
        item.folder_path for item in items if item.folder_path and os.path.isdir(item.folder_path)
    )


def _label(repo_key: str | None, items: tuple[Harvest, ...]) -> str:
    if repo_key is None:
        return NO_REPOSITORY
    folder = next((item.folder_path for item in items if item.folder_path), None)
    if folder:
        return Path(folder).name or folder
    _, _, path = repo_key.partition("/")
    return path or repo_key


def _group(repo_key: str | None, items: tuple[Harvest, ...]) -> HarvestGroup:
    return HarvestGroup(
        repo_key=repo_key,
        label=_label(repo_key, items),
        folder=next(_existing_folders(items), None),
        items=items,
    )


def harvest_groups(items: tuple[Harvest, ...]) -> tuple[HarvestGroup, ...]:
    buckets: dict[str | None, list[Harvest]] = defaultdict(list)
    for item in items:
        buckets[item.repo_key].append(item)
    groups = [_group(key, tuple(members)) for key, members in buckets.items()]
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
