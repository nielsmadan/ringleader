from __future__ import annotations

import re
from pathlib import Path
from typing import Any

INDEX_NAME = "MEMORY.md"
_NONTRIVIAL_LINE_LEN = 25

_FIXED_RE = re.compile(r"\b(FIXED|DONE)\b")
_STATUS_RE = re.compile(
    r"(?i)status \(as of|\bsteps?\s+\d+[-–]?\d*\s+(complete|done)|\b\d+\s+tests?\s+passing"  # noqa: RUF001 -- the char class intentionally matches both hyphen and en dash variants of a numeric range
)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    meta: dict[str, Any] = {}
    stack = [(-1, meta)]  # (indent, dict) for one level of nesting
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
            child: dict[str, Any] = {}
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
        lines = [ln for ln in body.splitlines() if len(ln.strip()) > _NONTRIVIAL_LINE_LEN]
        if lines and all(_normalize(ln) in doc for ln in lines):
            signals.append("already-in-doc")
    return signals
