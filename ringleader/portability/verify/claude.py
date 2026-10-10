from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import native
from ..capabilities import NativeRoots, NativeStage, TargetRequest
from ..claude import PROFILE, ClaudeAdapter, cli_version, read
from ..compare import compare
from ..model import Checkpoint, Conversation, Identity, Message, PreservationError, Text
from ..pi import PROFILE as PI_PROFILE
from ..pi import PiAdapter
from ..versions import parse_version
from .common import (
    PNG,
    HarnessResult,
    VerificationFailed,
    environment,
    isolated_roots,
    loopback,
    require,
    require_equal,
    require_outside_checkout,
    run_native,
)
from .fixtures import native_conversation

VARIANTS = ("success-image", "textual-failure", "failed-image-negative")
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}
EXPECTED_ROLES = ["user", "assistant", "user", "assistant", "user"]


def source_conversation(base: Path) -> Conversation:
    roots = isolated_roots(base)
    stage = PiAdapter().stage(
        native_conversation(),
        TargetRequest(Identity("pi", str(uuid4())), roots, PI_PROFILE, "Native portability smoke"),
    )
    return PiAdapter().read_stage(stage)


def checkpointed(original: Conversation, summary_text: str) -> Conversation:
    summary = Message(
        "summary",
        "user",
        (Text(summary_text),),
        original.history[0].provenance,
        timestamp=original.history[0].timestamp,
    )
    checkpoint = Checkpoint(
        "checkpoint",
        original.history[2].id,
        (summary,),
        tuple(item.id for item in original.history[1:3]),
    )
    return replace(
        original,
        checkpoints=(checkpoint,),
        context=native.linked([summary, *original.history[1:]]),
    )


def _variant_rows(stage: NativeStage, variant: str) -> bytes:
    rows = native.records(stage.files[0].data)
    if variant != "success-image":
        for row in rows:
            for block in row.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    block["is_error"] = True
                    if variant == "textual-failure":
                        block["content"] = block["content"][:1]
                        row["ringleaderAttachments"] = []
    return native.jsonl(rows)


def _refuses_failed_image(stage: NativeStage, data: bytes) -> None:
    try:
        ClaudeAdapter().read_stage(replace(stage, files=(replace(stage.files[0], data=data),)))
    except PreservationError as exc:
        require("strips failed tool-result images" in str(exc), "failed-image refusal reason")
    else:
        raise VerificationFailed("self-verification failed: failed-image source was accepted")


def _argv(executable: str, session_id: str, bare: bool = True) -> list[str]:
    return [
        executable,
        *(["--bare"] if bare else []),
        "--print",
        "--resume",
        session_id,
        "--model",
        "claude-sonnet-4-5",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--settings",
        '{"disableAllHooks":true}',
        "--setting-sources",
        "",
        "--no-chrome",
        "--disable-slash-commands",
        "OFFLINE_CAPTURE_ONLY",
    ]


def assert_request(messages: list[dict[str, Any]], compact: bool, variant: str) -> None:
    require_equal([item["role"] for item in messages], EXPECTED_ROLES, "request roles")
    first = messages[0]["content"]
    require_equal(
        first[0]["text"],
        "checkpoint summary" if compact else "Keep original history",
        "first user text",
    )
    if not compact:
        require("Historical user instruction" in first[1]["text"], "historical instruction")
        require_equal(first[2], IMAGE, "user image")
    call = messages[1]["content"][1]
    require_equal(
        (call["type"], call["name"], call["input"]),
        ("tool_use", "read", {"path": "example.txt"}),
        "tool call",
    )
    tool = messages[2]["content"][0]
    require_equal(tool["tool_use_id"], call["id"], "tool pairing")
    require_equal(tool["is_error"], variant != "success-image", "tool error flag")
    if variant == "success-image":
        require_equal(
            tool["content"], [{"type": "text", "text": "read failed"}, IMAGE], "tool image"
        )
    else:
        require(
            tool["content"] in ([{"type": "text", "text": "read failed"}], "read failed"),
            "failed tool result drops its image",
        )
    require_equal(messages[3]["content"][0]["text"], "Final answer", "final answer order")


