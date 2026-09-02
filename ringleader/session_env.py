"""Which agent session is this process running inside?

The `sources/` seam reads stored sessions; this one reads the live process, so
a skill can act on the session it is currently part of without being told its id.
"""

from __future__ import annotations

import os

# source name -> the variable that agent exports.
# Verified for claude-code (CLAUDE_CODE_SESSION_ID), codex (CODEX_THREAD_ID/
# CODEX_SESSION_ID) and pi (PI_SESSION_ID) via `env | sort` inside each agent
# and cross-checked against the on-disk session id. opencode probe
# (OPENCODE_SESSION_ID) is the variable opencode exposes when it does — see
# https://github.com/anomalyco/opencode/issues/9292 — but as of 1.18.25
# `opencode run "env | sort"` does not yet export it, so it is a forward
# probe: its Source exists, but `--current` cannot resolve it yet.
#
# Ordering matters: an agent's tool subprocess inherits the parent's env, so a
# nested shell can carry two session vars (e.g. CLAUDE_CODE_SESSION_ID from
# the outer Claude Code session and CODEX_THREAD_ID from the inner Codex
# session). We iterate in insertion order and return the first match, so more
# likely inner agents are listed before claude-code to prefer the innermost
# session.
PROBES: dict[str, str] = {
    "codex": "CODEX_THREAD_ID",
    "pi": "PI_SESSION_ID",
    "opencode": "OPENCODE_SESSION_ID",
    "claude-code": "CLAUDE_CODE_SESSION_ID",
}

# Codex sets both CODEX_THREAD_ID and CODEX_SESSION_ID to the same value for
# top-level sessions; for sub-agents THREAD_ID is the child's own id while
# SESSION_ID names the parent. Prefer THREAD_ID.
_CODEX_FALLBACK = "CODEX_SESSION_ID"


def current_session() -> tuple[str, str] | None:
    for source, var in PROBES.items():
        value = os.environ.get(var, "").strip()
        if value:
            return source, value
        if source == "codex":
            fallback = os.environ.get(_CODEX_FALLBACK, "").strip()
            if fallback:
                return source, fallback
    return None
