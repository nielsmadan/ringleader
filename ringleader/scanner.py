from __future__ import annotations

import sys

from .gitinfo import resolve_git_project
from .sources.base import NormalizedSession, PickerHidingSource, PickerShowingSource
from .sources.claude_code import ClaudeCodeSource
from .sources.codex import CodexSource

SOURCE_TYPES = (ClaudeCodeSource, CodexSource)


def _new_sources():
    return [source_type() for source_type in SOURCE_TYPES]


SOURCES = _new_sources()


def active_sources() -> list:
    # Fresh instances (not SOURCES) so each call re-reads the env-based default
    # roots — SOURCES is constructed once at import time and would otherwise
    # keep whatever root was current then, which breaks under monkeypatched env.
    return [source for source in _new_sources() if source.root.exists()]


def source_by_name(name: str):
    return next((source for source in _new_sources() if source.name == name), None)


def hide_from_picker(thread: dict) -> bool:
    """Clear the agent's own resume-picker entry for a thread, for sources that
    keep an index alongside the transcripts. Raises if the source has such an
    index but cannot be updated — callers must not then move or delete the
    transcript, or the picker is left pointing at a file that is gone."""
    src = source_by_name(thread.get("source") or "claude-code")
    if not (
        src and "hides_from_picker" in src.capabilities and isinstance(src, PickerHidingSource)
    ):
        return False
    return src.hide_from_picker(thread["sessionId"], thread["file"])


def show_in_picker(thread: dict, original_path: str) -> bool:
    """Restore a picker entry hidden by `hide_from_picker`. `original_path` is where
    the transcript lived when it was hidden — the index still points there — so the
    file must be back at that path before calling this."""
    src = source_by_name(thread.get("source") or "claude-code")
    if not (src and "shows_in_picker" in src.capabilities and isinstance(src, PickerShowingSource)):
        return False
    return src.show_in_picker(thread["sessionId"], original_path)


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