def _request(
    executable: str,
    roots: NativeRoots,
    session_id: str,
    base: Path,
    label: str,
    bare: bool = True,
) -> list[dict[str, Any]]:
    base.mkdir(parents=True, exist_ok=True)
    error = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "offline capture complete"},
    }
    with loopback(error) as capture:
        extra = {
            "ANTHROPIC_BASE_URL": capture.url,
            "ANTHROPIC_API_KEY": "offline-not-a-credential",
            **({"CLAUDE_CODE_SIMPLE": "1"} if bare else {"DISABLE_AUTOUPDATER": "1"}),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
        }
        result = subprocess.run(
            _argv(executable, session_id, bare),
            cwd=roots.cwd,
            env=environment(roots, "claude-code", extra),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        captures = []
        while not capture.requests.empty():
            captures.append(capture.requests.get())
    (base / "stdout.txt").write_text(result.stdout)
    (base / "stderr.txt").write_text(result.stderr)
    bodies = [{"path": item[0], "body": json.loads(item[2])} for item in captures]
    (base / "requests.json").write_text(json.dumps(bodies, indent=2))
    require(
        result.returncode == 1 and "offline capture complete" in result.stdout,
        f"claude exits on the mock's HTTP 400 ({label}, exit {result.returncode})",
    )
    require_equal(len(bodies), 1, f"one model request ({label})")
    require(bodies[0]["path"].startswith("/v1/messages"), "request reaches /v1/messages")
    messages: list[dict[str, Any]] = bodies[0]["body"]["messages"]
    return messages


def capture_case(
    stage: NativeStage, compact: bool, executable: str, base: Path, variant: str
) -> None:
    roots = replace(isolated_roots(base), cwd=stage.roots.cwd)
    data = _variant_rows(stage, variant)
    if variant == "failed-image-negative":
        _refuses_failed_image(stage, data)
    path = roots.store / stage.source_ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    (base / "staged-original.jsonl").write_bytes(data)
    messages = _request(executable, roots, stage.identity.native_id, base, variant)
    assert_request(messages, compact, variant)
    require((base / "staged-original.jsonl").read_bytes() == data, "staged bytes untouched")


def _text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


def _call(key: str, path: str) -> list[dict[str, Any]]:
    return [{"type": "tool_use", "id": key, "name": "read", "input": {"path": path}}]


def _result(key: str, text: str) -> list[dict[str, Any]]:
    return [{"type": "tool_result", "tool_use_id": key, "content": _text(text), "is_error": False}]


def preserved_rows(
    session_id: str, cwd: Path, spec: dict[str, Row] | None = None
) -> list[dict[str, Any]]:
    spec = PRESERVED_ROWS if spec is None else spec
    keys = {name: str(uuid4()) for name in spec}
    rows = []
    for second, (name, (kind, parent, content, message_id, extra)) in enumerate(spec.items()):
        message: dict[str, Any] = {"role": kind, "content": content}
        if kind == "assistant":
            message.update(
                id=f"msg_{message_id}",
                type="message",
                model="claude-sonnet-4-5",
                stop_reason="tool_use" if content[0]["type"] == "tool_use" else "end_turn",
                stop_sequence=None,
                usage={"input_tokens": 0, "output_tokens": 0},
            )
        links = {
            key: keys.get(value, value)
            for key, value in extra.items()
            if key in ("sourceToolAssistantUUID", "logicalParentUuid")
        }
        rows.append(
            {
                "type": kind,
                "uuid": keys[name],
                "parentUuid": keys[parent] if parent else None,
                "sessionId": session_id,
                "cwd": str(cwd),
                "timestamp": f"2026-01-01T00:00:{second:02d}.000Z",
                "isSidechain": False,
                "userType": "external",
                "version": "2.1.294",
                **({"message": message} if kind in ("user", "assistant") else {}),
                **(
                    {
                        "attachment": content,
                        "entrypoint": "cli",
                        "gitBranch": "",
                        "session_id": session_id,
                        "slug": "harness",
                    }
                    if kind == "attachment"
                    else {}
                ),
                **{key: value for key, value in extra.items() if key not in links},
                **links,
            }
        )
    boundary = next(row for row in rows if row["type"] == "system")
    boundary["compactMetadata"] = {
        "trigger": "manual",
        "preTokens": 100,
        "postTokens": 10,
        "durationMs": 5,
        "preservedSegment": {
            "headUuid": keys["u2"],
            "anchorUuid": keys["summary"],
            "tailUuid": keys["a2"],
        },
        "preservedMessages": {
            "anchorUuid": keys["summary"],
            "uuids": [keys["u2"], keys["a2"]],
            "allUuids": [keys["u2"], keys["a2"]],
        },
    }
    return rows


Row = tuple[str, str | None, Any, str | None, dict[str, Any]]
PRESERVED_ROWS: dict[str, Row] = {
    "u1": ("user", None, _text("First request"), None, {}),
    "a1": ("assistant", "u1", _text("First answer"), "first", {}),
    "u2": ("user", "a1", _text("Second request"), None, {}),
    "a2": ("assistant", "u2", _text("Second answer"), "second", {}),
    "boundary": (
        "system",
        None,
        None,
        None,
        {
            "subtype": "compact_boundary",
            "content": "Conversation compacted",
            "logicalParentUuid": "a2",
        },
    ),
    "summary": (
        "user",
        "boundary",
        _text("Preserved segment summary"),
        None,
        {"isCompactSummary": True, "isVisibleInTranscriptOnly": True},
    ),
    "hidden": ("user", "summary", _text("Hidden harness context"), None, {"isMeta": True}),
    "u3": ("user", "hidden", _text("Third request"), None, {}),
    "c1": ("assistant", "u3", _call("toolu_parallel_one", "one.txt"), "parallel", {}),
    "c2": ("assistant", "c1", _call("toolu_parallel_two", "two.txt"), "parallel", {}),
    "r2": (
        "user",
        "c2",
        _result("toolu_parallel_two", "two"),
        None,
        {"sourceToolAssistantUUID": "c2"},
    ),
    "r1": (
        "user",
        "c1",
        _result("toolu_parallel_one", "one"),
        None,
        {"sourceToolAssistantUUID": "c1"},
    ),
    "a3": ("assistant", "r1", _text("Final answer"), "final", {}),
    "c3": ("assistant", "a3", _call("toolu_unresolved", "three.txt"), "trailing", {}),
}


def _model_view(messages: list[dict[str, Any]]) -> list[tuple[str, list[Any]]]:
    merged: list[tuple[str, list[Any]]] = []
    for message in messages:
        content = message["content"]
        blocks = [
            {
                key: _text(value) if key == "content" and isinstance(value, str) else value
                for key, value in block.items()
                if key != "cache_control"
            }
            for block in (_text(content) if isinstance(content, str) else content)
        ]
        if merged and merged[-1][0] == message["role"]:
            merged[-1][1].extend(blocks)
        else:
            merged.append((message["role"], blocks))
    return merged


def _texts(view: list[tuple[str, list[Any]]]) -> list[str]:
    return [
        (block.get("text") or block.get("tool_use_id") or block.get("id")).rstrip()
        for _, blocks in view
        for block in blocks
    ]


def preserved_case(executable: str, base: Path) -> None:
    roots = isolated_roots(base / "native")
    source = Identity("claude-code", str(uuid4()))
    rows = preserved_rows(source.native_id, roots.cwd)
    data = native.jsonl(rows)
    conversation = replace(read(data, source), title="Preserved")
    latest = conversation.checkpoints[-1]
    require_equal(
        [item.content for item in conversation.context[:3]],
        [item.content for item in (*latest.summary, *conversation.history[2:4])],
        "preserved segment follows its summary",
    )
    require(
        latest.following_context is not None and latest.following_context[0].hidden,
        "hidden post-compaction context",
    )
    adapter = ClaudeAdapter(executable)
    target = Identity("claude-code", str(uuid4()))
    stage = adapter.stage(conversation, TargetRequest(target, roots, PROFILE, "Preserved"))
    compare(conversation, adapter.read_stage(stage), adapter.normalizations(PROFILE))
    folder = roots.store / Path(stage.source_ref).parent
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{source.native_id}.jsonl").write_bytes(data)
    (roots.store / stage.source_ref).write_bytes(stage.files[0].data)
    original = _model_view(_request(executable, roots, source.native_id, base / "source", "native"))
    converted = _model_view(
        _request(executable, roots, target.native_id, base / "target", "staged")
    )
    require_equal(converted, original, "converted model request matches native resume")
    require_equal(
        [role for role, _ in original],
        [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
        ],
        "native roles",
    )
    require_equal(
        _texts(original)[:13],
        [
            "Preserved segment summary",
            "Second request",
            "Second answer",
            "Hidden harness context",
            "Third request",
            "toolu_parallel_one",
            "toolu_parallel_two",
            "toolu_parallel_two",
            "toolu_parallel_one",
            "Final answer",
            "toolu_unresolved",
            "toolu_unresolved",
            "No response requested.",
        ],
        "native preserved-segment, parallel-call and interrupted-call order",
    )


ATTACHMENT_ROWS: dict[str, Row] = {
    **{name: PRESERVED_ROWS[name] for name in ("u1", "a1", "u2", "a2", "boundary", "summary")},
    "memory": (
        "attachment",
        "summary",
        {
            "type": "nested_memory",
            "path": "/harness/CLAUDE.md",
            "displayPath": "CLAUDE.md",
            "content": {"path": "/harness/CLAUDE.md", "type": "Project", "content": "Template"},
        },
        None,
        {
            "rendered": [
                {"content": "<system-reminder>\nStored memory rendering\n</system-reminder>"}
            ]
        },
    ),
    "u3": ("user", "memory", _text("Third request"), None, {}),
    "a3": ("assistant", "u3", _text("Third answer"), "third", {}),
    "edit": (
        "attachment",
        "a3",
        {"type": "edited_text_file", "filename": "/harness/notes.txt", "snippet": "1\tchanged"},
        None,
        {},
    ),
    "u4": ("user", "edit", _text("Fourth request"), None, {}),
    "a4": ("assistant", "u4", _text("Fourth answer"), "fourth", {}),
}


NATIVE_EDITED_NOTE = (
    "<system-reminder>\nNote: /harness/notes.txt changed on disk since you last read it. "
    "That's usually deliberate, so take it as the current state rather than reverting it; if "
    "the change looks wrong, say so rather than undoing it yourself \u2014 otherwise no need "
    "to call it out. Here are the relevant changes (shown with line numbers):\n1\tchanged\n"
    "</system-reminder>"
)


def attachment_case(executable: str, base: Path) -> None:
    roots = isolated_roots(base / "native")
    source = Identity("claude-code", str(uuid4()))
    data = native.jsonl(preserved_rows(source.native_id, roots.cwd, ATTACHMENT_ROWS))
    conversation = replace(read(data, source), title="Attachments")
    adapter = ClaudeAdapter(executable)
    target = Identity("claude-code", str(uuid4()))
    stage = adapter.stage(conversation, TargetRequest(target, roots, PROFILE, "Attachments"))
    compare(conversation, adapter.read_stage(stage), adapter.normalizations(PROFILE))
    folder = roots.store / Path(stage.source_ref).parent
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{source.native_id}.jsonl").write_bytes(data)
    (roots.store / stage.source_ref).write_bytes(stage.files[0].data)
    original = _model_view(
        _request(executable, roots, source.native_id, base / "source", "native", bare=False)
    )
    converted = _model_view(
        _request(executable, roots, target.native_id, base / "target", "staged", bare=False)
    )
    require_equal(converted[:-1], original[:-1], "converted attachments match native resume")
    require_equal(
        [_texts([view])[-1] for view in (original[-1], converted[-1])],
        ["OFFLINE_CAPTURE_ONLY"] * 2,
        "fresh prompt ends both requests",
    )
    require_equal(
        _texts(original[:-1]),
        [
            "Preserved segment summary",
            "Second request",
            "Second answer",
            "<system-reminder>\nStored memory rendering\n</system-reminder>",
            "Third request",
            "Third answer",
            NATIVE_EDITED_NOTE,
            "Fourth request",
            "Fourth answer",
        ],
        "native resume sends stored and templated attachments",
    )


def native_case(original: Conversation, compact: bool, executable: str, base: Path) -> None:
    adapter = ClaudeAdapter(executable)
    if compact:
        original = checkpointed(original, "checkpoint summary")
    identity = Identity("claude-code", str(uuid4()))
    roots = isolated_roots(base / "stage")
    stage = adapter.stage(original, TargetRequest(identity, roots, PROFILE, original.title or ""))
    compare(original, adapter.read_stage(stage), adapter.normalizations(PROFILE))
    for variant in VARIANTS:
        capture_case(stage, compact, executable, base / variant, variant)
    launch = adapter.launch_request(identity, roots)
    require_equal(launch.argv, (executable, "--resume", identity.native_id), "launch argv")
    require_equal(launch.cwd, roots.cwd, "launch cwd")


def probe(executable: str, base: Path) -> str:
    roots = isolated_roots(base)
    output = run_native([executable, "--version"], roots, "claude-code")
    version = cli_version(output)
    require(parse_version(version) is not None, f"claude --version output {output!r}")
    require("--resume" in run_native([executable, "--help"], roots, "claude-code"), "--resume")
    return version


def run(executable: str, workdir: Path) -> HarnessResult:
    workdir = require_outside_checkout(workdir)
    version = probe(executable, workdir / "version")
    original = source_conversation(workdir / "source")
    for compact in (False, True):
        native_case(original, compact, executable, workdir / f"case-{int(compact)}")
    preserved_case(executable, workdir / "preserved")
    attachment_case(executable, workdir / "attachments")
    return HarnessResult(
        version,
        f"PASS Claude Code {version}: ten /v1/messages loopback captures (plain and checkpoint "
        "x success image, textual failure, failed-image negative; native preserved-segment "
        "compaction with hidden context and parallel calls vs its conversion; stored and "
        "templated attachments after compaction, non-bare, vs its conversion); ordered "
        "roles/text, instructions, tool names/arguments/pairing/error flags, image bytes; "
        "launch argv. No external inference.",
    )
