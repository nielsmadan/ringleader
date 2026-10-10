from __future__ import annotations

CLAUDE_CODE = "claude-code"
CLAUDE_ALIAS = "claude"


def canonical_agent(name: str) -> str:
    return CLAUDE_CODE if name == CLAUDE_ALIAS else name


def display_agent(name: str) -> str:
    return CLAUDE_ALIAS if name == CLAUDE_CODE else name
