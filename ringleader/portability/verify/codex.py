from __future__ import annotations

import base64
import json
import os
import queue
import sqlite3
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from uuid import uuid4

from .. import codex_state, native
from ..capabilities import NativeRoots, NativeStage, TargetRequest
from ..codex import PROFILE, CodexAdapter, cli_version, read_rollout
from ..compare import compare
from ..model import (
    AttachmentRef,
    Content,
    Conversation,
    HistoricalTimestamp,
    Identity,
    Instruction,
    PreservationError,
    Text,
    ToolCall,
    ToolOmission,
    ToolResult,
)
from ..verification import HARNESS_VERSION, VerificationRecord
from ..versions import parse_version
from .common import (
    REPR_LIMIT,
    Capture,
    HarnessResult,
    VerificationFailed,
    isolated_roots,
    loopback,
    require,
    require_equal,
    require_outside_checkout,
    run_native,
)
from .fixtures import (
    CODEX_BASE_INSTRUCTIONS,
    CODEX_COMPACTION_CONTENT,
    SESSION_ID,
    checkpoint_conversation,
    codex_compacted_records,
    codex_mcp_records,
    codex_paginated_records,
    codex_tool_records,
    conversation,
    split_checkpoint,
)

PIXEL = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
ERROR = {"error": {"message": "offline capture complete", "type": "invalid_request_error"}}
BOOTSTRAP_ITEMS = 3
RPC_TIMEOUT = 60


class Client:
    def __init__(self, executable: str, base: Path, url: str | None = None) -> None:
        self.roots = NativeRoots(
            base / "home", base / "store", base / "cwd", base / "config", base / "cache"
        )
        for directory in ("home", "store", "cwd", "config", "cache", "tmp", "data", "state"):
            (base / directory).mkdir(parents=True, exist_ok=True)
        config = "[analytics]\nenabled=false\n"
        if url:
            config += (
                f'\n[model_providers.smoke]\nname="smoke"\nbase_url="{url}"\nwire_api="responses"\n'
                "requires_openai_auth=false\nsupports_websockets=false\n"
            )
        (base / "store" / "config.toml").write_text(config)
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(base / "home"),
            "CODEX_HOME": str(base / "store"),
            "XDG_CONFIG_HOME": str(base / "config"),
            "XDG_CACHE_HOME": str(base / "cache"),
            "XDG_DATA_HOME": str(base / "data"),
            "XDG_STATE_HOME": str(base / "state"),
            "TMPDIR": str(base / "tmp"),
            "OTEL_SDK_DISABLED": "true",
            "RUST_LOG": "codex_rollout=trace",
        }
        self.stderr_path = base / "stderr.log"
        self.stderr = self.stderr_path.open("ab")
        self.process = subprocess.Popen(
            [executable, "app-server"],
            cwd=base / "cwd",
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
        )
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.counter = 0
        self.reader = threading.Thread(target=self._receive, daemon=True)
        self.reader.start()
        try:
            self.call(
                "initialize",
                {
                    "clientInfo": {"name": "ringleader-verify", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"method": "initialized"})
        except Exception:
            self._terminate()
            raise

    def _pipe(self, stream: IO[str] | None) -> IO[str]:
        if stream is None:
            raise VerificationFailed("self-verification failed: codex app-server pipe is closed")
        return stream

    def _receive(self) -> None:
        for line in self._pipe(self.process.stdout):
            self.queue.put(json.loads(line))

    def _send(self, value: dict[str, Any]) -> None:
        stdin = self._pipe(self.process.stdin)
        stdin.write(json.dumps(value) + "\n")
        stdin.flush()

    def call(self, method: str, params: dict[str, Any]) -> Any:
        self.counter += 1
        self._send({"id": self.counter, "method": method, "params": params})
        deadline = time.monotonic() + RPC_TIMEOUT
        while time.monotonic() < deadline:
            try:
                result = self.queue.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if result.get("id") == self.counter:
                if "error" in result:
                    raise VerificationFailed(
                        f"self-verification failed: codex {method}: {result['error']}"
                    )
                return result["result"]
        raise VerificationFailed(
            f"self-verification failed: codex {method} timed out "
            f"(exit={self.process.poll()}, stderr tail: {self._stderr_tail()!r})"
        )

    def _stderr_tail(self, limit: int = REPR_LIMIT) -> str:
        try:
            data = self.stderr_path.read_bytes()
        except OSError:
            return ""
        return data[-limit:].decode(errors="replace").strip()

    def _terminate(self) -> None:
        with suppress(OSError):
            self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                self.process.kill()
            with suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=10)
        self.reader.join(timeout=10)
        for stream in (self.process.stdin, self.process.stdout, self.stderr):
            with suppress(OSError):
                if stream is not None:
                    stream.close()

    def close(self) -> None:
        self._terminate()


