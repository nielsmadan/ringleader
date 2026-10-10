from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from ringleader.portability import diagnostics, launch, native, native_resume
from ringleader.portability.adapters import builtin_adapters
from ringleader.portability.capabilities import NativeRoots, RunRequest, RunResult, TargetRequest
from ringleader.portability.compare import compare
from ringleader.portability.droid import (
    ANSWERED_LATER,
    CANCELLED,
    LLM_SUMMARY,
    PROFILE,
    PROVIDER_SWITCH,
    DroidAdapter,
    parse_summary,
    read,
    summary_text,
)
from ringleader.portability.model import (
    AttachmentRef,
    Checkpoint,
    Identity,
    Message,
    PreservationError,
    Provenance,
    Text,
    ToolCall,
    ToolResult,
)
from ringleader.portability.verification import HARNESS_VERSION, VerificationRecord
from ringleader.portability.verify import droid as harness
from ringleader.portability.verify.common import PNG, VerificationFailed
from ringleader.portability.verify.fixtures import native_conversation
from ringleader.slug import droid_slug

SESSION = "6d0b2f7c-2a51-4c61-9f43-5b1f0d3c9a10"
IDENTITY = Identity("droid", SESSION)
STAMP = "2026-01-02T03:04:05.000Z"


def header(**extra):
    return {
        "type": "session_start",
        "id": SESSION,
        "title": "Synthetic",
        "owner": "someone",
        "version": 2,
        "cwd": "/work",
        **extra,
    }


def message(key, role, content, **message_fields):
    return {
        "type": "message",
        "id": key,
        "timestamp": STAMP,
        "message": {"role": role, "content": content, **message_fields},
    }


def text(value):
    return [{"type": "text", "text": value}]


def call(key, path="a.txt"):
    return {"type": "tool_use", "id": key, "name": "Read", "input": {"path": path}}


def result(key, content="out"):
    return {"type": "tool_result", "tool_use_id": key, "content": content, "is_error": False}


def compaction(key, kind=LLM_SUMMARY, anchor=None, **extra):
    row = {
        "type": "compaction_state",
        "id": key,
        "timestamp": STAMP,
        "summaryText": "the summary",
        "summaryTokens": 3,
        "summaryKind": kind,
        "removedCount": 2,
        "systemInfo": {},
        "systemInfoText": "SYSTEM INFO BOOTSTRAP",
        **extra,
    }
    if anchor:
        row["anchorMessage"] = {"id": anchor, "index": 1}
    return row


def parse(*rows, head=None):
    data = "".join(json.dumps(row) + "\n" for row in (head or header(), *rows)).encode()
    return read(data, IDENTITY)


def texts(messages):
    return [
        (item.role, [part.text for part in item.content if isinstance(part, Text)])
        for item in messages
    ]


def test_header_must_be_a_version_2_top_level_session():
    with pytest.raises(PreservationError, match="version 2 session_start"):
        parse(message("u", "user", text("hi")), head=header(version=1))
    with pytest.raises(PreservationError, match="subagent"):
        parse(message("u", "user", text("hi")), head=header(callingSessionId="parent"))


def test_ui_only_records_bootstrap_and_provider_fields_are_dropped():
    conversation = parse(
        message(
            "context-1",
            "user",
            text("<system-reminder>catalog</system-reminder>"),
            visibility="llm_only",
        ),
        message("u", "user", text("question")),
        message("hook", "user", [], visibility="user_only", hookEventName="PreToolUse"),
        {"type": "todo_state", "id": "t", "timestamp": STAMP, "todos": [], "messageIndex": 0},
        message(
            "a",
            "assistant",
            [{"type": "thinking", "thinking": "hm", "signature": "s"}, *text("answer")],
            openaiEncryptedContent="opaque",
            modelId="model",
        ),
        message("byok", "user", text("BYOK Error: 401"), visibility="user_only"),
        {"type": "agent_turn_outcome", "turnId": "u", "reason": "completed", "resultKind": "text"},
    )
    assert texts(conversation.history) == [("user", ["question"]), ("assistant", ["answer"])]
    assert texts(conversation.context) == texts(conversation.history)


