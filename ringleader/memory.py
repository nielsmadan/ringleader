from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .gitinfo import resolve_git_project
from .sources.claude_code import default_projects_dir as projects_dir
from .transcript import parse_transcript

INDEX_NAME = "MEMORY.md"

_FIXED_RE = re.compile(r"\b(FIXED|DONE)\b")
_STATUS_RE = re.compile(r"(?i)status \(as of|\bsteps?\s+\d+[-–]?\d*\s+(complete|done)|\b\d+\s+tests?\s+passing")
_LINK_RE = re.compile(r"\]\(([^)]+\.md)\)")


@dataclass
class MemoryEntry:
    path: str
    encoded_dir: str
    project: str | None
    mtime: str
    name: str | None
    description: str | None
    mem_type: str | None
    body: str
    is_inline: bool = False
    git_project_key: str | None = None
    git_project_label: str | None = None
    git_root: str | None = None
    git_remote: str | None = None
    repo_gone: bool = False
    obsolete_signals: list[str] = field(default_factory=list)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5:]
    meta: dict = {}
    stack = [(-1, meta)]                 # (indent, dict) for one level of nesting
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(":")
        val = val.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = val
    return meta, body


def repo_doc_path(cwd: str | None) -> Path | None:
    if not cwd:
        return None
    root = Path(cwd)
    for name in ("CLAUDE.md", "AGENTS.md"):
        p = root / name
        if p.exists():
            return p
    return None


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def detect_obsolete(body: str, *, repo_gone: bool, repo_doc_text: str | None) -> list[str]:
    signals: list[str] = []
    if repo_gone:
        signals.append("repo-gone")
    if _FIXED_RE.search(body):
        signals.append("fixed")
    if _STATUS_RE.search(body):
        signals.append("status-snapshot")
    if repo_doc_text:
        doc = _normalize(repo_doc_text)
        # each non-trivial line of the body already present verbatim in the doc
        lines = [ln for ln in body.splitlines() if len(ln.strip()) > 25]
        if lines and all(_normalize(ln) in doc for ln in lines):
            signals.append("already-in-doc")
    return signals


def parse_index_links(text: str) -> list[str]:
    return [m for m in _LINK_RE.findall(text) if Path(m).name != INDEX_NAME]


def rewrite_index_without(text: str, targets: set[str]) -> str:
    keep = []
    for line in text.splitlines():
        links = _LINK_RE.findall(line)
        if links and any(l in targets for l in links):
            continue
        keep.append(line)
    return "\n".join(keep).rstrip() + "\n"


def iter_memory_dirs(root) -> Iterator[Path]:
    root = Path(root)
    if not root.exists():
        return
    for d in sorted(root.glob("*/memory")):
        if d.is_dir():
            yield d


def _iso_mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()


def _resolve_cwd(project_dir: Path) -> str | None:
    for tp in sorted(project_dir.glob("*.jsonl")):
        try:
            info = parse_transcript(tp, tail_turns=1)
        except OSError:
            continue
        if info.cwd:
            return info.cwd
    return None


def scan_memory(root, git_resolver=None) -> list[MemoryEntry]:
    if git_resolver is None:
        git_resolver = resolve_git_project
    root = Path(root)
    cwd_cache: dict[str, str | None] = {}
    git_cache: dict[str, object] = {}
    out: list[MemoryEntry] = []
    for mem_dir in iter_memory_dirs(root):
        project_dir = mem_dir.parent
        encoded = project_dir.name
        if encoded not in cwd_cache:
            cwd_cache[encoded] = _resolve_cwd(project_dir)
        cwd = cwd_cache[encoded]
        gp = None
        if cwd is not None:
            if cwd not in git_cache:
                git_cache[cwd] = git_resolver(cwd)
            gp = git_cache[cwd]
        repo_gone = bool(cwd) and not Path(cwd).exists()
        doc = repo_doc_path(cwd)
        doc_text = None
        if doc:
            try:
                doc_text = doc.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                doc_text = None
        for f in sorted(mem_dir.glob("*.md")):
            if f.name == INDEX_NAME:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                print(f"skipped {f}: {e}", file=sys.stderr)
                continue
            meta, body = parse_frontmatter(text)
            md = meta.get("metadata", {}) if isinstance(meta.get("metadata"), dict) else {}
            out.append(MemoryEntry(
                path=str(f), encoded_dir=encoded, project=cwd,
                mtime=_iso_mtime(f),
                name=meta.get("name"), description=meta.get("description"),
                mem_type=md.get("type"), body=body.strip(),
                git_project_key=getattr(gp, "key", None),
                git_project_label=getattr(gp, "label", None),
                git_root=getattr(gp, "root", None),
                git_remote=getattr(gp, "remote", None),
                repo_gone=repo_gone,
                obsolete_signals=detect_obsolete(body, repo_gone=repo_gone,
                                                 repo_doc_text=doc_text),
            ))
    return out