@contextmanager
def client(executable: str, base: Path, url: str | None = None) -> Iterator[Client]:
    session = Client(executable, base, url)
    try:
        yield session
    finally:
        session.close()


def initialize(executable: str, base: Path, url: str | None = None) -> NativeRoots:
    with client(executable, base, url) as session:
        session.call("thread/list", {"limit": 10})
        deadline = time.monotonic() + 10
        while True:
            try:
                codex_state.inspect_database(session.roots, None)
                return session.roots
            except PreservationError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)


@dataclass(frozen=True)
class Candidate:
    adapter: CodexAdapter
    runtime_version: str


def candidate(executable: str, version: str, schema: str) -> Candidate:
    record = VerificationRecord(
        "codex",
        version,
        HARNESS_VERSION["codex"],
        "model-request-capture",
        datetime.now(UTC).isoformat(timespec="seconds"),
        schema,
    )
    return Candidate(CodexAdapter(executable, (record,)), f"codex-cli {version}")


def stage(target: Candidate, original: Conversation, roots: NativeRoots) -> NativeStage:
    return target.adapter.stage(
        original,
        TargetRequest(
            Identity("codex", str(uuid4())),
            roots,
            PROFILE,
            original.title or "",
            runtime_version=target.runtime_version,
        ),
    )


def publish(target: Candidate, original: Conversation, roots: NativeRoots) -> NativeStage:
    adapter = target.adapter
    staged = stage(target, original, roots)
    compare(original, adapter.read_stage(staged), staged.normalizations or ())
    adapter.append_name(staged)
    for item in staged.files:
        path = roots.store / item.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.data)
    require(adapter.register_stage(staged), "codex thread registration")
    require(adapter.register_stage(staged, recover=True) is False, "registration recovery")
    return staged


def _text(part: Content) -> str:
    if isinstance(part, Text):
        return part.text
    if isinstance(part, Instruction | ToolOmission):
        return native.note(part)
    raise VerificationFailed(f"self-verification failed: unexpected canonical prose {part!r}")


def _expected_blocks(
    parts: tuple[Content, ...], assets: dict[str, Any]
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        ("image", assets[part.attachment_id].media_type, assets[part.attachment_id].data)
        if isinstance(part, AttachmentRef)
        else ("text", _text(part))
        for part in parts
    )


def _image_url(url: str) -> tuple[str, str, bytes]:
    require(url.startswith("data:"), "embedded image data URL")
    mime, encoded = url[5:].split(";base64,", 1)
    return ("image", mime, base64.b64decode(encoded, validate=True))


def _actual_blocks(parts: list[dict[str, Any]]) -> tuple[tuple[Any, ...], ...]:
    result: list[tuple[Any, ...]] = []
    for part in parts:
        kind = part["type"]
        if kind in ("text", "input_text", "output_text"):
            result.append(("text", part["text"]))
        elif kind == "input_image":
            result.append(_image_url(part["image_url"]))
        elif kind == "image" and "url" in part:
            result.append(_image_url(part["url"]))
        elif kind == "image" and "data" in part:
            result.append(
                ("image", part["mimeType"], base64.b64decode(part["data"], validate=True))
            )
        else:
            raise VerificationFailed(f"self-verification failed: unknown native content {kind}")
    return tuple(result)


