from __future__ import annotations

import shutil
from pathlib import Path

from .paths import archive_root, default_registry, ensure_state_dir


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
    for src, dest in to_move:
        shutil.move(str(src), str(dest))
        moved.append(str(dest))
    return moved
