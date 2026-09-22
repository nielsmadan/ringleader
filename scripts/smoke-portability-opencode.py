from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ringleader.portability import native
from ringleader.portability.capabilities import NativeRoots, TargetRequest
from ringleader.portability.compact import compact
from ringleader.portability.compare import compare
from ringleader.portability.model import (
    AttachmentRef,
    Identity,
    Instruction,
    Text,
    ToolCall,
    ToolOmission,
    ToolResult,
)
from ringleader.portability.opencode import PROFILE, OpenCodeAdapter, _payload_export, parse
from ringleader.portability.opencode_state import decode, encode
from tests.portability.test_opencode import ordinary_export, shared_conversation

CONTEXT_SCRIPT = r"""
globalThis.fetch = () => { throw new Error("Network disabled in native context smoke") }
const { filterCompacted, toModelMessages } = await import(process.argv[2])
const input = await Bun.file(process.argv[3]).json()
const model = { providerID: "anthropic", id: "offline-smoke", api: { npm: "@ai-sdk/anthropic", id: "offline-smoke" } }
const selected = filterCompacted([...input.messages].reverse())
const result = await toModelMessages(selected, model)
await Bun.write(process.argv[4], JSON.stringify(result, null, 2))
"""


def invoke(argv, cwd, env, evidence, label):
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=60, check=False)
    (evidence / f"{label}.stdout").write_bytes(result.stdout)
    (evidence / f"{label}.stderr").write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError(f"{label} failed ({result.returncode}); inspect {evidence}")
    return result.stdout


def isolated(base):
    roots = NativeRoots(
        base / "home", base / "data" / "opencode", base / "cwd", base / "config", base / "cache"
    )
    for path in (
        roots.home,
        roots.store,
        roots.cwd,
        roots.config,
        roots.cache,
        base / "tmp",
        base / "state",
    ):
        path.mkdir(parents=True)
    (roots.config / "opencode").mkdir()
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
        "PATH": os.environ["PATH"],
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


def media_bytes(value, mime):
    assert isinstance(value, str), "Native media must contain owned string bytes"
    if value.startswith("data:"):
        header, value = value.split(",", 1)
        assert header == f"data:{mime};base64", "Native media MIME/encoding mismatch"
    return base64.b64decode(value, validate=True)


def expected_context(conversation):
    assets = {item.id: item for item in conversation.attachments}
    calls = {}
    result = []
    for message in conversation.context:
        role = native.native_role(message)
        content = []
        for item in message.content:
            if isinstance(item, Text):
                content.append(("text", item.text))
            elif isinstance(item, Instruction | ToolOmission):
                content.append(("text", native.note(item)))
            elif isinstance(item, AttachmentRef):
                asset = assets[item.attachment_id]
                content.append(("media", asset.media_type, asset.data))
            elif isinstance(item, ToolCall):
                assert item.argument_format == "json"
                calls[item.call_id] = item.name
                content.append(("call", item.call_id, item.name, json.loads(item.arguments)))
            elif isinstance(item, ToolResult):
                role = "tool"
                output = []
                has_media = any(isinstance(part, AttachmentRef) for part in item.output)
                for part in item.output:
                    if isinstance(part, Text):
                        if part.text or not has_media:
                            output.append(("text", part.text))
                    else:
                        asset = assets[part.attachment_id]
                        output.append(("media", asset.media_type, asset.data))
                content.append(("result", item.call_id, calls[item.call_id], item.is_error, output))
            else:
                raise TypeError(f"Unsupported expected context content: {item!r}")
        result.append((role, content))
    return result


def model_output(value):
    assert isinstance(value, dict)
    assert set(value) == {"type", "value"}, value
    if value["type"] in ("text", "error-text"):
        assert isinstance(value["value"], str)
        return value["type"] == "error-text", [("text", value["value"])]
    assert value["type"] == "content", value
    output = []
    for part in value["value"]:
        if part.get("type") == "text":
            assert set(part) == {"type", "text"}
            output.append(("text", part["text"]))
        else:
            assert set(part) == {"type", "mediaType", "data"} and part["type"] == "media", part
            output.append(
                ("media", part["mediaType"], media_bytes(part["data"], part["mediaType"]))
            )
    return False, output


def actual_context(messages):
    result = []
    for message in messages:
        assert set(message) == {"role", "content"}, message
        assert message["role"] in ("user", "assistant", "tool"), message
        parts = message["content"]
        if isinstance(parts, str):
            parts = [{"type": "text", "text": parts}]
        content = []
        for part in parts:
            kind = part["type"]
            if kind == "text":
                assert set(part) == {"type", "text"}, part
                content.append(("text", part["text"]))
            elif kind in ("image", "file"):
                key = "image" if kind == "image" else "data"
                assert set(part) <= {"type", key, "mediaType", "filename"}, part
                content.append(
                    ("media", part["mediaType"], media_bytes(part[key], part["mediaType"]))
                )
            elif kind == "tool-call":
                assert set(part) == {"type", "toolCallId", "toolName", "input"}, part
                content.append(("call", part["toolCallId"], part["toolName"], part["input"]))
            elif kind == "tool-result":
                assert set(part) == {"type", "toolCallId", "toolName", "output"}, part
                error, output = model_output(part["output"])
                content.append(("result", part["toolCallId"], part["toolName"], error, output))
            else:
                raise AssertionError(f"Unsupported native model context content: {part!r}")
        result.append((message["role"], content))
    return result