def _expected_model(original: Conversation) -> list[tuple[Any, ...]]:
    assets = {a.id: a for a in original.attachments}
    result: list[tuple[Any, ...]] = []
    calls = {
        part.call_id: part
        for message in original.context
        for part in message.content
        if isinstance(part, ToolCall)
    }
    for message in original.context:
        prose: list[Content] = []
        role = "user" if message.role in ("system", "developer") else message.role
        for part in (*message.content, None):
            if part is not None and not isinstance(part, ToolCall | ToolResult):
                prose.append(part)
                continue
            if prose:
                result.append(("message", role, _expected_blocks(tuple(prose), assets)))
                prose = []
            if isinstance(part, ToolCall):
                args = (
                    json.loads(part.arguments) if part.argument_format == "json" else part.arguments
                )
                result.append(("call", part.argument_format, part.call_id, part.name, args))
            elif isinstance(part, ToolResult):
                status = "failed" if part.is_error else "succeeded"
                output = (
                    ("text", f"[Historical tool result: {status}]"),
                    *_expected_blocks(part.output, assets),
                )
                result.append(("result", calls[part.call_id].argument_format, part.call_id, output))
    return result


def _actual_model(items: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    actual: list[tuple[Any, ...]] = []
    for item in items:
        kind = item["type"]
        if kind == "message":
            actual.append((kind, item["role"], _actual_blocks(item["content"])))
        elif kind in ("function_call", "custom_tool_call"):
            form = "json" if kind == "function_call" else "freeform"
            args = json.loads(item["arguments"]) if form == "json" else item["input"]
            actual.append(("call", form, item["call_id"], item["name"], args))
        elif kind in ("function_call_output", "custom_tool_call_output"):
            form = "json" if kind == "function_call_output" else "freeform"
            actual.append(("result", form, item["call_id"], _actual_blocks(item["output"])))
        else:
            raise VerificationFailed(f"self-verification failed: unexpected model item {kind}")
    return actual


def assert_model(
    body: dict[str, Any], original: Conversation, cwd: Path, *, project_instructions: bool = True
) -> None:
    items = body["input"]
    expected = _expected_model(original)
    require_equal(len(items), len(expected) + BOOTSTRAP_ITEMS, "model request item count")
    require_equal(_actual_model(items[:-BOOTSTRAP_ITEMS]), expected, "historical model prefix")
    bootstrap, environment, prompt = items[-BOOTSTRAP_ITEMS:]
    require_equal(
        [item["role"] for item in items[-BOOTSTRAP_ITEMS:]],
        ["developer", "user", "user"],
        "fresh request suffix roles",
    )
    require(
        all(item["type"] == "message" and item.get("id") for item in items[-BOOTSTRAP_ITEMS:]),
        "fresh request suffix messages",
    )
    skill_block, permission_block = bootstrap["content"]
    require(
        skill_block["type"] == permission_block["type"] == "input_text", "bootstrap block types"
    )
    require(skill_block["text"].startswith("<skills_instructions>"), "skills bootstrap")
    require(permission_block["text"].startswith("<permissions instructions>"), "permissions")
    if project_instructions:
        require_equal(
            environment["content"][0],
            {
                "type": "input_text",
                "text": f"# AGENTS.md instructions for {cwd}\n\n<INSTRUCTIONS>\n"
                "CURRENT_PROJECT_INSTRUCTIONS: keep test fixtures intact.\n\n</INSTRUCTIONS>",
            },
            "destination AGENTS.md instructions",
        )
    require_equal(len(environment["content"]), 1 + int(project_instructions), "environment blocks")
    require(
        environment["content"][-1]["text"].startswith("<environment_context>\n"),
        "environment context",
    )
    require(f"<cwd>{cwd}</cwd>" in environment["content"][-1]["text"], "destination cwd")
    require_equal(
        prompt["content"], [{"type": "input_text", "text": "CAPTURE_ONLY"}], "probe prompt"
    )
    instructions = body.get("instructions") or ""
    require(
        not any(
            isinstance(part, Instruction) and part.text in instructions
            for message in original.context
            for part in message.content
        ),
        "historical instructions stay out of system instructions",
    )


def _expected_visible(original: Conversation) -> list[tuple[Any, ...]]:
    assets = {a.id: a for a in original.attachments}
    results = {
        p.call_id: p for m in original.history for p in m.content if isinstance(p, ToolResult)
    }
    expected: list[tuple[Any, ...]] = []
    for message in original.history:
        for part in message.content:
            if isinstance(part, ToolCall):
                output = results[part.call_id]
                arguments = (
                    json.loads(part.arguments) if part.argument_format == "json" else part.arguments
                )
                expected.append(
                    (
                        "tool",
                        part.call_id,
                        part.name,
                        arguments,
                        output.is_error,
                        _expected_blocks(output.output, assets),
                    )
                )
            elif isinstance(part, ToolResult):
                continue
            elif isinstance(part, AttachmentRef):
                kind, blocks = expected[-1]
                require(kind == "user", "visible image follows user text")
                expected[-1] = (kind, (*blocks, *_expected_blocks((part,), assets)))
            elif message.role == "assistant":
                expected.append(("assistant", _text(part)))
            else:
                expected.append(("user", (("text", _text(part)),)))
    return expected


def _actual_visible(thread: dict[str, Any]) -> list[tuple[Any, ...]]:
    actual: list[tuple[Any, ...]] = []
    for turn in thread["turns"]:
        require_equal(turn["status"], "completed", "visible turn status")
        for item in turn["items"]:
            kind = item["type"]
            if kind == "userMessage":
                actual.append(("user", _actual_blocks(item["content"])))
            elif kind == "agentMessage":
                actual.append(("assistant", item["text"]))
            elif kind == "mcpToolCall":
                require(item["status"] in ("completed", "failed"), "visible tool status")
                actual.append(
                    (
                        "tool",
                        item["id"],
                        item["tool"],
                        item["arguments"],
                        item["status"] == "failed",
                        _actual_blocks(item["result"]["content"]),
                    )
                )
            else:
                raise VerificationFailed(f"self-verification failed: unknown visible item {kind}")
    return actual


def assert_visible(thread: dict[str, Any], original: Conversation) -> None:
    require_equal(thread["name"], original.title, "visible thread name")
    require_equal(
        (thread["source"], thread["historyMode"]), ("cli", "legacy"), "thread source and mode"
    )
    require_equal(thread["sessionId"], thread["id"], "root thread identity")
    require_equal(_actual_visible(thread), _expected_visible(original), "visible history")


def _model_body(capture: Capture) -> dict[str, Any]:
    try:
        _path, headers, raw = capture.requests.get(timeout=RPC_TIMEOUT)
    except queue.Empty as exc:
        raise VerificationFailed("self-verification failed: no codex model request") from exc
    if {key.lower(): value for key, value in headers.items()}.get("content-encoding") == "zstd":
        raw = subprocess.run(
            ["zstd", "-d", "-c"], input=raw, capture_output=True, check=True
        ).stdout
    body: dict[str, Any] = json.loads(raw)
    return body


def verify_native_sources(executable: str, base: Path, target: Candidate) -> None:
    adapter = target.adapter
    expected = {
        "exec": ["userMessage", "agentMessage"],
        "patch": ["userMessage", "fileChange", "agentMessage"],
        "completion": ["userMessage", "functionCallOutput", "agentMessage"],
    }
    for kind, item_types in expected.items():
        source_base = base / f"native-source-{kind}"
        roots = initialize(executable, source_base)
        rows = codex_tool_records(roots.cwd, kind, success=False)
        canonical = read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))
        staged = stage(target, replace(canonical, title="Native source"), roots)
        rows[0] = native.records(staged.files[0].data)[0]
        rows[0]["payload"].pop("ringleader")
        for row in rows:
            if row["payload"].get("type") == "item_completed":
                row["payload"]["thread_id"] = staged.identity.native_id
        staged = replace(staged, files=(replace(staged.files[0], data=native.jsonl(rows)),))
        adapter.append_index(staged)
        path = roots.store / staged.source_ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(staged.files[0].data)
        adapter.register_stage(staged)
        with client(executable, source_base) as session:
            response = session.call(
                "thread/read", {"threadId": staged.identity.native_id, "includeTurns": True}
            )
        (source_base / "native-read.json").write_text(json.dumps(response, indent=2))
        items = [item for turn in response["thread"]["turns"] for item in turn["items"]]
        require_equal([item["type"] for item in items], item_types, f"native {kind} items")
        if kind == "patch":
            require_equal(items[1]["status"], "failed", "patch status")
            require_equal(
                {change["path"] for change in items[1]["changes"]},
                {"added.txt", "deleted.txt", "updated.txt", "moved.txt"},
                "patch changes",
            )
        elif kind == "completion":
            require_equal(items[1]["output"], "complete native visible output", "output")