def test_llm_only_messages_are_hidden_harness_context():
    conversation = parse(
        message("u", "user", text("question")),
        message(
            "task-completion:1", "user", text("Background task completed."), visibility="llm_only"
        ),
        message("a", "assistant", text("answer")),
    )
    assert texts(conversation.history) == [("user", ["question"]), ("assistant", ["answer"])]
    hidden = conversation.context[1]
    assert (hidden.hidden, hidden.provenance.origin, hidden.content) == (
        True,
        "harness",
        (Text("Background task completed."),),
    )


def test_droid_reminders_are_dropped_as_bootstrap_and_interrupt_markers_are_carried():
    reminder = "__internal__system-reminder__0b9c6a52-63b5-4b5e-9d64-1c2f4c1b7e10"
    conversation = parse(
        message("u", "user", text("question")),
        message(
            reminder,
            "user",
            text("<system-reminder>skills</system-reminder>"),
            visibility="llm_only",
        ),
        message(
            "todo-stale-1",
            "user",
            text("<system-reminder>todo</system-reminder>"),
            visibility="llm_only",
        ),
        message(
            "todo-suggest-1",
            "user",
            text("<system-reminder>todo</system-reminder>"),
            visibility="llm_only",
        ),
        message("a", "assistant", text("answer")),
        message(
            "5d0c1e2a-0000-4000-8000-000000000001",
            "user",
            text("Request interrupted by user"),
            visibility="llm_only",
        ),
    )
    assert texts(conversation.context) == [
        ("user", ["question"]),
        ("assistant", ["answer"]),
        ("user", ["Request interrupted by user"]),
    ]


def test_tool_results_split_per_result_and_carry_images():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}
    conversation = parse(
        message("u", "user", text("go")),
        message("a", "assistant", [call("one"), call("two")]),
        message("r", "user", [result("one"), result("two", [image, *text("seen")])]),
        message("f", "assistant", text("done")),
    )
    first, second = conversation.history[2:4]
    assert (first.id, first.role, first.content) == (
        "r:0",
        "tool",
        (ToolResult("one", (Text("out"),)),),
    )
    assert second.content == (ToolResult("two", (AttachmentRef("asset-0"), Text("seen"))),)
    assert conversation.attachments[0].media_type == "image/png"


def test_unanswered_calls_get_droids_cancellation_result():
    conversation = parse(
        message("u", "user", text("go")),
        message("a", "assistant", [call("one"), call("two")]),
        message("r", "user", [result("one")]),
        message("late", "user", [result("two")], visibility="user_only"),
        message("next", "user", text("again")),
    )
    synthetic = conversation.history[3]
    assert synthetic.content == (ToolResult("two", (Text(CANCELLED),), False),)
    assert synthetic.provenance.origin == "harness"
    assert texts(conversation.history[4:]) == [("user", ["again"])]


def test_a_result_answered_after_the_cancellation_is_refused():
    with pytest.raises(PreservationError, match=ANSWERED_LATER):
        parse(
            message("u", "user", text("go")),
            message("a", "assistant", [call("one")]),
            message("next", "user", text("again")),
            message("r", "user", [result("one")]),
        )


def test_llm_summary_compaction_becomes_a_checkpoint():
    conversation = parse(
        message("u1", "user", text("old")),
        message("a1", "assistant", text("old answer")),
        message("u2", "user", text("kept")),
        message("a2", "assistant", text("kept answer")),
        compaction("c", anchor="a1"),
        message("u3", "user", text("after")),
    )
    [checkpoint] = conversation.checkpoints
    assert (checkpoint.after_message_id, checkpoint.retained_message_ids) == ("a2", ("u2", "a2"))
    assert checkpoint.summary[0].content == (Text(summary_text(LLM_SUMMARY, "the summary")),)
    assert texts(conversation.context) == [
        ("user", [summary_text(LLM_SUMMARY, "the summary")]),
        ("user", ["kept"]),
        ("assistant", ["kept answer"]),
        ("user", ["after"]),
    ]