def assert_context(messages, expected):
    actual = actual_context(messages)
    wanted = expected_context(expected)
    assert actual == wanted, {"expected": wanted, "actual": actual}


def context_cases(roots):
    full = shared_conversation()
    history = []
    for message in full.history:
        content = tuple(
            replace(item, output=(*item.output, AttachmentRef("image")))
            if isinstance(item, ToolResult) and not item.is_error
            else item
            for item in message.content
        )
        history.append(replace(message, content=content))
    full = replace(full, history=tuple(history), context=tuple(history))
    checkpoint = ordinary_export(roots, compaction=True)
    checkpoint["messages"][3]["parts"][0]["tail_start_id"] = "msg_3"
    return {
        "full": full,
        "compact": compact(full).conversation,
        "native-compaction": parse(checkpoint, Identity("opencode", "ses_native")),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--native-source", required=True, type=Path)
    parser.add_argument("--bun", default="bun")
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    executable = str(args.executable.resolve())
    source = args.native_source.resolve()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    with TemporaryDirectory(prefix="ringleader-opencode-smoke-") as temporary:
        base = Path(temporary)
        roots, env = isolated(base)
        version = (
            invoke([executable, "--version"], roots.cwd, env, evidence, "version").decode().strip()
        )
        assert version == "1.18.30", f"Expected exact OpenCode 1.18.30, got {version!r}"
        revision = (
            invoke(
                ["git", "-C", str(source), "describe", "--tags", "--exact-match", "HEAD"],
                roots.cwd,
                env,
                evidence,
                "source-version",
            )
            .decode()
            .strip()
        )
        assert revision == "v1.18.30", revision
        dirty = invoke(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"],
            roots.cwd,
            env,
            evidence,
            "source-status",
        )
        assert not dirty.strip(), "Native source checkout has tracked modifications"
        invoke(
            [executable, "session", "list", "--format", "json"],
            roots.cwd,
            env,
            evidence,
            "initialize",
        )
        adapter = OpenCodeAdapter(executable)
        adapter.inspect_target(roots, None).require()
        source_file = base / "context.ts"
        source_file.write_text(CONTEXT_SCRIPT)
        cases = context_cases(roots)
        for label, value in cases.items():
            result = adapter.stage(
                value, TargetRequest(adapter.allocate_identity(), roots, PROFILE, value.title)
            )
            graph = _payload_export(decode(result.database_payload))
            imported = base / (label + ".json")
            imported.write_bytes(encode(graph))
            frozen = imported.read_bytes()
            invoke(
                [executable, "import", str(imported)], roots.cwd, env, evidence, label + "-import"
            )
            exported = invoke(
                [executable, "export", result.identity.native_id],
                roots.cwd,
                env,
                evidence,
                label + "-export",
            )
            native_export = json.loads(exported)
            compare(value, parse(native_export, result.identity), adapter.normalizations(PROFILE))
            listing = json.loads(
                invoke(
                    [executable, "session", "list", "--format", "json"],
                    roots.cwd,
                    env,
                    evidence,
                    label + "-list",
                )
            )
            assert any(row["id"] == result.identity.native_id for row in listing)
            context_input = base / (label + "-export.json")
            context_input.write_bytes(exported)
            context_output = evidence / (label + "-context.json")
            invoke(
                [
                    args.bun,
                    "--tsconfig-override",
                    str(source / "packages/opencode/tsconfig.json"),
                    str(source_file),
                    str(source / "packages/opencode/src/session/message-v2.ts"),
                    str(context_input),
                    str(context_output),
                ],
                roots.cwd,
                env,
                evidence,
                label + "-context",
            )
            assert_context(json.loads(context_output.read_text()), value)
            assert imported.read_bytes() == frozen
            published = adapter.stage(
                value, TargetRequest(adapter.allocate_identity(), roots, PROFILE, value.title)
            )
            assert adapter.register_stage(published)
            registered = json.loads(
                invoke(
                    [executable, "export", published.identity.native_id],
                    roots.cwd,
                    env,
                    evidence,
                    label + "-registered-export",
                )
            )
            compare(value, parse(registered, published.identity), adapter.normalizations(PROFILE))
        shutil.copy2(roots.store / "opencode.db", evidence / "isolated-native.db")
    print(
        "Native import/export/list, insert-only registration and source context builder passed. No provider request was made."
    )


if __name__ == "__main__":
    main()