def verify_distinct_mcp_streams(executable: str, base: Path, target: Candidate) -> None:
    with loopback(ERROR) as capture:
        mcp_base = base / "distinct-mcp"
        roots = initialize(executable, mcp_base, capture.url)
        source = read_rollout(
            native.jsonl(codex_mcp_records(roots.cwd)), Identity("codex", SESSION_ID)
        )
        source = replace(source, title="Distinct MCP")
        staged = publish(target, source, roots)
        with client(executable, mcp_base, capture.url) as session:
            result = session.call(
                "thread/read", {"threadId": staged.identity.native_id, "includeTurns": True}
            )
        (mcp_base / "read.json").write_text(json.dumps(result, indent=2))
        assert_visible(result["thread"], source)
        require(
            (roots.store / staged.source_ref).read_bytes() == staged.files[0].data,
            "cold read leaves rollout bytes untouched",
        )
        resume_base = mcp_base / "resume-copy"
        roots = initialize(executable, resume_base, capture.url)
        staged = publish(target, source, roots)
        with client(executable, resume_base, capture.url) as session:
            result = session.call(
                "thread/resume",
                {
                    "threadId": staged.identity.native_id,
                    "modelProvider": "smoke",
                    "model": "gpt-5.4",
                    "cwd": str(roots.cwd),
                },
            )
            (mcp_base / "resume.json").write_text(json.dumps(result, indent=2))
            assert_visible(result["thread"], source)
            session.call(
                "turn/start",
                {
                    "threadId": staged.identity.native_id,
                    "input": [{"type": "text", "text": "CAPTURE_ONLY"}],
                },
            )
            body = _model_body(capture)
        (mcp_base / "model.json").write_text(json.dumps(body, indent=2))
        assert_model(body, source, roots.cwd, project_instructions=False)


