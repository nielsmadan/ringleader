import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from ringleader.portability import native
from ringleader.portability.capabilities import RunResult, SourceRequest, TargetRequest
from ringleader.portability.codex import PROFILE, CodexAdapter, read_rollout
from ringleader.portability.codex_state import SCHEMA_ID, inspect_database
from ringleader.portability.compact import compact
from ringleader.portability.compare import compare
from ringleader.portability.model import (
    Checkpoint,
    HistoricalTimestamp,
    Identity,
    PreservationError,
    Text,
    validate,
)
from ringleader.portability.serialization import dumps, loads
from tests.portability.fixtures import (
    SESSION_ID,
    checkpoint_conversation,
    codex_records,
    conversation,
)
from tests.portability.test_native_adapters import roots


def initialized(tmp_path):
    target = roots(tmp_path)
    target.store.mkdir(parents=True)
    fixture = json.loads(Path(__file__).with_name("codex-state-5.json").read_text())
    with closing(sqlite3.connect(target.store / "state_5.sqlite")) as db, db:
        for kind in ("table", "index", "trigger", "view"):
            for row in fixture["schema"]:
                if row[0] == kind:
                    db.execute(row[3])
        db.executemany(
            "INSERT INTO _sqlx_migrations(version,description,success,checksum,execution_time) VALUES(?,?,?,?,0)",
            [
                (n, label, success, bytes.fromhex(digest))
                for n, label, success, digest in fixture["migrations"]
            ],
        )
        db.execute("INSERT INTO backfill_state(id,status,updated_at) VALUES(1,'complete',0)")
    return target


def stage(tmp_path, original=None):
    original = original or conversation()
    target = initialized(tmp_path)
    return CodexAdapter().stage(
        original, TargetRequest(Identity("codex", str(uuid4())), target, PROFILE, original.title)
    )


@pytest.mark.parametrize("invalid_schema", [False, True])
def test_schema_inspection_closes_connection(tmp_path, monkeypatch, invalid_schema):
    target = initialized(tmp_path)
    if invalid_schema:
        with closing(sqlite3.connect(target.store / "state_5.sqlite")) as db, db:
            db.execute("UPDATE backfill_state SET status='running'")
    connections = []
    connect = sqlite3.connect

    def record_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", record_connection)
    try:
        if invalid_schema:
            with pytest.raises(PreservationError, match="backfill"):
                inspect_database(target)
        else:
            assert inspect_database(target) == SCHEMA_ID
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connections[0].execute("SELECT 1")
    finally:
        for connection in connections:
            connection.close()


def publish_files(staged):
    for item in staged.files:
        path = staged.roots.store / item.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.data)


@pytest.mark.parametrize("factory", [conversation, checkpoint_conversation])
def test_roundtrip_native_streams_compaction_and_images(tmp_path, factory):
    original = factory()
    adapter = CodexAdapter()
    staged = stage(tmp_path, original)
    compare(original, adapter.read_stage(staged), adapter.normalizations(PROFILE))
    assert inspect_database(staged.roots) == SCHEMA_ID
    assert list(staged.roots.store.iterdir()) == [staged.roots.store / "state_5.sqlite"]
    rows = native.records(staged.files[0].data)
    assert rows[0]["payload"]["session_id"] == staged.identity.native_id
    assert rows[0]["payload"]["source"] == "cli"
    events = [row["payload"] for row in rows if row["type"] == "event_msg"]
    results = [event for event in events if event["type"] == "mcp_tool_call_end"]
    assert results[0]["invocation"]["server"] == "ringleader_history"
    assert results[-1]["result"]["Ok"]["isError"] is True
    assert json.loads(staged.database_payload)["tokens_used"] == 0


def test_registration_atomic_collision_recovery_and_other_rows(tmp_path):
    staged = stage(tmp_path)
    adapter = CodexAdapter()
    assert adapter.append_name(staged)
    publish_files(staged)
    assert adapter.register_stage(staged)
    assert adapter.register_stage(staged, recover=True) is False
    with pytest.raises(PreservationError, match="collision"):
        adapter.register_stage(staged)
    with closing(sqlite3.connect(staged.roots.store / "state_5.sqlite")) as db, db:
        row = db.execute("SELECT id,cwd,name,history_mode FROM threads").fetchone()
        assert row == (
            staged.identity.native_id,
            str(staged.roots.cwd),
            "Portable example",
            "legacy",
        )
    assert adapter.append_name(staged) is False
    index = staged.roots.store / "session_index.jsonl"
    index.write_bytes(
        index.read_bytes()
        + json.dumps(
            {"id": staged.identity.native_id, "thread_name": "Renamed", "updated_at": "later"}
        ).encode()
        + b"\n"
    )
    assert adapter.append_name(staged) is False
    assert json.loads(index.read_bytes().splitlines()[-1])["thread_name"] == "Renamed"


