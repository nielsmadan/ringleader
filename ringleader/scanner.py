from __future__ import annotations

import sys

from .gitinfo import resolve_git_project
from .sources.base import NormalizedSession
from .sources.claude_code import ClaudeCodeSource
from .sources.codex import CodexSource

SOURCES = [ClaudeCodeSource(), CodexSource()]


def active_sources() -> list:
    # Fresh instances (not SOURCES) so each call re-reads the env-based default
    # roots — SOURCES is constructed once at import time and would otherwise
    # keep whatever root was current then, which breaks under monkeypatched env.
    return [s for s in (ClaudeCodeSource(), CodexSource()) if s.root.exists()]


def source_by_name(name: str):
    return next((s for s in SOURCES if s.name == name), None)


def scan(sources=None, git_resolver=None) -> list[NormalizedSession]:
    if sources is None:
        sources = SOURCES
    if git_resolver is None:
        git_resolver = resolve_git_project
    cache: dict[str, object] = {}  # cwd -> GitProject|None, resolved once per cwd
    out: list[NormalizedSession] = []
    for src in sources:
        for ref in src.discover():
            try:
                ns = src.parse(ref)
            except OSError as e:
                print(f"skipped {ref.file}: {e}", file=sys.stderr)
                continue
            if ns.cwd is not None:
                if ns.cwd not in cache:
                    cache[ns.cwd] = git_resolver(ns.cwd)
                gp = cache[ns.cwd]
                if gp is not None:
                    ns.git_project_key = gp.key
                    ns.git_project_label = gp.label
                    ns.git_root = gp.root
                    ns.git_remote = gp.remote
            out.append(ns)
    return out