def verify_paginated_source(executable: str, base: Path, target: Candidate) -> None:
    with loopback(ERROR) as capture:
        source_base = base / "paginated-source"
        roots = initialize(executable, source_base, capture.url)
        source = read_rollout(
            native.jsonl(codex_paginated_records(roots.cwd)), Identity("codex", SESSION_ID)
        )
        published = publish(target, replace(source, title="Paginated source"), roots)
        with client(executable, source_base, capture.url) as session:
            resumed = session.call(
                "thread/resume",
                {
                    "threadId": published.identity.native_id,
                    "modelProvider": "smoke",
                    "model": "gpt-5.4",
                    "cwd": str(roots.cwd),
                },
            )
            session.call(
                "turn/start",
                {
                    "threadId": published.identity.native_id,
                    "input": [{"type": "text", "text": "CAPTURE_ONLY"}],
                },
            )
            body = _model_body(capture)
        (source_base / "model.json").write_text(json.dumps(body, indent=2))
    items = [item for turn in resumed["thread"]["turns"] for item in turn["items"]]
    require_equal(
        [item["type"] for item in items], ["userMessage", "agentMessage"], "paginated visible"
    )
    require_equal(body.get("instructions"), CODEX_BASE_INSTRUCTIONS, "carried base instructions")
    history = body["input"][:-BOOTSTRAP_ITEMS]
    require_equal(
        [(item["type"], item.get("role"), item.get("phase")) for item in history],
        [
            ("message", "user", None),
            ("function_call", None, None),
            ("function_call_output", None, None),
            ("custom_tool_call", None, None),
            ("custom_tool_call_output", None, None),
            ("message", "assistant", "final_answer"),
        ],
        "paginated model history keeps phase and drops bootstrap",
    )
    namespaced_call = history[1]
    require_equal(
        (namespaced_call.get("name"), namespaced_call.get("namespace")),
        ("collaboration.spawn_agent", None),
        "resumed paginated namespaced call is sent flattened, with no native namespace field",
    )


