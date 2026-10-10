from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import native
from ..capabilities import NativeRoots, RunResult, TargetRequest
from ..compare import compare
from ..droid import (
    CANCELLED,
    LLM_SUMMARY,
    PROFILE,
    PROVIDER_SWITCH,
    DroidAdapter,
    local_owner,
    read,
    summary_text,
)
from ..launch import ProbeRunner
from ..model import (
    Conversation,
    Identity,
    Message,
    PreservationError,
    Provenance,
    Text,
    ToolResult,
)
from ..versions import parse_version
from .claude import IMAGE, checkpointed, source_conversation
from .common import (
    HarnessResult,
    VerificationFailed,
    environment,
    isolated_roots,
    loopback,
    require,
    require_equal,
    require_outside_checkout,
)

MODEL = "mock-model"
PROBE = "OFFLINE_CAPTURE_ONLY"
OWNER = "ringleader-self-verification"
HIDDEN = "Hidden harness context note"
SWITCH_BODY = "checkpoint summary with ``` fenced text"
REMINDER = "<system-reminder>"
FOREIGN = "Another agent's conversation summary"
SUCCESS: dict[str, Any] = {
    "is_error": False,
    "content": [{"type": "text", "text": "read failed"}, IMAGE],
}


def droid_roots(base: Path) -> NativeRoots:
    roots = isolated_roots(base)
    roots = replace(
        roots,
        store=roots.home / ".factory",
        data=roots.home / "data",
        state=roots.home / "state",
        temporary=roots.cache / "tmp",
    )
    (roots.store / "sessions").mkdir(parents=True, exist_ok=True)
    return roots


def seed_owner(roots: NativeRoots) -> str:
    seed = roots.store / "sessions" / "-seed" / f"{uuid4()}.jsonl"
    seed.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "type": "session_start",
        "id": seed.stem,
        "title": "Owner seed",
        "owner": OWNER,
        "version": 2,
        "cwd": "/seed",
    }
    seed.write_text(json.dumps(header) + "\n")
    owner = local_owner(roots.store / "sessions")
    require_equal(owner, OWNER, "owner taken from the newest local top-level session")
    return OWNER


def with_hidden(original: Conversation) -> Conversation:
    history = original.history
    hidden = Message(
        "hidden-note",
        "user",
        (Text(HIDDEN),),
        Provenance("harness", original.source, "hidden-note", "self-verification hidden context"),
        timestamp=history[-1].timestamp,
        hidden=True,
    )
    return replace(original, context=native.linked([*history[:-1], hidden, history[-1]]))


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _harness_reminder(message: dict[str, Any]) -> bool:
    content = message["content"]
    return (
        message["role"] == "user"
        and isinstance(content, list)
        and bool(content)
        and all(
            block.get("type") == "text" and block.get("text", "").startswith(REMINDER)
            for block in content
        )
    )