@pytest.mark.parametrize("mutation", ["column", "migration", "backfill"])
def test_registration_rechecks_schema_inside_transaction(tmp_path, mutation):
    staged = stage(tmp_path)
    publish_files(staged)
    with closing(sqlite3.connect(staged.roots.store / "state_5.sqlite")) as db, db:
        db.execute(
            {
                "column": "ALTER TABLE threads ADD COLUMN surprise TEXT",
                "migration": "UPDATE _sqlx_migrations SET checksum=X'00' WHERE version=55",
                "backfill": "UPDATE backfill_state SET status='running'",
            }[mutation]
        )
    with pytest.raises(PreservationError, match=r"schema|backfill"):
        CodexAdapter().register_stage(staged)
    with closing(sqlite3.connect(staged.roots.store / "state_5.sqlite")) as db, db:
        assert db.execute("SELECT count(*) FROM threads").fetchone() == (0,)


def test_malformed_annotations_cannot_restore_missing_content(tmp_path):
    staged = stage(tmp_path)
    rows = native.records(staged.files[0].data)
    tool = next(row for row in rows if row["payload"].get("ringleader_projection") == "result")
    tool["payload"]["result"]["Ok"]["content"][0]["text"] = "changed"
    with pytest.raises(PreservationError, match="disagrees"):
        read_rollout(native.jsonl(rows), staged.identity)


def test_source_keeps_independent_ui_and_model_tool_output(tmp_path):
    staged = stage(tmp_path)
    rows = native.records(staged.files[0].data)
    response = next(row for row in rows if row["payload"].get("type") == "function_call_output")
    response["payload"]["output"][1]["text"] = "truncated model output"
    parsed = read_rollout(native.jsonl(rows), staged.identity)
    assert parsed.history[3].content[0].output == (Text("contents"),)
    assert parsed.context[3].content[0].output == (Text("truncated model output"),)


def test_source_preserves_instruction_lookalike_and_fails_unknown(tmp_path):
    rows = codex_records(
        tmp_path,
        [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<INSTRUCTIONS>Literal user text</INSTRUCTIONS>"}
                ],
            }
        ],
    )
    rows.append(
        {
            "timestamp": rows[0]["timestamp"],
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "<INSTRUCTIONS>Literal user text</INSTRUCTIONS>",
                "images": [],
            },
        }
    )
    identity = Identity("codex", SESSION_ID)
    original = read_rollout(native.jsonl(rows), identity)
    assert original.history[0].content[0].text == "<INSTRUCTIONS>Literal user text</INSTRUCTIONS>"
    assert original.context[0].content[0].text == original.history[0].content[0].text
    rows.append({"type": "response_item", "payload": {"type": "unknown_conversation"}})
    with pytest.raises(PreservationError, match="Unsupported Codex response_item"):
        read_rollout(native.jsonl(rows), identity)


def test_version_probe_does_not_write_target(tmp_path):
    class Runner:
        def run(self, request):
            assert request.cwd != tmp_path
            assert dict(request.env)["CODEX_HOME"] != str(tmp_path / "store")
            return RunResult(0, b"codex-cli 9.0\n", b"")

    result = CodexAdapter().inspect_target(roots(tmp_path), Runner())
    assert result.supported is False
    assert list(tmp_path.iterdir()) == []


def test_configured_database_refused(tmp_path, monkeypatch):
    target = initialized(tmp_path)
    (target.store / "config.toml").write_text('sqlite_home = "/elsewhere"\n')
    with pytest.raises(PreservationError, match="sqlite_home override"):
        inspect_database(target)
    (target.store / "config.toml").unlink()
    monkeypatch.setenv("CODEX_SQLITE_HOME", "/elsewhere")
    with pytest.raises(PreservationError, match="CODEX_SQLITE_HOME"):
        inspect_database(target)


