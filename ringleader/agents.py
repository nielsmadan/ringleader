from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CLAUDE_CODE = "claude-code"
CLAUDE_ALIAS = "claude"


def canonical_agent(name: str) -> str:
    return CLAUDE_CODE if name == CLAUDE_ALIAS else name


def display_agent(name: str) -> str:
    return CLAUDE_ALIAS if name == CLAUDE_CODE else name


def source_of(record: Mapping[str, Any]) -> str:
    embedded = record.get("thread") or {}
    return record.get("source") or embedded.get("source") or CLAUDE_CODE