def test_provider_switch_compaction_uses_its_fenced_wrapper():
    body = "has ``` inside"
    conversation = parse(
        message("u1", "user", text("old")),
        message("a1", "assistant", text("old answer")),
        compaction("c", kind=PROVIDER_SWITCH, anchor="a1", summaryText=body),
    )
    wrapper = conversation.context[0].content[0].text
    assert "````text\nhas ``` inside\n````" in wrapper
    assert parse_summary(wrapper) == (PROVIDER_SWITCH, body)


def test_a_compaction_before_any_message_is_hidden_leading_context():
    conversation = parse(compaction("c"), message("u", "user", text("first")))
    assert texts(conversation.history) == [("user", ["first"])]
    assert [item.hidden for item in conversation.context] == [True, False]
    assert conversation.context[0].content == (Text(summary_text(LLM_SUMMARY, "the summary")),)


def test_hidden_messages_after_the_latest_compaction_are_following_context():
    conversation = parse(
        message("u1", "user", text("old")),
        message("a1", "assistant", text("old answer")),
        compaction("c", anchor="a1"),
        message("note", "user", text("reminder"), visibility="llm_only"),
        message("u2", "user", text("after")),
    )
    following = conversation.checkpoints[0].following_context
    assert following is not None
    assert [item.hidden for item in following] == [True, False]


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (compaction("c", kind="mystery", anchor="u"), "Unsupported Droid summaryKind: mystery"),
        (compaction("c", anchor="missing"), "anchor is missing"),
        ({"type": "brand_new", "id": "x"}, "Unsupported Droid record type: brand_new"),
        (message("x", "user", text("hi"), newField=1), "Unsupported Droid message field: newField"),
        (message("x", "user", text("hi"), visibility="team"), "visibility: team"),
        (message("x", "user", [{"type": "document"}]), "Unsupported Droid content block: document"),
    ],
)
def test_unrecognised_shapes_fail_closed(row, reason):
    with pytest.raises(PreservationError, match=reason):
        parse(message("u", "user", text("hi")), message("a", "assistant", text("ok")), row)


def test_droid_slug_matches_droids_own_encoder():
    assert droid_slug("/tmp_dir/a.b/c d/") == "-tmp_dir-a.b-c d"


def roots(tmp_path: Path) -> NativeRoots:
    home = tmp_path / "home"
    cwd = tmp_path / "work_dir"
    cwd.mkdir(parents=True)
    return NativeRoots(
        home, home / ".factory", cwd.resolve(), tmp_path / "config", tmp_path / "cache"
    )


def target(tmp_path, conversation, configuration=(("owner", "someone"),)):
    return TargetRequest(
        Identity("droid", str(uuid4())), roots(tmp_path), PROFILE, "Imported", configuration
    )


def hidden_conversation():
    original = native_conversation()
    note = Message(
        "note",
        "user",
        (Text("hidden reminder"),),
        Provenance("harness", original.source, "note", "synthetic hidden context"),
        timestamp=original.history[-1].timestamp,
        hidden=True,
    )
    history = original.history
    return replace(
        original,
        title="Imported",
        context=native.linked([*history[:-1], note, history[-1]]),
    )


def test_writer_round_trips_history_hidden_context_and_attachments(tmp_path):
    adapter = DroidAdapter()
    original = hidden_conversation()
    request = target(tmp_path, original)
    stage = adapter.stage(original, request)
    compare(original, adapter.read_stage(stage), adapter.normalizations(PROFILE))
    assert stage.source_ref == (
        f"sessions/{droid_slug(request.roots.cwd)}/{request.identity.native_id}.jsonl"
    )
    rows = native.records(stage.files[0].data)
    assert {key: rows[0][key] for key in ("type", "owner", "version", "title")} == {
        "type": "session_start",
        "owner": "someone",
        "version": 2,
        "title": "Imported",
    }
    assert [row["message"].get("visibility") for row in rows[1:]] == [
        None,
        None,
        None,
        "llm_only",
        None,
    ]
    assert adapter.stage_metadata(stage).cwd == str(request.roots.cwd)