def verify_encrypted_compaction(executable: str, base: Path, target: Candidate) -> None:
    with loopback(ERROR) as capture:
        source_base = base / "encrypted-compaction"
        roots = initialize(executable, source_base, capture.url)
        source = read_rollout(
            native.jsonl(codex_compacted_records(roots.cwd)), Identity("codex", SESSION_ID)
        )
        published = publish(target, replace(source, title="Encrypted compaction"), roots)
        with client(executable, source_base, capture.url) as session:
            session.call(
                "thread/resume",
                {
                    "threadId": published.identity.native_id,
                    "modelProvider": "smoke",
                    "model": "gpt-5.4",
                    "cwd": str(roots.cwd),
                },
            )
            session.call(
                "turn/start",
                {
                    "threadId": published.identity.native_id,
                    "input": [{"type": "text", "text": "CAPTURE_ONLY"}],
                },
            )
            body = _model_body(capture)
        (source_base / "model.json").write_text(json.dumps(body, indent=2))
    history = body["input"][:-BOOTSTRAP_ITEMS]
    require_equal(
        [
            (item["type"], item.get("role"), item.get("encrypted_content"), _plain(item))
            for item in history
        ],
        [
            ("message", "user", None, "Before compaction?"),
            ("compaction", None, CODEX_COMPACTION_CONTENT, None),
            ("message", "user", None, "After compaction?"),
            ("message", "assistant", None, "Later answer."),
        ],
        "resumed request replays the retained message, then the encrypted compaction verbatim",
    )


def _plain(item: dict[str, Any]) -> str | None:
    content = item.get("content")
    return "".join(part["text"] for part in content) if isinstance(content, list) else None


def originals() -> list[Conversation]:
    pixel = base64.b64decode(PIXEL)
    cases = [
        replace(c, attachments=(replace(c.attachments[0], data=pixel),))
        for c in (conversation(), checkpoint_conversation(), split_checkpoint())
    ]
    plain = cases[0]
    history = tuple(
        replace(m, timestamp=HistoricalTimestamp("2020-01-02T03:04:05Z", m.provenance))
        for m in plain.history
    )
    cases[0] = replace(plain, history=history, context=history)
    identical = tuple(
        replace(
            m,
            content=tuple(
                replace(part, output=(Text("identical"),)) if isinstance(part, ToolResult) else part
                for part in m.content
            ),
        )
        for m in cases[0].history
    )
    return [*cases, replace(cases[0], history=identical, context=identical)]