def test_historical_times_and_fresh_recency(tmp_path):
    original = conversation()
    history = tuple(
        replace(m, timestamp=HistoricalTimestamp("2020-01-02T03:04:05Z", m.provenance))
        for m in original.history
    )
    original = replace(original, history=history, context=history)
    staged = stage(tmp_path, original)
    compare(original, CodexAdapter().read_stage(staged), CodexAdapter().normalizations(PROFILE))
    rows = native.records(staged.files[0].data)
    assert rows[0]["payload"]["timestamp"].startswith(str(datetime.now(UTC).year))
    assert rows[1]["timestamp"].startswith("2020-01-02")
    assert json.loads(staged.database_payload)["recency_at"] > 1700000000


def split_checkpoint():
    base = conversation()
    summary = replace(base.history[0], id="summary", content=(Text("Summary"),), parent_id=None)
    model_suffix = native.linked(
        [
            replace(
                m, id="model-" + m.id, provenance=replace(m.provenance, locator="model-" + m.id)
            )
            for m in base.history[1:]
        ]
    )
    checkpoint = Checkpoint("cp", base.history[0].id, (summary,), following_context=model_suffix)
    return replace(base, context=native.linked((summary, *model_suffix)), checkpoints=(checkpoint,))


def test_split_checkpoint_serialization_and_compaction_keep_visible_prose(tmp_path):
    original = split_checkpoint()
    validate(original)
    compare(original, loads(dumps(original)))
    adapter = CodexAdapter()
    staged = stage(tmp_path, original)
    compare(original, adapter.read_stage(staged), adapter.normalizations(PROFILE))
    compacted = compact(original).conversation
    assert any(Text("One succeeded.") in m.content for m in compacted.history)
    assert any(
        Text("# AGENTS.md instructions: this is my literal input") in m.content
        for m in compacted.history
    )
    assert any(Text("One succeeded.") in m.content for m in compacted.context)
    assert compacted.attachments[0].data == b"fixture-image"
    validate(compacted)


def test_split_checkpoint_mismatch_and_other_writers_reject(tmp_path):
    from ringleader.portability.claude import PROFILE as CLAUDE_PROFILE
    from ringleader.portability.claude import ClaudeAdapter
    from ringleader.portability.pi import PROFILE as PI_PROFILE
    from ringleader.portability.pi import PiAdapter

    original = split_checkpoint()
    with pytest.raises(PreservationError, match="Active context"):
        validate(replace(original, context=original.history))
    for adapter, profile in [(ClaudeAdapter(), CLAUDE_PROFILE), (PiAdapter(), PI_PROFILE)]:
        with pytest.raises(PreservationError, match="independent post-checkpoint"):
            adapter.stage(
                original,
                TargetRequest(
                    Identity(profile.agent, str(uuid4())), roots(tmp_path), profile, "title"
                ),
            )


def test_snapshot_owns_embedded_assets_after_source_deleted(tmp_path):
    staged = stage(tmp_path)
    publish_files(staged)
    path = staged.roots.store / staged.source_ref
    snapshot = CodexAdapter().snapshot(
        SourceRequest(staged.identity, staged.roots, staged.source_ref)
    )
    path.unlink()
    assert snapshot.conversation.attachments[0].data == b"fixture-image"
    assert snapshot.files[0].data == staged.files[0].data


def test_native_base_instructions_remain_historical_context(tmp_path):
    rows = codex_records(
        tmp_path,
        [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Question"}],
            }
        ],
    )
    rows[0]["payload"]["base_instructions"] = {"text": "Custom or default source policy"}
    rows.append({"type": "event_msg", "payload": {"type": "user_message", "message": "Question"}})
    original = read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))
    assert original.context[0].content[0].text == "Custom or default source policy"
    assert original.context[0].provenance.origin == "native"
    assert len(original.history) == 1
    staged = stage(tmp_path, original)
    compare(original, CodexAdapter().read_stage(staged), CodexAdapter().normalizations(PROFILE))
    target_rows = native.records(staged.files[0].data)
    assert target_rows[0]["payload"]["base_instructions"] is None
    model = next(row["payload"] for row in target_rows if row["type"] == "response_item")
    assert model["role"] == "user"
    assert "Historical system instruction" in model["content"][0]["text"]


