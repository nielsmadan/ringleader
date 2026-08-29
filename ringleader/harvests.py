from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from .gitinfo import normalize_remote
from .memory import parse_frontmatter
from .paths import state_dir
from .stateio import atomic_write_bytes, publish_lock


def harvests_dir() -> Path:
    return state_dir() / "harvests"


@dataclass(frozen=True)
class Harvest:
    path: Path
    harvest_id: str
    digest: str
    session_id: str
    name: str
    folder_path: str | None
    repo_url: str | None
    clone_url: str | None
    harvested_at: str | None
    content: str

    @property
    def token(self) -> str:
        return f"{self.harvest_id}:{self.digest}"

    @property
    def content_bytes(self) -> int:
        return len(self.content.encode("utf-8"))

    @property
    def repo_key(self) -> str | None:
        if self.repo_url:
            return normalize_remote(self.repo_url)
        return str(Path(self.folder_path).resolve()) if self.folder_path else None


@dataclass(frozen=True)
class HarvestProblem:
    path: Path
    message: str


@dataclass(frozen=True)
class HarvestSnapshot:
    items: tuple[Harvest, ...]
    problems: tuple[HarvestProblem, ...]


def _slug(name: str, session_id: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", (name or session_id).lower()).strip("-") or "harvest"
    return f"{base}-{session_id[:8]}"


def _legacy_id(path: Path) -> str:
    relative_path = path.relative_to(harvests_dir()).as_posix().encode("utf-8")
    return f"legacy-{hashlib.sha256(relative_path).hexdigest()}"


def _parse_harvest(path: Path, raw: bytes) -> Harvest:
    text = raw.decode("utf-8")
    meta, body = parse_frontmatter(text)
    session_id = meta.get("sessionId")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("missing non-empty sessionId")
    harvest_id = meta["harvestId"] if "harvestId" in meta else _legacy_id(path)
    if not isinstance(harvest_id, str) or not harvest_id.strip():
        raise ValueError("invalid harvestId")
    return Harvest(
        path=path,
        harvest_id=harvest_id,
        digest=hashlib.sha256(raw).hexdigest(),
        session_id=session_id,
        name=meta.get("name") or "",
        folder_path=meta.get("folderPath") or None,
        repo_url=meta.get("repoUrl") or None,
        clone_url=meta.get("cloneUrl") or None,
        harvested_at=meta.get("harvestedAt") or None,
        content=body.strip(),
    )


def load_harvest_path(path: Path) -> Harvest:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("harvest is not a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return _parse_harvest(path, handle.read())
    finally:
        if fd >= 0:
            os.close(fd)


def snapshot_harvests() -> HarvestSnapshot:
    d = harvests_dir()
    if not d.exists():
        return HarvestSnapshot(items=(), problems=())
    items: list[Harvest] = []
    problems: list[HarvestProblem] = []
    for path in sorted(d.glob("*.md")):
        try:
            items.append(load_harvest_path(path))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            problems.append(HarvestProblem(path=path, message=str(error)))
    items.sort(key=lambda harvest: harvest.harvested_at or "", reverse=True)
    return HarvestSnapshot(items=tuple(items), problems=tuple(problems))


def _write_harvest_unlocked(  # noqa: PLR0913
    *,
    session_id: str,
    name: str,
    folder_path: str | None,
    repo_url: str | None,
    harvested_at: str,
    content: str,
    clone_url: str | None = None,
) -> Path:
    harvest_id = uuid.uuid4().hex
    fields = [
        "---",
        f"harvestId: {harvest_id}",
        f"sessionId: {session_id}",
        f"name: {name or ''}",
        f"folderPath: {folder_path or ''}",
        f"repoUrl: {repo_url or ''}",
    ]
    if clone_url:
        fields.append(f"cloneUrl: {clone_url}")
    fields.extend((f"harvestedAt: {harvested_at or ''}", "---", "", content.rstrip(), ""))
    data = "\n".join(fields).encode("utf-8")
    path = harvests_dir() / f"{_slug(name or '', session_id)}-{harvest_id}.md"
    atomic_write_bytes(path, data)
    return path


def write_harvest(  # noqa: PLR0913
    *,
    session_id: str,
    name: str,
    folder_path: str | None,
    repo_url: str | None,
    harvested_at: str,
    content: str,
    clone_url: str | None = None,
) -> Path:
    with publish_lock():
        return _write_harvest_unlocked(
            session_id=session_id,
            name=name,
            folder_path=folder_path,
            repo_url=repo_url,
            harvested_at=harvested_at,
            content=content,
            clone_url=clone_url,
        )


def load_harvests() -> list[Harvest]:
    return list(snapshot_harvests().items)
