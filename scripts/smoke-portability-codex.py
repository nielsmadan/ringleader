from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ringleader.portability import native
from ringleader.portability.capabilities import NativeRoots, TargetRequest
from ringleader.portability.codex import PROFILE, CodexAdapter, read_rollout
from ringleader.portability.codex_state import inspect_database
from ringleader.portability.compare import compare
from ringleader.portability.model import HistoricalTimestamp, Identity, Text, ToolResult
from tests.portability.fixtures import (
    SESSION_ID,
    checkpoint_conversation,
    codex_mcp_records,
    codex_tool_records,
    conversation,
)
from tests.portability.test_codex import split_checkpoint


class Client:
    def __init__(self, executable, base, url=None):
        self.base = base
        self.roots = NativeRoots(
            base / "home", base / "store", base / "cwd", base / "config", base / "cache"
        )
        for directory in ("home", "store", "cwd", "config", "cache", "tmp", "data", "state"):
            (base / directory).mkdir(parents=True, exist_ok=True)
        config = "[analytics]\nenabled=false\n"
        if url:
            config += f'\n[model_providers.smoke]\nname="smoke"\nbase_url="{url}"\nwire_api="responses"\nrequires_openai_auth=false\nsupports_websockets=false\n'
        (base / "store" / "config.toml").write_text(config)
        env = {
            "PATH": os.environ["PATH"],
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
        self.stderr = (base / "stderr.log").open("ab")
        self.process = subprocess.Popen(
            [executable, "app-server"],
            cwd=base / "cwd",
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
        )
        self.queue = queue.Queue()
        self.counter = 0
        threading.Thread(target=self.receive, daemon=True).start()
        self.call(
            "initialize",
            {
                "clientInfo": {"name": "ringleader-smoke", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self.send({"method": "initialized"})

    def receive(self):
        for line in self.process.stdout:
            self.queue.put(json.loads(line))

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def call(self, method, params):
        self.counter += 1
        self.send({"id": self.counter, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = self.queue.get(timeout=max(0.01, deadline - time.monotonic()))
            if result.get("id") == self.counter:
                if "error" in result:
                    raise RuntimeError(result)
                return result["result"]
        raise TimeoutError(method)

    def close(self):
        self.process.terminate()
        self.process.wait(timeout=10)
        self.stderr.close()


def initialize(executable, base, url=None):
    client = Client(executable, base, url)
    try:
        client.call("thread/list", {"limit": 10})
        deadline = time.monotonic() + 10
        while True:
            try:
                inspect_database(client.roots)
                return client.roots
            except ValueError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
    finally:
        client.close()


def publish(original, roots):
    adapter = CodexAdapter()
    staged = adapter.stage(
        original, TargetRequest(Identity("codex", str(uuid4())), roots, PROFILE, original.title)
    )
    compare(original, adapter.read_stage(staged), adapter.normalizations(PROFILE))
    adapter.append_name(staged)
    for item in staged.files:
        path = roots.store / item.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.data)
    assert adapter.register_stage(staged)
    assert adapter.register_stage(staged, recover=True) is False
    return staged


def assert_visible(thread, title, identical=False):
    assert thread["name"] == title
    assert thread["source"] == "cli"
    assert thread["historyMode"] == "legacy"
    assert thread["sessionId"] == thread["id"]
    items = [item for turn in thread["turns"] for item in turn["items"]]
    assert [item["type"] for item in items] == [
        "userMessage",
        "userMessage",
        "agentMessage",
        "mcpToolCall",
        "mcpToolCall",
        "agentMessage",
    ]
    assert items[1]["content"][0]["text"] == "# AGENTS.md instructions: this is my literal input"
    assert items[2]["text"] == "Checking both."
    assert items[3]["tool"] == "read" and items[3]["arguments"] == {"file": "a"}
    assert items[3]["result"]["content"][0]["text"] == ("identical" if identical else "contents")
    assert items[4]["tool"] == "shell" and items[4]["arguments"] == "cat b"
    assert items[4]["status"] == "failed"
    assert items[4]["result"]["content"][0]["text"] == ("identical" if identical else "failed")
    if not identical:
        assert items[4]["result"]["content"][1]["data"]
    assert items[-1]["text"] == "One succeeded."
    assert all(turn["status"] == "completed" for turn in thread["turns"])


class Capture(BaseHTTPRequestHandler):
    requests = queue.Queue()

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.requests.put((dict(self.headers), body))
        response = {
            "error": {"message": "offline capture complete", "type": "invalid_request_error"}
        }
        data = json.dumps(response).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def assert_model(body, checkpoint, identical=False):
    items = body["input"]
    calls = [item for item in items if item.get("type") in ("function_call", "custom_tool_call")]
    assert [(call["name"], call.get("arguments", call.get("input"))) for call in calls] == [
        ("read", '{"file":"a"}'),
        ("shell", "cat b"),
    ]
    outputs = [
        item
        for item in items
        if item.get("type") in ("function_call_output", "custom_tool_call_output")
    ]
    assert outputs[0]["output"][0]["text"] == "[Historical tool result: succeeded]"
    assert outputs[0]["output"][1]["text"] == ("identical" if identical else "contents")
    assert outputs[1]["output"][0]["text"] == "[Historical tool result: failed]"
    assert outputs[1]["output"][1]["text"] == ("identical" if identical else "failed")
    if not identical:
        assert outputs[1]["output"][2]["image_url"].startswith("data:image/png;base64,")
    assert [item["call_id"] for item in outputs] == [item["call_id"] for item in calls]
    texts = [
        part["text"]
        for item in items
        if item.get("type") == "message"
        for part in item["content"]
        if part["type"] in ("input_text", "output_text")
    ]
    assert "Checking both." in texts and "One succeeded." in texts
    assert any("CURRENT_PROJECT_INSTRUCTIONS" in text for text in texts)
    if checkpoint:
        assert any("summarized" in text or text == "Summary" for text in texts)
    historical = [text for text in texts if "Historical user instruction" in text]
    if not checkpoint:
        assert len(historical) == 1
    assert body.get("instructions") is None or "Preserve my formatting." not in body["instructions"]


def verify_native_sources(executable, base):
    for kind in ("exec", "patch", "completion"):
        source_base = base / f"native-source-{kind}"
        roots = initialize(executable, source_base)
        rows = codex_tool_records(roots.cwd, kind, success=False)
        canonical = read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))
        adapter = CodexAdapter()
        staged = adapter.stage(
            canonical,
            TargetRequest(Identity("codex", str(uuid4())), roots, PROFILE, "Native source"),
        )
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
        client = Client(executable, source_base)
        try:
            response = client.call(
                "thread/read", {"threadId": staged.identity.native_id, "includeTurns": True}
            )
            (source_base / "native-read.json").write_text(json.dumps(response, indent=2))
            items = [item for turn in response["thread"]["turns"] for item in turn["items"]]
            expected = {
                "exec": ["userMessage", "agentMessage"],
                "patch": ["userMessage", "fileChange", "agentMessage"],
                "completion": ["userMessage", "functionCallOutput", "agentMessage"],
            }
            assert [item["type"] for item in items] == expected[kind]
            if kind == "patch":
                assert items[1]["status"] == "failed"
                assert {change["path"] for change in items[1]["changes"]} == {
                    "added.txt",
                    "deleted.txt",
                    "updated.txt",
                    "moved.txt",
                }
            elif kind == "completion":
                assert items[1]["output"] == "complete native visible output"
        finally:
            client.close()


def verify_distinct_mcp_streams(executable, base):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        mcp_base = base / "distinct-mcp"
        roots = initialize(executable, mcp_base, url)
        source = read_rollout(
            native.jsonl(codex_mcp_records(roots.cwd)), Identity("codex", SESSION_ID)
        )
        source = replace(source, title="Distinct MCP")
        staged = publish(source, roots)
        for method in ("thread/read", "thread/resume"):
            runtime_base = mcp_base
            if method == "thread/resume":
                runtime_base = mcp_base / "resume-copy"
                roots = initialize(executable, runtime_base, url)
                staged = publish(source, roots)
            client = Client(executable, runtime_base, url)
            try:
                params = {"threadId": staged.identity.native_id}
                if method == "thread/read":
                    params["includeTurns"] = True
                else:
                    params.update(modelProvider="smoke", model="gpt-5.4", cwd=str(roots.cwd))
                result = client.call(method, params)
                (mcp_base / (method.split("/")[1] + ".json")).write_text(
                    json.dumps(result, indent=2)
                )
                items = [item for turn in result["thread"]["turns"] for item in turn["items"]]
                assert [item["type"] for item in items] == [
                    "userMessage",
                    "mcpToolCall",
                    "agentMessage",
                ]
                assert items[1]["tool"] == "read"
                assert items[1]["arguments"] == {"path": "a"}
                assert items[1]["result"]["content"][0]["text"] == "complete native visible output"
                if method == "thread/read":
                    assert (roots.store / staged.source_ref).read_bytes() == staged.files[0].data
                    continue
                client.call(
                    "turn/start",
                    {
                        "threadId": staged.identity.native_id,
                        "input": [{"type": "text", "text": "CAPTURE_ONLY"}],
                    },
                )
                headers, raw = Capture.requests.get(timeout=30)
                if headers.get("content-encoding") == "zstd":
                    raw = subprocess.run(
                        ["zstd", "-d", "-c"], input=raw, capture_output=True, check=True
                    ).stdout
                body = json.loads(raw)
                (mcp_base / "model.json").write_text(json.dumps(body, indent=2))
                call = next(item for item in body["input"] if item.get("type") == "function_call")
                output = next(
                    item for item in body["input"] if item.get("type") == "function_call_output"
                )
                assert call["name"] == "mcp__files__read"
                assert call["arguments"] == '{"path":"a"}'
                assert output["call_id"] == call["call_id"]
                assert output["output"][1]["text"] == "model output truncated independently"
            finally:
                client.close()
    finally:
        server.shutdown()
        server.server_close()


def run(executable, base):
    verify_native_sources(executable, base)
    verify_distinct_mcp_streams(executable, base)
    roots = initialize(executable, base / "cold")
    originals = [conversation(), checkpoint_conversation(), split_checkpoint()]
    originals = [
        replace(
            c,
            attachments=(
                replace(
                    c.attachments[0],
                    data=base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                    ),
                ),
            ),
        )
        for c in originals
    ]
    plain = originals[0]
    history = tuple(
        replace(m, timestamp=HistoricalTimestamp("2020-01-02T03:04:05Z", m.provenance))
        for m in plain.history
    )
    originals[0] = replace(plain, history=history, context=history)
    identical_history = tuple(
        replace(
            m,
            content=tuple(
                replace(part, output=(Text("identical"),)) if isinstance(part, ToolResult) else part
                for part in m.content
            ),
        )
        for m in originals[0].history
    )
    originals.append(replace(originals[0], history=identical_history, context=identical_history))
    staged = [publish(original, roots) for original in originals]
    with sqlite3.connect(roots.store / "state_5.sqlite") as db:
        assert db.execute("SELECT status FROM backfill_state").fetchone() == ("complete",)
        assert db.execute("SELECT count(*) FROM threads").fetchone() == (len(originals),)
    for _ in range(2):
        client = Client(executable, base / "cold")
        try:
            listing = client.call(
                "thread/list", {"limit": 20, "sourceKinds": ["cli"], "cwd": str(roots.cwd)}
            )
            assert {item["id"] for item in listing["data"]} == {
                item.identity.native_id for item in staged
            }
            for index, item in enumerate(staged):
                response = client.call(
                    "thread/read", {"threadId": item.identity.native_id, "includeTurns": True}
                )
                assert_visible(response["thread"], "Portable example", index == len(staged) - 1)
                if index == 0:
                    assert response["thread"]["turns"][0]["startedAt"] == int(
                        datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC).timestamp()
                    )
                assert response["thread"]["createdAt"] > int(time.time()) - 60
                assert response["thread"]["recencyAt"] > int(time.time()) - 60
            wrong = client.call(
                "thread/list", {"cwd": str(base / "wrong-cwd"), "sourceKinds": ["cli"]}
            )
            assert wrong["data"] == []
        finally:
            client.close()
    for item in staged:
        assert (roots.store / item.source_ref).read_bytes() == item.files[0].data
    server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        for index, original in enumerate(originals):
            resume_base = base / f"resume-{index}"
            resume_roots = initialize(executable, resume_base, url)
            (resume_roots.cwd / "AGENTS.md").write_text(
                "CURRENT_PROJECT_INSTRUCTIONS: keep test fixtures intact.\n"
            )
            target = publish(original, resume_roots)
            client = Client(executable, resume_base, url)
            try:
                resumed = client.call(
                    "thread/resume",
                    {
                        "threadId": target.identity.native_id,
                        "modelProvider": "smoke",
                        "model": "gpt-5.4",
                        "cwd": str(resume_roots.cwd),
                    },
                )
                assert_visible(resumed["thread"], "Portable example", index == len(originals) - 1)
                client.call(
                    "turn/start",
                    {
                        "threadId": target.identity.native_id,
                        "input": [{"type": "text", "text": "CAPTURE_ONLY"}],
                    },
                )
                headers, raw = Capture.requests.get(timeout=30)
                if headers.get("content-encoding") == "zstd":
                    raw = subprocess.run(
                        ["zstd", "-d", "-c"], input=raw, capture_output=True, check=True
                    ).stdout
                body = json.loads(raw)
                (base / f"model-{index}.json").write_text(json.dumps(body, indent=2))
                assert_model(body, index in (1, 2), index == len(originals) - 1)
            finally:
                client.close()
    finally:
        server.shutdown()
        server.server_close()
    print(
        "PASS Codex 0.155.1: native exec/patch/completed-output source parser probes; initialized-home DB registration; cold list/read and restart; visible messages, tools, errors, image bytes, title, cwd/source filters; historical turns and fresh recency; native resume and five local model request captures including distinct MCP UI/model names and identical success/failure output; current project instructions. No external inference or tool execution."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.evidence:
        args.evidence.mkdir(parents=True, exist_ok=False)
        run(args.codex, args.evidence.resolve())
    else:
        with TemporaryDirectory(prefix="ringleader-codex-smoke-") as temporary:
            run(args.codex, Path(temporary).resolve())


if __name__ == "__main__":
    main()