def test_native_split_checkpoint_keeps_distinct_suffix(tmp_path):
    rows = codex_records(
        tmp_path,
        [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Before"}],
            }
        ],
    )
    rows.extend(
        [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Before"}},
            {
                "type": "compacted",
                "payload": {
                    "message": "Summary",
                    "replacement_history": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Summary"}],
                        }
                    ],
                },
            },
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Visible after"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Model after"}],
                },
            },
        ]
    )
    original = read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))
    assert original.checkpoints[0].following_context[0].content == (Text("Model after"),)
    compacted = compact(original).conversation
    assert compacted.history[-1].content == (Text("Visible after"),)
    assert compacted.context[-1].content == (Text("Model after"),)
    staged = stage(tmp_path, original)
    compare(original, CodexAdapter().read_stage(staged), CodexAdapter().normalizations(PROFILE))


def test_model_status_markers_are_native_and_verified(tmp_path):
    staged = stage(tmp_path)
    rows = native.records(staged.files[0].data)
    output = next(row for row in rows if row["payload"].get("type") == "custom_tool_call_output")
    assert output["payload"]["output"][0]["text"] == "[Historical tool result: failed]"
    assert output["payload"]["output"][1]["text"] == "failed"
    output["payload"]["output"][0]["text"] = "[Historical tool result: succeeded]"
    with pytest.raises(PreservationError, match="status annotation disagrees"):
        read_rollout(native.jsonl(rows), staged.identity)


def test_read_only_preflight_does_not_create_wal_sidecars(tmp_path):
    target = initialized(tmp_path)
    with closing(sqlite3.connect(target.store / "state_5.sqlite")) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
    db.close()
    before = {p.name: p.read_bytes() for p in target.store.iterdir()}
    assert inspect_database(target) == SCHEMA_ID
    assert {p.name: p.read_bytes() for p in target.store.iterdir()} == before


def test_concurrent_registration_preserves_unrelated_threads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    staged = stage(tmp_path)
    adapter = CodexAdapter()
    other = adapter.stage(
        conversation(),
        TargetRequest(Identity("codex", str(uuid4())), staged.roots, PROFILE, "Other"),
    )
    publish_files(staged)
    publish_files(other)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(adapter.register_stage, [staged, other]))
    assert results == [True, True]
    with closing(sqlite3.connect(staged.roots.store / "state_5.sqlite")) as db, db:
        assert set(db.execute("SELECT id,name FROM threads")) == {
            (staged.identity.native_id, "Portable example"),
            (other.identity.native_id, "Other"),
        }


def test_recovery_refuses_same_id_different_native_content(tmp_path):
    staged = stage(tmp_path)
    publish_files(staged)
    adapter = CodexAdapter()
    adapter.register_stage(staged)
    (staged.roots.store / staged.source_ref).write_bytes(b"foreign session\n")
    with pytest.raises(PreservationError, match="collision"):
        adapter.register_stage(staged, recover=True)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_audio", "audio_url": "data:audio/wav;base64,AA=="}],
        },
        {"type": "function_call", "call_id": "dangling", "name": "read", "arguments": "{}"},
    ],
)
def test_unknown_or_incomplete_model_payload_fails(tmp_path, payload):
    rows = codex_records(tmp_path, [payload])
    rows.append({"type": "event_msg", "payload": {"type": "user_message", "message": "visible"}})
    with pytest.raises(PreservationError):
        read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))


def test_native_turn_grouping_survives_multiple_users_in_one_task(tmp_path):
    rows = codex_records(tmp_path, [])
    rows.extend(
        [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "original-turn"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "First"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "First"}],
                },
            },
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Second"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Second"}],
                },
            },
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "original-turn"}},
        ]
    )
    original = read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))
    staged = stage(tmp_path, original)
    output = native.records(staged.files[0].data)
    starts = [row["payload"] for row in output if row["payload"].get("type") == "task_started"]
    assert len(starts) == 1


def test_pending_native_task_fails(tmp_path):
    rows = codex_records(tmp_path, [])
    rows.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "pending"}})
    with pytest.raises(PreservationError, match="Incomplete Codex turn"):
        read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))


