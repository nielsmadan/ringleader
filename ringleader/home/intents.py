from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .. import overview
from ..memory_types import MemoryOrigin
from .state import ProjectRef

if TYPE_CHECKING:
    from ..memory_types import MemoryRef
    from ..picker.app import Choice
    from .harvest_view import HarvestGroup

type HandoffPurpose = Literal["triage", "publish", "memory"]
_HARVEST_TOKEN = re.compile(r"(?:[0-9a-f]{32}|legacy-[0-9a-f]{64}):[0-9a-f]{64}")


def publish_tokens(group: HarvestGroup) -> tuple[str, ...]:
    tokens = tuple(item.token for item in group.items)
    if any(not _HARVEST_TOKEN.fullmatch(token) for token in tokens):
        raise ValueError("This group has an invalid harvest token")
    return tokens


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


def handoff_cwd(
    candidates: Sequence[str | None], home: Path, is_dir: Callable[[str], bool] = os.path.isdir
) -> str:
    return next((c for c in candidates if c and is_dir(c)), str(home))


def triage_handoff(
    agent: str,
    project: ProjectRef,
    threads: Sequence[dict[str, Any]],
    home: Path,
    *,
    is_dir: Callable[[str], bool] = os.path.isdir,
) -> AgentHandoffIntent:
    members = sorted(
        (t for t in threads if overview.group_key(t) == project.key),
        key=lambda t: t.get("mtime") or "",
        reverse=True,
    )
    candidates = [path for t in members for path in (t.get("gitRoot"), t.get("project"))]
    return AgentHandoffIntent(
        agent=agent,
        cwd=handoff_cwd(candidates, home, is_dir),
        prompt=triage_prompt(project.label, project.key),
        purpose="triage",
        label=project.label,
    )


def publish_handoff(
    agent: str, group: HarvestGroup | None, cwd: str, *, memory_registry: Path | None = None
) -> AgentHandoffIntent:
    prompt = publish_prompt(group.label if group is not None else None)
    if group and any(isinstance(item.origin, MemoryOrigin) for item in group.items):
        tokens = ", ".join(publish_tokens(group))
        prompt = f"Use the ringleader-publish skill in this checkout for these exact harvest tokens only: {tokens}."
    if memory_registry is not None:
        prompt += (
            f" Use --registry {shlex.quote(str(memory_registry.absolute()))} for this publication."
        )
    return AgentHandoffIntent(
        agent=agent,
        cwd=cwd,
        prompt=prompt,
        purpose="publish",
        label=group.label if group is not None else "all harvests",
    )


def memory_handoff(agent: str, ref: MemoryRef, cwd: str, registry_path: Path) -> AgentHandoffIntent:
    return AgentHandoffIntent(
        agent=agent,
        cwd=cwd,
        purpose="memory",
        label="native memory",
        prompt=(
            "Use the ringleader-memory-triage skill to review only the native memory entry "
            f"with ID {ref.memory_id}, fingerprint {ref.fingerprint} and review incarnation "
            f"{ref.incarnation}. Use --registry {shlex.quote(str(registry_path.absolute()))} "
            "for all memory commands. Ask for the user's decision. If the revision changed or "
            "the skill is unavailable, stop. The launch directory is not a publication destination."
        ),
    )
