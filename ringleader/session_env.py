"""Which agent session is this process running inside?

The `sources/` seam reads transcripts off disk; this one reads the live process, so
a skill can act on the session it is currently part of without being told its id.
"""

from __future__ import annotations

import os

# source name -> the variable that agent exports. Verified for claude-code only;
# `rl session whoami` inside another agent is how the rest get filled in.
PROBES = {
    "claude-code": "CLAUDE_CODE_SESSION_ID",
}


def current_session() -> tuple[str, str] | None:
    for source, var in PROBES.items():
        value = os.environ.get(var, "").strip()
        if value:
            return source, value
    return None
