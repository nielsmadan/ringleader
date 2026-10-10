from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import native
from .. import opencode_state as state
from ..capabilities import NativeRoots, RunRequest, RunResult, TargetRequest
from ..compact import compact
from ..compare import compare
from ..model import (
    AttachmentRef,
    Conversation,
    HistoricalTimestamp,
    Message,
    Text,
    ToolResult,
)
from ..opencode import PROFILE, OpenCodeAdapter, parse
from ..verification import HARNESS_VERSION, VerificationRecord
from ..versions import parse_version
from .common import (
    REPR_LIMIT,
    HarnessResult,
    VerificationFailed,
    require,
    require_outside_checkout,
)
from .fixtures import opencode_conversation


class NoRunner:
    def run(self, request: RunRequest) -> RunResult:
        raise VerificationFailed("self-verification failed: OpenCode inspection ran a command")


def isolated(base: Path) -> tuple[NativeRoots, dict[str, str]]:
    roots = NativeRoots(
        base / "home", base / "data" / "opencode", base / "cwd", base / "config", base / "cache"
    )
    for path in (roots.home, roots.store, roots.cwd, roots.cache, base / "tmp", base / "state"):
        path.mkdir(parents=True, exist_ok=True)
    (roots.config / "opencode").mkdir(parents=True, exist_ok=True)
    config = roots.config / "opencode" / "opencode.json"
    config.write_text(
        json.dumps(
            {
                "plugin": [],
                "mcp": {},
                "autoupdate": False,
                "share": "disabled",
                "model": "anthropic/claude-sonnet-4-5",
            }
        )
    )
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "en_US.UTF-8",
        "HOME": str(roots.home),
        "XDG_DATA_HOME": str(roots.store.parent),
        "XDG_CONFIG_HOME": str(roots.config),
        "XDG_CACHE_HOME": str(roots.cache),
        "XDG_STATE_HOME": str(base / "state"),
        "TMPDIR": str(base / "tmp"),
        "OPENCODE_DB": str(roots.store / "opencode.db"),
        "OPENCODE_CONFIG": str(config),
        "OPENCODE_CONFIG_DIR": str(roots.config),
        "OPENCODE_PURE": "true",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_MODELS_FETCH": "true",
        "OPENCODE_DISABLE_FFF": "true",
        "OTEL_SDK_DISABLED": "true",
    }
    return roots, env


def invoke(argv: list[str], roots: NativeRoots, env: dict[str, str], label: str) -> bytes:
    evidence = roots.store.parent.parent
    try:
        result = subprocess.run(
            argv, cwd=roots.cwd, env=env, capture_output=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationFailed(f"self-verification failed: opencode {label}: {exc}") from exc
    (evidence / f"{label}.stdout").write_bytes(result.stdout)
    (evidence / f"{label}.stderr").write_bytes(result.stderr)
    if result.returncode:
        detail = (result.stdout + result.stderr).decode(errors="replace").strip()
        raise VerificationFailed(
            f"self-verification failed: opencode {label} exited {result.returncode}: "
            f"{detail[-REPR_LIMIT:]}"
        )
    return result.stdout


def cases() -> dict[str, Conversation]:
    full = opencode_conversation()
    history = tuple(
        replace(
            message,
            content=tuple(
                replace(item, output=(*item.output, AttachmentRef("image")))
                if isinstance(item, ToolResult) and not item.is_error
                else item
                for item in message.content
            ),
        )
        for message in full.history
    )
    full = replace(full, history=history, context=history)
    return {
        "full": full,
        "compact": compact(full).conversation,
        "cross-agent": cross_agent(full),
    }


def cross_agent(base: Conversation) -> Conversation:
    def at(message: Message, value: str) -> Message:
        return replace(message, timestamp=HistoricalTimestamp(value, message.provenance))

    instructions, request, calls, results, answer = base.history
    first, second = (
        replace(results, id=f"result-{item.call_id}", content=(item,))
        for item in results.content
        if isinstance(item, ToolResult)
    )
    first = replace(
        first,
        content=tuple(
            replace(item, output=(Text("contents"), Text("more contents"), *item.output[1:]))
            for item in first.content
            if isinstance(item, ToolResult)
        ),
    )
    history = native.linked(
        [
            replace(message, parent_id=None)
            for message in (
                at(instructions, "2026-09-21T12:00:02.000Z"),
                at(request, "2026-09-21T12:00:01.000Z"),
                calls,
                second,
                first,
                answer,
            )
        ]
    )
    return replace(base, history=history, context=history, branch_id=history[-1].id)


def candidate(executable: str, version: str, schema: str) -> OpenCodeAdapter:
    record = VerificationRecord(
        "opencode",
        version,
        HARNESS_VERSION["opencode"],
        "cli-acceptance",
        datetime.now(UTC).isoformat(timespec="seconds"),
        schema,
    )
    return OpenCodeAdapter(executable, (record,))


def accept_case(
    adapter: OpenCodeAdapter, fresh: Path, base: Path, label: str, value: Conversation
) -> None:
    executable = adapter.executable
    roots, env = isolated(base)
    for path in fresh.parent.glob(fresh.name + "*"):
        shutil.copy2(path, roots.store / path.name)
    compatibility = adapter.inspect_target(roots, NoRunner())
    profile = compatibility.require()
    published = adapter.stage(
        value,
        TargetRequest(
            adapter.allocate_identity(),
            roots,
            profile,
            value.title or "",
            compatibility.target_configuration,
        ),
    )
    require(adapter.register_stage(published), f"OpenCode {label} registration")
    session_id = published.identity.native_id
    exported: dict[str, Any] = json.loads(
        invoke([executable, "export", session_id], roots, env, f"{label}-export")
    )
    compare(value, parse(exported, published.identity), adapter.normalizations(PROFILE))
    listing = json.loads(
        invoke([executable, "session", "list", "--format", "json"], roots, env, f"{label}-list")
    )
    require(
        any(row.get("id") == session_id for row in listing),
        f"OpenCode session list shows the {label} session",
    )


def run(executable: str, workdir: Path) -> HarnessResult:
    workdir = require_outside_checkout(workdir)
    roots, env = isolated(workdir / "fresh")
    version = invoke([executable, "--version"], roots, env, "version").decode().strip()
    require(parse_version(version) is not None, f"opencode --version output {version!r}")
    invoke([executable, "session", "list", "--format", "json"], roots, env, "initialize")
    fresh = roots.store / "opencode.db"
    require(fresh.is_file(), "OpenCode initialised its database")
    schema = state.inspect_database(roots, None)
    adapter = candidate(executable, version, schema)
    for label, value in cases().items():
        accept_case(adapter, fresh, workdir / label, label, value)
    return HarnessResult(
        version,
        f"PASS OpenCode {version} (CLI acceptance): insert-only registration into a copy of a "
        "freshly initialised database; opencode export reads back full, compacted and "
        "cross-agent sessions (out-of-order split results, multi-block tool output, decreasing "
        "source times) with tools, errors and images; session list shows them. No provider "
        "request.",
        schema,
    )