def verify_cold_reads(
    executable: str, base: Path, target: Candidate, cases: list[Conversation]
) -> None:
    roots = initialize(executable, base / "cold")
    staged = [publish(target, original, roots) for original in cases]
    with closing(sqlite3.connect(roots.store / "state_5.sqlite")) as db:
        require_equal(
            db.execute("SELECT status FROM backfill_state").fetchone(), ("complete",), "backfill"
        )
        require_equal(
            db.execute("SELECT count(*) FROM threads").fetchone(), (len(cases),), "thread rows"
        )
    for _ in range(2):
        with client(executable, base / "cold") as session:
            listing = session.call(
                "thread/list", {"limit": 20, "sourceKinds": ["cli"], "cwd": str(roots.cwd)}
            )
            require_equal(
                {item["id"] for item in listing["data"]},
                {item.identity.native_id for item in staged},
                "thread listing",
            )
            for index, item in enumerate(staged):
                response = session.call(
                    "thread/read", {"threadId": item.identity.native_id, "includeTurns": True}
                )
                (base / f"visible-{index}.json").write_text(
                    json.dumps(response["thread"], indent=2)
                )
                assert_visible(response["thread"], cases[index])
                if index == 0:
                    require_equal(
                        response["thread"]["turns"][0]["startedAt"],
                        int(datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()),
                        "historical turn start",
                    )
                fresh = int(time.time()) - 60
                require(response["thread"]["createdAt"] > fresh, "fresh creation time")
                require(response["thread"]["recencyAt"] > fresh, "fresh recency time")
            wrong = session.call(
                "thread/list", {"cwd": str(base / "wrong-cwd"), "sourceKinds": ["cli"]}
            )
            require_equal(wrong["data"], [], "cwd filter")
    for item in staged:
        require(
            (roots.store / item.source_ref).read_bytes() == item.files[0].data,
            "cold reads leave rollout bytes untouched",
        )


def verify_resumes(
    executable: str, base: Path, target: Candidate, cases: list[Conversation]
) -> None:
    with loopback(ERROR) as capture:
        for index, original in enumerate(cases):
            resume_base = base / f"resume-{index}"
            roots = initialize(executable, resume_base, capture.url)
            (roots.cwd / "AGENTS.md").write_text(
                "CURRENT_PROJECT_INSTRUCTIONS: keep test fixtures intact.\n"
            )
            published = publish(target, original, roots)
            with client(executable, resume_base, capture.url) as session:
                resumed = session.call(
                    "thread/resume",
                    {
                        "threadId": published.identity.native_id,
                        "modelProvider": "smoke",
                        "model": "gpt-5.4",
                        "cwd": str(roots.cwd),
                    },
                )
                assert_visible(resumed["thread"], original)
                session.call(
                    "turn/start",
                    {
                        "threadId": published.identity.native_id,
                        "input": [{"type": "text", "text": "CAPTURE_ONLY"}],
                    },
                )
                body = _model_body(capture)
            (base / f"model-{index}.json").write_text(json.dumps(body, indent=2))
            assert_model(body, original, roots.cwd)


def probe(executable: str, base: Path) -> tuple[str, str]:
    output = run_native([executable, "--version"], isolated_roots(base / "version"), "codex")
    version = cli_version(output)
    require(parse_version(version) is not None, f"codex --version output {output!r}")
    roots = initialize(executable, base / "schema")
    return version, codex_state.inspect_database(roots, None)


def run(executable: str, workdir: Path) -> HarnessResult:
    workdir = require_outside_checkout(workdir)
    version, schema = probe(executable, workdir / "probe")
    target = candidate(executable, version, schema)
    verify_native_sources(executable, workdir, target)
    verify_distinct_mcp_streams(executable, workdir, target)
    verify_paginated_source(executable, workdir, target)
    verify_encrypted_compaction(executable, workdir, target)
    cases = originals()
    verify_cold_reads(executable, workdir, target, cases)
    verify_resumes(executable, workdir, target, cases)
    return HarnessResult(
        version,
        f"PASS Codex {version}: native exec/patch/completed-output parser probes; "
        "initialized-home DB registration; cold list/read and restart; visible messages, tools, "
        "errors, image bytes, title, cwd/source filters; historical turns and fresh recency; "
        "native resume and six loopback model request captures including distinct MCP UI/model "
        "names, identical success/failure output, and a paginated source's carried base "
        "instructions and message phase, and an encrypted compaction replayed verbatim after its "
        "retained messages; destination project instructions. "
        "No external inference or tool execution.",
        schema,
    )