def test_writer_emits_checkpoints_as_compaction_state(tmp_path):
    adapter = DroidAdapter()
    original = native_conversation()
    summary = Message(
        "summary",
        "user",
        (Text(summary_text(LLM_SUMMARY, "short")),),
        original.history[0].provenance,
        timestamp=original.history[0].timestamp,
    )
    retained = tuple(item.id for item in original.history[1:3])
    checkpoint = Checkpoint("cp", original.history[2].id, (summary,), retained)
    compacted = replace(
        original,
        title="Imported",
        checkpoints=(checkpoint,),
        context=native.linked([summary, *original.history[1:]]),
    )
    stage = adapter.stage(compacted, target(tmp_path, compacted))
    compare(compacted, adapter.read_stage(stage), adapter.normalizations(PROFILE))
    rows = native.records(stage.files[0].data)
    [state] = [row for row in rows if row["type"] == "compaction_state"]
    assert (state["summaryText"], state["summaryKind"], state["anchorMessage"]["id"]) == (
        "short",
        LLM_SUMMARY,
        rows[1]["id"],
    )


def test_writer_refuses_a_missing_owner(tmp_path):
    original = replace(native_conversation(), title="Imported")
    with pytest.raises(PreservationError, match="owner is unknown"):
        DroidAdapter().stage(original, target(tmp_path, original, ()))


def test_writer_writes_a_foreign_summary_as_an_ordinary_message(tmp_path):
    original = replace(native_conversation(), title="Imported")
    summary = replace(original.history[0], id="summary", content=(Text("Another agent's summary"),))
    checkpoint = Checkpoint("cp", original.history[3].id, (summary,))
    foreign = replace(original, checkpoints=(checkpoint,), context=(summary,))
    adapter = DroidAdapter()
    stage = adapter.stage(foreign, target(tmp_path, foreign))
    rows = native.records(stage.files[0].data)
    assert [(row["type"], row["message"]["content"]) for row in rows[1:]] == [
        ("message", text("Another agent's summary"))
    ]
    compare(foreign, adapter.read_stage(stage), adapter.normalizations(PROFILE))


def test_writer_refuses_an_id_already_in_the_store(tmp_path):
    original = replace(native_conversation(), title="Imported")
    request = target(tmp_path, original)
    taken = request.roots.store / "sessions" / "-elsewhere" / f"{request.identity.native_id}.jsonl"
    taken.parent.mkdir(parents=True)
    taken.write_text("")
    with pytest.raises(PreservationError, match="already exists in the native store"):
        DroidAdapter().stage(original, request)


def test_writer_chains_messages_with_millisecond_utc_timestamps(tmp_path):
    original = replace(native_conversation(), title="Imported")
    rows = native.records(DroidAdapter().stage(original, target(tmp_path, original)).files[0].data)
    messages = rows[1:]
    assert [row.get("parentId") for row in messages] == [
        None,
        *(row["id"] for row in messages[:-1]),
    ]
    assert {row["timestamp"] for row in messages} == {"2020-01-02T03:04:05.000Z"}


def test_writer_emits_a_provider_switch_summary_with_its_kind(tmp_path):
    original = native_conversation()
    body = "switch body with ``` fence"
    summary = Message(
        "summary",
        "user",
        (Text(summary_text(PROVIDER_SWITCH, body)),),
        original.history[0].provenance,
        timestamp=original.history[0].timestamp,
    )
    checkpoint = Checkpoint("cp", original.history[3].id, (summary,), (original.history[3].id,))
    compacted = replace(
        original,
        title="Imported",
        checkpoints=(checkpoint,),
        context=native.linked([summary, original.history[3]]),
    )
    adapter = DroidAdapter()
    stage = adapter.stage(compacted, target(tmp_path, compacted))
    [state] = [row for row in native.records(stage.files[0].data) if row["type"] != "message"][1:]
    assert (state["summaryKind"], state["summaryText"], state["removedCount"]) == (
        PROVIDER_SWITCH,
        body,
        3,
    )
    compare(compacted, adapter.read_stage(stage), adapter.normalizations(PROFILE))


