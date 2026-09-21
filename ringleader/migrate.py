from __future__ import annotations

import shutil
from contextlib import ExitStack
from pathlib import Path

from .paths import archive_root, default_registry, ensure_state_dir
from .stateio import publish_lock, registry_lock


def migrate(old_dir: Path) -> list[str]:
    """Move a legacy repo-root registry.json/archive/ into state_dir(). No-op if
    state already exists there — this is meant to run once, at install time."""
    old_dir = Path(old_dir)
    to_move: list[tuple[Path, Path]] = []
    old_registry = old_dir / "registry.json"
    old_archive = old_dir / "archive"
    if old_registry.exists() and not default_registry().exists():
        to_move.append((old_registry, default_registry()))
    if old_archive.exists() and not archive_root().exists():
        to_move.append((old_archive, archive_root()))
    if not to_move:
        return []

    ensure_state_dir()
    moved: list[str] = []
    with publish_lock():
        for src, dest in to_move:
            with ExitStack() as locks:
                if src == old_registry:
                    for path in sorted({src.resolve(), dest.resolve()}):
                        locks.enter_context(registry_lock(path))
                if src.exists() and not dest.exists():
                    shutil.move(str(src), str(dest))
                    moved.append(str(dest))
    return moved
