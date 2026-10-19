from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..agents import canonical_agent
from ..usage import aggregate
from ..usage.codex import StartCache
from . import store
from .claude import extract_claude
from .codex import extract_codex
from .facts import Agent, Root, TranscriptFacts, TranscriptRef

CLAUDE_RECORD_TYPES = frozenset(
    {
        "user",
        "assistant",
        "system",
        "progress",
        "attachment",
        "summary",
        "last-prompt",
        "custom-title",
        "agent-name",
        "mode",
        "permission-mode",
        "queue-operation",
    }
)
CODEX_FIRST_TYPE = "session_meta"


@dataclass(frozen=True)
class Roots:
    claude: Path
    codex: Path
    archive: Path


type Extract = Callable[[TranscriptRef, StartCache | None], TranscriptFacts]


@dataclass(frozen=True)
class AnalysisAdapter:
    agent: Agent
    root: Root
    live_dir: Callable[[Roots], Path]
    globs: tuple[str, ...]
    companion_globs: tuple[str, ...]
    sniff: Callable[[Mapping[str, object]], bool]
    extract: Extract
    after_ingest: Callable[[sqlite3.Connection], object] | None = None


def _is_claude(record: Mapping[str, object]) -> bool:
    return "sessionId" in record and record.get("type") in CLAUDE_RECORD_TYPES


def _is_codex(record: Mapping[str, object]) -> bool:
    return record.get("type") == CODEX_FIRST_TYPE


def _claude_dir(roots: Roots) -> Path:
    return roots.claude


def _codex_dir(roots: Roots) -> Path:
    return roots.codex


def _extract_claude(ref: TranscriptRef, _starts: StartCache | None) -> TranscriptFacts:
    return extract_claude(ref)


ADAPTERS: tuple[AnalysisAdapter, ...] = (
    AnalysisAdapter(
        agent="claude-code",
        root="claude",
        live_dir=_claude_dir,
        globs=(
            "*/*.jsonl",
            "*/*/subagents/agent-*.jsonl",
            "*/*/subagents/workflows/*/agent-*.jsonl",
        ),
        companion_globs=("*/subagents/**/agent-*.jsonl",),
        sniff=_is_claude,
        extract=_extract_claude,
    ),
    AnalysisAdapter(
        agent="codex",
        root="codex",
        live_dir=_codex_dir,
        globs=("**/*.jsonl",),
        companion_globs=(),
        sniff=_is_codex,
        extract=extract_codex,
        after_ingest=store.roll_up_codex_sessions,
    ),
)
BY_AGENT: Mapping[Agent, AnalysisAdapter] = {a.agent: a for a in ADAPTERS}
ARCHIVE_GLOBS: tuple[str, ...] = (
    "*/*.jsonl",
    *(f"*/{pattern}" for a in ADAPTERS for pattern in a.companion_globs),
)
TOKENS_ONLY_SOURCES: tuple[str, ...] = tuple(
    name for name in aggregate.READERS if canonical_agent(name) not in BY_AGENT
)