def test_name_append_requires_previsibility_and_preserves_other_names(tmp_path):
    staged = stage(tmp_path)
    adapter = CodexAdapter()
    index = staged.roots.store / "session_index.jsonl"
    prior = b'{"id":"other","thread_name":"Other","updated_at":"2020"}\n'
    index.write_bytes(prior)
    publish_files(staged)
    with pytest.raises(PreservationError, match="before the rollout"):
        adapter.append_index(staged)
    assert index.read_bytes() == prior
    assert adapter.index_intent(staged).relative_path == "session_index.jsonl"


def test_paginated_and_root_inheritance_are_explicitly_unsupported(tmp_path):
    for field, value in [
        ("history_mode", "paginated"),
        ("history_base", {"ordinal": 2}),
        ("session_id", str(uuid4())),
    ]:
        rows = codex_records(tmp_path, [])
        rows[0]["payload"][field] = value
        with pytest.raises(PreservationError, match=r"paginated|inherited"):
            read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))


@pytest.mark.parametrize("kind", ["exec", "patch", "completion"])
@pytest.mark.parametrize("success", [True, False])
def test_native_tool_families_preserve_full_visible_and_model_outputs(tmp_path, kind, success):
    from ringleader.portability.model import ToolOmission, ToolResult
    from tests.portability.fixtures import codex_tool_records

    rows = codex_tool_records(tmp_path, kind, success)
    original = read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))
    result = original.history[2].content[0]
    assert isinstance(result, ToolResult)
    if kind == "exec":
        assert result.output == (
            Text("[Native stdout]"),
            Text("full stdout\n"),
            Text("[Native stderr]"),
            Text("full stderr\n"),
            Text("[Native aggregated_output]"),
            Text("full stdout\nfull stderr\n"),
            Text("[Native formatted_output]"),
            Text("model output truncated independently"),
        )
        assert result.is_error is not success
        assert '"exit_code":' in original.history[2].provenance.evidence
    elif kind == "patch":
        assert result.output[1] == Text("")
        changes = json.loads(result.output[-1].text)
        assert changes == next(
            row["payload"]["changes"]
            for row in rows
            if row["payload"].get("type") == "patch_apply_end"
        )
        assert result.is_error is not success
    else:
        assert result.output == (Text("complete native visible output"),)
    assert original.context[2].content[0].output == (Text("model output truncated independently"),)
    staged = stage(tmp_path, original)
    adapter = CodexAdapter()
    compare(original, adapter.read_stage(staged), adapter.normalizations(PROFILE))
    compacted = compact(original).conversation
    omissions = [
        part
        for message in compacted.history
        for part in message.content
        if isinstance(part, ToolOmission)
    ]
    assert omissions[0].names == ("apply_patch" if kind == "patch" else "exec_command",)
    compact_stage = adapter.stage(
        compacted, TargetRequest(Identity("codex", str(uuid4())), staged.roots, PROFILE, "compact")
    )
    compare(
        replace(compacted, title="compact"),
        adapter.read_stage(compact_stage),
        adapter.normalizations(PROFILE),
    )


def test_native_patch_extra_fields_and_output_correlation_fail(tmp_path):
    from tests.portability.fixtures import codex_tool_records

    rows = codex_tool_records(tmp_path, "patch")
    patch = next(row["payload"] for row in rows if row["payload"].get("type") == "patch_apply_end")
    patch["changes"]["added.txt"]["unrecognized_conversational_content"] = "must survive"
    with pytest.raises(PreservationError, match="FileChange"):
        read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))
    rows = codex_tool_records(tmp_path, "completion")
    completed = next(
        row["payload"] for row in rows if row["payload"].get("type") == "item_completed"
    )
    completed["item"]["id"] = "unknown"
    with pytest.raises(PreservationError, match="unambiguous matched model output"):
        read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))


def test_native_exec_unknown_conversational_field_fails(tmp_path):
    from tests.portability.fixtures import codex_tool_records

    rows = codex_tool_records(tmp_path, "exec")
    execution = next(
        row["payload"] for row in rows if row["payload"].get("type") == "exec_command_end"
    )
    execution["future_output"] = "must not disappear"
    with pytest.raises(PreservationError, match="execution completion fields"):
        read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))


