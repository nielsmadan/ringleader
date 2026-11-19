from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .paths import AGENT_CONFIG_DIRS, AGENT_INSTRUCTION_BASENAMES, AGENT_INSTRUCTION_DIRS

_MAX_ENTRIES = 250_000
_MAX_ALIAS_BYTES = 128 * 1024
type Identity = tuple[int, int]
type Area = Literal["checkout", "configuration", "protected"]


def _identity(path: Path) -> Identity | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


@dataclass(frozen=True)
class InstructionTargets:
    identities: frozenset[Identity]
    paths: frozenset[Path]
    aliases: tuple[str, ...]

    def protects(self, path: Path) -> bool:
        return any(
            parent in self.paths or _identity(parent) in self.identities
            for parent in (path, *path.parents)
        )


def document_scope(path: Path, config_roots: Iterable[Path]) -> tuple[Path, bool]:
    fixture = os.environ.get("RINGLEADER_MEMORY_FIXTURE_ROOT")
    parents = [p for p in path.parents if fixture is None or p.is_relative_to(fixture)]
    checkout = next((p for p in parents if (p / ".git").exists()), None)
    if checkout is not None:
        return checkout, True
    home = (Path(fixture) / "home" if fixture else Path.home()).resolve()
    configs = {config.resolve() for config in config_roots}
    recurse = path.parent != home
    for parent in parents:
        if parent == home:
            break
        markers = {
            marker.resolve() for name in AGENT_CONFIG_DIRS if (marker := parent / name).is_dir()
        }
        if markers - configs:
            return parent, True
        if markers and parent == path.parent:
            recurse = False
    return path.parent, recurse


def _child_area(area: Area, name: str, in_checkout: bool) -> Area | None:
    if area == "protected" or name in AGENT_INSTRUCTION_BASENAMES:
        return "protected"
    if area == "configuration" and name in AGENT_INSTRUCTION_DIRS:
        return "protected"
    if not in_checkout or name == ".git":
        return None
    return "configuration" if name in AGENT_CONFIG_DIRS else "checkout"


def instruction_targets(
    checkout: Path, config_roots: Iterable[Path], *, recurse: bool = True
) -> InstructionTargets:
    root = checkout.resolve()
    pending: list[tuple[Path, Area]] = [(root, "checkout")]
    seen: set[tuple[Identity, Area]] = set()
    protected: set[Identity] = set()
    names: set[Path] = set()
    pending.extend((config, "configuration") for config in config_roots)
    checkout_links: list[Path] = []
    candidates: dict[Path, Identity] = {}
    count = 0
    while pending:
        directory, area = pending.pop()
        if area == "configuration":
            directory = directory.resolve()
            names.update(directory / name for name in AGENT_INSTRUCTION_DIRS)
        identity = _identity(directory)
        if identity is None or (identity, area) in seen:
            continue
        seen.add((identity, area))
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_ENTRIES:
                    raise ValueError("Too many entries to verify agent instruction targets")
                path = Path(entry.path)
                child_area = _child_area(
                    area, entry.name.casefold(), recurse and path.is_relative_to(root)
                )
                if child_area is None:
                    continue
                link = entry.is_symlink()
                if link and child_area == "checkout":
                    checkout_links.append(path)
                    continue
                try:
                    info = entry.stat()
                except FileNotFoundError:
                    if link and child_area == "protected":
                        names.add(path.resolve())
                    elif link and child_area == "configuration":
                        pending.append((path, child_area))
                    continue
                key = info.st_dev, info.st_ino
                if child_area == "protected":
                    protected.add(key)
                    if area != "protected" or link:
                        names.add(path.resolve())
                elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    candidates[path] = key
                if stat.S_ISDIR(info.st_mode):
                    pending.append((path, child_area))
    targets = InstructionTargets(frozenset(protected), frozenset(names), ())
    names.update(p for p in checkout_links if targets.protects(p.resolve()))
    names.update(p for p, key in candidates.items() if key in protected)
    excluded = {p for p in names if p.is_relative_to(root)}
    relative = tuple(
        sorted(
            str(p.relative_to(root)) for p in excluded if not any(q in excluded for q in p.parents)
        )
    )
    if len(json.dumps(relative)) > _MAX_ALIAS_BYTES:
        raise ValueError("Instruction aliases exceed the 128 KiB verification limit")
    return InstructionTargets(targets.identities, frozenset(names), relative)
