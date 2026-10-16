from __future__ import annotations

import os
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..picker.app import Choice

type HandoffPurpose = Literal["triage", "publish"]


@dataclass(frozen=True)
class ResumeIntent:
    choice: Choice


@dataclass(frozen=True)
class AgentHandoffIntent:
    agent: str
    cwd: str
    prompt: str
    purpose: HandoffPurpose
    label: str


type ExitIntent = ResumeIntent | AgentHandoffIntent


def triage_prompt(label: str, selector: str) -> str:
    q = shlex.quote(selector)
    return (
        f"Use the ringleader-triage skill to triage the dormant sessions of the project {label}. "
        f"Work only this project: in the prune step run `rl prune --project {q}` (and `--delete` "
        f"only after the user agrees), and build the inbox with `rl triage --project {q} --json` "
        "(no --recent bound), oldest first. If the ringleader-triage skill is not available, "
        "say so and stop."
    )


def publish_prompt(repo_label: str | None) -> str:
    scope = f" for {repo_label} only." if repo_label else "."
    return f"Use the ringleader-publish skill to publish the pending ringleader harvests{scope}"


def handoff_cwd(candidates: Sequence[str | None], home: Path) -> str:
    return next((c for c in candidates if c and os.path.isdir(c)), str(home))