@pytest.mark.parametrize("existing_index", [False, True])
def test_name_append_rejects_unnamed_database_identity_before_mutation(tmp_path, existing_index):
    existing = stage(tmp_path)
    publish_files(existing)
    adapter = CodexAdapter()
    adapter.register_stage(existing)
    with closing(sqlite3.connect(existing.roots.store / "state_5.sqlite")) as db, db:
        db.execute("UPDATE threads SET name=NULL WHERE id=?", (existing.identity.native_id,))
    index = existing.roots.store / "session_index.jsonl"
    prior = b'{"id":"other","thread_name":"Keep","updated_at":"2020"}\n'
    if existing_index:
        index.write_bytes(prior)
    colliding = adapter.stage(
        conversation(), TargetRequest(existing.identity, existing.roots, PROFILE, "Foreign title")
    )
    alternative = (
        "sessions/2099/01/01/rollout-alternative-" + existing.identity.native_id + ".jsonl"
    )
    payload = json.loads(colliding.database_payload)
    payload["rollout_path"] = str(existing.roots.store / alternative)
    colliding = replace(
        colliding,
        source_ref=alternative,
        files=(replace(colliding.files[0], relative_path=alternative),),
        database_payload=json.dumps(payload).encode(),
    )
    with pytest.raises(PreservationError, match="session ID collision"):
        adapter.append_index(colliding)
    assert index.read_bytes() == prior if existing_index else not index.exists()
    with closing(sqlite3.connect(existing.roots.store / "state_5.sqlite")) as db, db:
        assert db.execute(
            "SELECT name FROM threads WHERE id=?", (existing.identity.native_id,)
        ).fetchone() == (None,)


@pytest.mark.parametrize("directory", ["sessions", "archived_sessions"])
def test_name_append_rejects_foreign_rollout_identity(tmp_path, directory):
    staged = stage(tmp_path)
    foreign = (
        staged.roots.store / directory / ("rollout-older-" + staged.identity.native_id + ".jsonl")
    )
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_bytes(staged.files[0].data)
    with pytest.raises(PreservationError, match="rollout identity collision"):
        CodexAdapter().append_index(staged)
    assert not (staged.roots.store / "session_index.jsonl").exists()
    assert foreign.read_bytes() == staged.files[0].data


def test_source_mcp_keeps_different_visible_and_model_tool_identity(tmp_path):
    from tests.portability.fixtures import codex_mcp_records

    original = read_rollout(
        native.jsonl(codex_mcp_records(tmp_path)), Identity("codex", SESSION_ID)
    )
    assert original.history[1].content[0].name == "read"
    assert original.context[1].content[0].name == "mcp__files__read"
    assert original.history[1].provenance.evidence.endswith('mcp-server:"files"')
    staged = stage(tmp_path, original)
    adapter = CodexAdapter()
    compare(original, adapter.read_stage(staged), adapter.normalizations(PROFILE))
    rows = native.records(staged.files[0].data)
    visible = [row["payload"] for row in rows if row["payload"].get("type") == "mcp_tool_call_end"]
    assert [item["invocation"]["tool"] for item in visible] == ["read", "read"]
    model = next(row["payload"] for row in rows if row["payload"].get("type") == "function_call")
    assert model["name"] == "mcp__files__read"


@pytest.mark.parametrize("stream", ["event_msg", "response_item"])
@pytest.mark.parametrize("phase", ["commentary", "final_answer"])
def test_assistant_phases_are_rejected_before_canonicalization(tmp_path, stream, phase):
    from tests.portability.fixtures import codex_mcp_records

    rows = codex_mcp_records(tmp_path)
    message = next(
        row["payload"]
        for row in rows
        if row["type"] == stream
        and (
            row["payload"].get("type") == "agent_message"
            or row["payload"].get("role") == "assistant"
        )
    )
    message["phase"] = phase
    with pytest.raises(PreservationError, match="message phases are unsupported"):
        read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))


def test_visible_local_audio_is_rejected_with_text_only_model_stream(tmp_path):
    from tests.portability.fixtures import codex_mcp_records

    rows = codex_mcp_records(tmp_path)
    user = next(row["payload"] for row in rows if row["payload"].get("type") == "user_message")
    user["local_audio"] = [str(tmp_path / "voice.wav")]
    with pytest.raises(PreservationError, match="audio"):
        read_rollout(native.jsonl(rows), Identity("codex", SESSION_ID))
