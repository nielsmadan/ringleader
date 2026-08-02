from __future__ import annotations

import shutil
from pathlib import Path

from .paths import archive_root, default_registry, state_dir


def migrate(old_dir: Path) -> list[str]:
    """Move a legacy repo-root registry.json/archive/ into state_dir(). No-op if
    state already exists there — this is meant to run once, at install time."""
    old_dir = Path(old_dir)
    if default_registry().exists():
        return []

    moved: list[str] = []
    old_registry = old_dir / "registry.json"
    old_archive = old_dir / "archive"
    if not old_registry.exists() and not old_archive.exists():
        return []

    state_dir().mkdir(parents=True, exist_ok=True)

    if old_registry.exists():
        dest = default_registry()
        shutil.move(str(old_registry), str(dest))
        moved.append(str(dest))

    if old_archive.exists():
        dest = archive_root()
        shutil.move(str(old_archive), str(dest))
        moved.append(str(dest))

    return moved