@pytest.mark.parametrize("removed", [0, 2])
def test_an_anchorless_compaction_after_messages_is_refused(removed):
    with pytest.raises(PreservationError, match="without an anchor after messages"):
        parse(
            message("u", "user", text("hi")),
            message("a", "assistant", text("ok")),
            compaction("c", removedCount=removed),
        )


def test_writer_refuses_a_message_carrying_several_tool_results(tmp_path):
    original = replace(native_conversation(), title="Imported")
    calls = replace(
        original.history[1],
        content=(
            ToolCall("one", "read", "{}", "json"),
            ToolCall("two", "read", "{}", "json"),
        ),
    )
    results = replace(
        original.history[2],
        content=(ToolResult("one", (Text("a"),)), ToolResult("two", (Text("b"),))),
    )
    history = native.linked([original.history[0], calls, results, original.history[3]])
    combined = replace(original, history=history, context=history)
    with pytest.raises(PreservationError, match="one tool result per message"):
        DroidAdapter().stage(combined, target(tmp_path, combined))


class Runner:
    def __init__(self, stdout=b"0.228.0\n", stderr=b""):
        self.stdout, self.stderr = stdout, stderr
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return RunResult(0, self.stdout, self.stderr)


def seed(store: Path, owner: str, **extra) -> None:
    path = store / "sessions" / "-somewhere" / f"{uuid4()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**header(owner=owner), "id": path.stem, **extra}) + "\n")


def verified(version="0.228.0"):
    return (
        VerificationRecord(
            "droid", version, HARNESS_VERSION["droid"], "model-request-capture", STAMP
        ),
    )


def test_inspect_target_needs_verification_for_every_version(tmp_path):
    runner = Runner()
    result = DroidAdapter().inspect_target(roots(tmp_path), runner)
    assert (result.supported, result.needs_verification) == (False, True)
    [request] = runner.requests
    env = dict(request.env)
    assert request.argv[-1] == "--version"
    assert env["HOME"] == env["FACTORY_HOME_OVERRIDE"]
    assert env["FACTORY_DISABLE_KEYRING"] == "1"
    assert "FACTORY_API_KEY" not in env


def test_inspect_target_takes_owner_from_the_newest_top_level_session(tmp_path):
    target_roots = roots(tmp_path)
    adapter = DroidAdapter(verified=verified())
    missing = adapter.inspect_target(target_roots, Runner())
    assert missing.diagnostics == (
        "Droid target has no local top-level session to take its owner from",
    )
    seed(target_roots.store, "user-1")
    seed(target_roots.store, "subagent-owner", callingSessionId="parent")
    accepted = adapter.inspect_target(target_roots, Runner())
    assert (accepted.supported, accepted.target_configuration) == (True, (("owner", "user-1"),))


def test_inspect_target_refuses_when_version_starts_the_login_flow(tmp_path):
    result = DroidAdapter(verified=verified()).inspect_target(
        roots(tmp_path), Runner(stderr=b"Device authorization required. Browser opened.")
    )
    assert result.diagnostics == (
        "droid --version started Factory's login flow; refusing droid as a target",
    )


def test_roots_follow_factory_home_override_as_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FACTORY_HOME_OVERRIDE", raising=False)
    adapters = builtin_adapters()
    assert adapters.roots("droid", tmp_path).store == (tmp_path / "home" / ".factory").resolve()
    monkeypatch.setenv("FACTORY_HOME_OVERRIDE", str(tmp_path / "other"))
    found = adapters.roots("droid", tmp_path)
    assert found.store == (tmp_path / "other" / ".factory").resolve()
    assert dict(native.env(found, "droid"))["FACTORY_HOME_OVERRIDE"] == str(
        (tmp_path / "other").resolve()
    )


