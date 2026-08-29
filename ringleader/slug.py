"""Claude Code's project-slug function, reproduced.

Claude Code stores a session's transcript under `~/.claude/projects/<slug>`, where
the slug is the launch directory's absolute path with every non-alphanumeric
character replaced by `-`. Past 200 characters it truncates and appends a base36
rolling hash of the original path.

The slug is not reversible — `-` stands for `/`, `.`, `_` and everything else
alike — so paths are always mapped forward to slugs, never decoded back.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

SLUG_MAX_LEN = 200
_UINT32 = 0xFFFFFFFF
_INT32_MIN = 0x80000000
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _rolling_hash(s: str) -> int:
    """Claude Code's `h = (h << 5) - h + charCode | 0` — hash*31 + c, truncated to
    a signed 32-bit int at every step."""
    h = 0
    for ch in s:
        h = (h << 5) - h + ord(ch)
        h &= _UINT32
        if h >= _INT32_MIN:
            h -= _INT32_MIN * 2
    return h


def _base36(n: int) -> str:
    if n == 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _DIGITS[r] + out
    return out


def slugify(path: str | Path) -> str:
    raw = str(path)
    slug = re.sub(r"[^a-zA-Z0-9]", "-", raw)
    if len(slug) <= SLUG_MAX_LEN:
        return slug
    return slug[:SLUG_MAX_LEN] + "-" + _base36(abs(_rolling_hash(raw)))


def normalize(path: str | Path) -> str:
    """Absolute and lexically normalized, without resolving symlinks — a resolved
    path would not match the cwd the agent recorded."""
    return os.path.abspath(os.path.expanduser(str(path)))


def is_beneath(path: str | None, root: str) -> bool:
    """Lexical containment, respecting directory boundaries so `/a/b-two` is not
    beneath `/a/b`. Deliberately not `projects.matches()`: that compares git
    labels, which would sweep in sibling checkouts of the same repo."""
    if not path:
        return False
    p, r = normalize(path), normalize(root)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def remap(path: str, old_root: str, new_root: str) -> str:
    """Rewrite `path` from beneath `old_root` to the same spot beneath `new_root`."""
    p, old = normalize(path), normalize(old_root)
    rel = os.path.relpath(p, old)
    return normalize(new_root) if rel == "." else os.path.join(normalize(new_root), rel)
