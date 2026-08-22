from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .memory import parse_frontmatter
from .paths import ensure_state_dir, state_dir


def harvests_dir() -> Path:
    return state_dir() / "harvests"


@dataclass
class Harvest:
    path: Path
    session_id: str
    name: str
    folder_path: str | None
    repo_url: str | None
    harvested_at: str | None
    content: str


def _slug(name: str, session_id: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", (name or session_id).lower()).strip("-") or "harvest"
    return f"{base}-{session_id[:8]}"


def write_harvest(
    *,
    session_id: str,
    name: str,
    folder_path: str | None,
    repo_url: str | None,
    harvested_at: str,
    content: str,
) -> Path:
    ensure_state_dir()
    d = harvests_dir()
    d.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        f"sessionId: {session_id}\n"
        f"name: {name or ''}\n"
        f"folderPath: {folder_path or ''}\n"
        f"repoUrl: {repo_url or ''}\n"
        f"harvestedAt: {harvested_at or ''}\n"
        "---\n\n"
    )
    p = d / f"{_slug(name or '', session_id)}.md"
    p.write_text(fm + content.rstrip() + "\n", encoding="utf-8")
    return p


def load_harvests() -> list[Harvest]:
    d = harvests_dir()
    if not d.exists():
        return []
    out: list[Harvest] = []
    for f in d.glob("*.md"):
        meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
        out.append(
            Harvest(
                path=f,
                session_id=meta.get("sessionId", ""),
                name=meta.get("name") or "",
                folder_path=meta.get("folderPath") or None,
                repo_url=meta.get("repoUrl") or None,
                harvested_at=meta.get("harvestedAt") or None,
                content=body.strip(),
            )
        )
    return sorted(out, key=lambda h: h.harvested_at or "", reverse=True)