def test_launch_resumes_by_id_in_the_session_directory(tmp_path):
    target_roots = roots(tmp_path)
    request = DroidAdapter().launch_request(IDENTITY, target_roots)
    assert (request.argv, request.cwd) == (("droid", "resume", SESSION), target_roots.cwd)


class Recording:
    def __init__(self, stderr=b""):
        self.stderr = stderr
        self.requests = []

    def __call__(self):
        return self

    def run(self, request):
        self.requests.append(request)
        return RunResult(0, b"0.228.0\n", self.stderr)


def test_the_resume_version_probe_is_the_keyring_free_droid_probe(monkeypatch):
    runner = Recording()
    monkeypatch.setattr(native_resume, "ProbeRunner", runner)

    assert native_resume._installed_version("droid", "droid") == "0.228.0"
    env = dict(runner.requests[0].env)
    assert (env["FACTORY_DISABLE_KEYRING"], env["FACTORY_HOME_OVERRIDE"]) == ("1", env["HOME"])


def test_the_resume_version_probe_refuses_droids_login_flow(monkeypatch):
    monkeypatch.setattr(native_resume, "ProbeRunner", Recording(b"Device authorization"))

    with pytest.raises(PreservationError, match="started Factory's login flow"):
        native_resume._installed_version("droid", "droid")


def test_version_probes_never_inherit_the_terminal(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"1.0\n", b"")

    monkeypatch.setattr(launch.subprocess, "run", run)
    launch.ProbeRunner().run(RunRequest(("droid", "--version"), Path("/"), ()))
    assert seen["stdin"] is subprocess.DEVNULL


class Hanging:
    def run(self, request):
        raise subprocess.TimeoutExpired(request.argv, 15)


def test_doctor_reports_a_hanging_version_probe_as_unsupported(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for name in ("FACTORY_HOME_OVERRIDE", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENCODE_DB"):
        monkeypatch.delenv(name, raising=False)
    worker = SimpleNamespace(adapters=builtin_adapters(), runner=Hanging())

    rows = diagnostics.compatibility_rows(cast(Any, worker), tmp_path)
    droid = next(row for row in rows if row["agent"] == "droid")
    assert (droid["supported"], droid["diagnostics"]) == (
        False,
        ["Command '('droid', '--version')' timed out after 15 seconds"],
    )


def captured(tool):
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Keep original history"},
                {"type": "text", "text": "[Historical user instruction]"},
                image,
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking"},
                {"type": "tool_use", "id": "c1", "name": "read", "input": {"path": "example.txt"}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", **tool}]},
        {"role": "user", "content": harness.HIDDEN},
        {"role": "assistant", "content": [{"type": "text", "text": "Final answer"}]},
        {"role": "user", "content": harness.PROBE},
    ]


def test_the_harness_requires_droid_to_replay_failed_and_cancelled_results():
    by_name = {case.name: case for case in harness.cases(native_conversation())}
    error, cancelled = by_name["error"], by_name["cancelled"]
    harness.assert_request(captured({"is_error": True, "content": "read failed"}), error)
    harness.assert_request(captured({"content": CANCELLED}), cancelled)
    with pytest.raises(VerificationFailed, match="tool result and error flag"):
        harness.assert_request(captured({"content": "read failed"}), error)
    assert (cancelled.drop_results, by_name["model-view"].compactions) == (True, 0)


def test_picker_metadata_reads_only_the_session_header(tmp_path):
    adapter = DroidAdapter()
    original = hidden_conversation()
    stage = adapter.stage(original, target(tmp_path, original))
    (item,) = stage.files
    torn = replace(stage, files=(replace(item, data=item.data + b"{not json\n"),))

    assert adapter.stage_metadata(torn) == adapter.stage_metadata(stage)