def _request(
    executable: str, roots: NativeRoots, session_id: str, base: Path
) -> tuple[list[dict[str, Any]], str]:
    error = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "offline capture complete"},
    }
    with loopback(error) as capture:
        settings = {
            "customModels": [
                {
                    "model": MODEL,
                    "displayName": "Mock",
                    "baseUrl": capture.url,
                    "apiKey": "dummy",
                    "provider": "anthropic",
                    "maxOutputTokens": 1024,
                }
            ]
        }
        (roots.store / "settings.json").write_text(json.dumps(settings))
        try:
            result = subprocess.run(
                [executable, "exec", "--fork", session_id, "-m", f"custom:{MODEL}", PROBE],
                cwd=roots.cwd,
                env=environment(roots, "droid", {"FACTORY_DISABLE_KEYRING": "1"}),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VerificationFailed(f"self-verification failed: droid exec: {exc}") from exc
        captures = []
        while not capture.requests.empty():
            captures.append(capture.requests.get())
    (base / "stdout.txt").write_bytes(result.stdout)
    (base / "stderr.txt").write_bytes(result.stderr)
    require(
        not native.started_auth(RunResult(result.returncode, result.stdout, result.stderr)),
        "droid exec never starts Factory's login flow",
    )
    bodies = [{"path": item[0], "body": json.loads(item[2])} for item in captures]
    (base / "requests.json").write_text(json.dumps(bodies, indent=2))
    require(result.returncode != 0, "droid exec stops on the mock's HTTP 400")
    require_equal(len(bodies), 1, "one model request")
    require(bodies[0]["path"].startswith("/v1/messages"), "request reaches /v1/messages")
    messages = [
        message
        for message in _clean(bodies[0]["body"]["messages"])
        if not _harness_reminder(message)
    ]
    log = roots.store / "logs" / "droid-log-single.log"
    return messages, log.read_text(errors="replace") if log.exists() else ""


def _text_of(message: dict[str, Any]) -> str:
    content = message["content"]
    return content if isinstance(content, str) else content[0]["text"]


@dataclass(frozen=True)
class Case:
    name: str
    conversation: Conversation
    summary: str | None = None
    tool: dict[str, Any] = field(default_factory=lambda: SUCCESS)
    drop_results: bool = False
    compactions: int = 0


def assert_request(messages: list[dict[str, Any]], case: Case) -> None:
    require_equal(_text_of(messages[-1]), PROBE, "probe prompt last")
    body = messages[:-1]
    if case.summary is None:
        require_equal(
            [item["role"] for item in body],
            ["user", "assistant", "user", "user", "assistant"],
            "request roles",
        )
        first = body[0]["content"]
        require_equal(first[0]["text"], "Keep original history", "first user text")
        require("Historical user instruction" in first[1]["text"], "historical instruction")
        require_equal(first[2], IMAGE, "user image")
        require_equal(_text_of(body[3]), HIDDEN, "llm_only hidden context replayed in place")
    else:
        require_equal(
            [item["role"] for item in body], ["user", "assistant", "user", "assistant"], "roles"
        )
        require_equal(
            body[0]["content"], [{"type": "text", "text": case.summary}], "summary wrapper"
        )
    call = body[1]["content"][1]
    require_equal(
        (call["type"], call["name"], call["input"]),
        ("tool_use", "read", {"path": "example.txt"}),
        "tool call",
    )
    tool = body[2]["content"][0]
    require_equal(
        (tool["type"], tool["tool_use_id"], tool.get("is_error", False), tool["content"]),
        ("tool_result", call["id"], case.tool["is_error"], case.tool["content"]),
        "tool result and error flag",
    )
    require_equal(_text_of(body[-1]), "Final answer", "final answer order")


def _without_results(data: bytes) -> bytes:
    return native.jsonl(
        [
            row
            for row in native.records(data)
            if not (
                row.get("type") == "message"
                and any(block.get("type") == "tool_result" for block in row["message"]["content"])
            )
        ]
    )


def _cancelled(conversation: Conversation) -> bool:
    return any(
        part == ToolResult(part.call_id, (Text(CANCELLED),), False)
        for message in conversation.context
        for part in message.content
        if isinstance(part, ToolResult)
    )


def native_case(case: Case, executable: str, base: Path) -> str:
    adapter = DroidAdapter(executable)
    roots = droid_roots(base)
    owner = seed_owner(roots)
    identity = Identity("droid", str(uuid4()))
    stage = adapter.stage(
        case.conversation,
        TargetRequest(identity, roots, PROFILE, case.conversation.title or "", (("owner", owner),)),
    )
    compare(case.conversation, adapter.read_stage(stage), adapter.normalizations(PROFILE))
    data = stage.files[0].data
    require_equal(
        sum(row.get("type") == "compaction_state" for row in native.records(data)),
        case.compactions,
        "native compaction_state rows",
    )
    if case.drop_results:
        data = _without_results(data)
        require(_cancelled(read(data, identity)), "reader cancels the unanswered call")
    path = roots.store / stage.source_ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    messages, log = _request(executable, roots, identity.native_id, base)
    assert_request(messages, case)
    require(path.read_bytes() == data, "staged droid bytes untouched")
    launch = adapter.launch_request(identity, roots)
    require_equal(launch.argv, (executable, "resume", identity.native_id), "launch argv")
    return log


def failed(original: Conversation) -> Conversation:
    history = tuple(
        replace(
            message,
            content=tuple(
                replace(part, output=(Text("read failed"),), is_error=True)
                if isinstance(part, ToolResult)
                else part
                for part in message.content
            ),
        )
        for message in original.history
    )
    return replace(original, history=history, context=history)


def cases(original: Conversation) -> tuple[Case, ...]:
    return (
        Case("plain", with_hidden(original)),
        Case(
            "error",
            with_hidden(failed(original)),
            tool={"is_error": True, "content": "read failed"},
        ),
        Case(
            "cancelled",
            with_hidden(original),
            tool={"is_error": False, "content": CANCELLED},
            drop_results=True,
        ),
        *(
            Case(
                kind,
                checkpointed(original, summary_text(kind, body)),
                summary_text(kind, body),
                compactions=1,
            )
            for kind, body in ((LLM_SUMMARY, "checkpoint summary"), (PROVIDER_SWITCH, SWITCH_BODY))
        ),
        Case("model-view", checkpointed(original, FOREIGN), FOREIGN),
    )


def run(executable: str, workdir: Path) -> HarnessResult:
    workdir = require_outside_checkout(workdir)
    try:
        probe = native.probe_version(executable, "droid", ProbeRunner())
    except PreservationError as exc:
        raise VerificationFailed(f"self-verification failed: {exc}") from exc
    version = probe.stdout.decode(errors="replace").strip()
    require(
        probe.returncode == 0 and parse_version(version) is not None,
        f"droid --version output {version!r}",
    )
    original = source_conversation(workdir / "source")
    logs = [
        native_case(case, executable, workdir / f"case-{case.name}") for case in cases(original)
    ]
    unauthorized = sum(log.count('"statusCode":401') for log in logs)
    return HarnessResult(
        version,
        f"PASS Droid {version}: droid exec --fork replays a written session into the captured "
        "/v1/messages request for plain, failed-tool, cancelled-call, llm_summary, "
        "provider-switch and foreign-summary (model view) sessions; roles, text, tool pairing, "
        "is_error true and droid's cancellation result, image bytes, llm_only hidden context and "
        "the native summary wrappers; owner lookup, launch argv. No login flow; tolerated "
        f"Factory calls logged {unauthorized} unauthorized responses. No external inference.",
    )
