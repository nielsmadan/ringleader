from __future__ import annotations

import base64
import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from ringleader.portability.adapters import builtin_adapters
from ringleader.portability.capabilities import NativeRoots, SourceRequest, TargetRequest
from ringleader.portability.compact import compact
from ringleader.portability.compare import compare
from ringleader.portability.model import (
    AttachmentRef,
    Identity,
    PreservationError,
    Text,
    ToolCall,
    ToolResult,
)
from ringleader.portability.opencode import (
    PROFILE,
    QUESTION,
    OpenCodeAdapter,
    _payload_export,
    parse,
)
from ringleader.portability.opencode_state import (
    SCHEMA_ID,
    decode,
    encode,
    insert,
    normalized_remote,
    resolve_project,
    schema_identity,
)
from tests.portability.fixtures import conversation


@pytest.fixture
def roots(tmp_path):
    roots = NativeRoots(
        tmp_path / "home",
        tmp_path / "data" / "opencode",
        tmp_path / "cwd",
        tmp_path / "config",
        tmp_path / "cache",
    )
    for path in (roots.home, roots.store, roots.cwd, roots.config, roots.cache):
        path.mkdir(parents=True)
    (roots.config / "opencode").mkdir()
    (roots.config / "opencode" / "opencode.json").write_text(
        '{"model":"anthropic/offline-fixture"}'
    )
    schema = json.loads(Path("tests/portability/opencode-1.18.30.json").read_text())
    with sqlite3.connect(roots.store / "opencode.db") as connection:
        for statement in schema["sql"]:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO migration VALUES (?,0)", ((item,) for item in schema["migrations"])
        )
    return roots


def shared_conversation():
    base = conversation()
    history = []
    for message in base.history:
        contents = []
        for original in message.content:
            item = original
            if isinstance(item, ToolCall) and item.argument_format == "freeform":
                item = replace(item, argument_format="json", arguments='{"command":"cat b"}')
            if isinstance(item, ToolResult) and item.is_error:
                item = replace(item, output=(Text("failed"),))
            contents.append(item)
        history.append(replace(message, content=tuple(contents)))
    return replace(base, history=tuple(history), context=tuple(history))


def stage(roots, value=None):
    adapter = OpenCodeAdapter()
    return adapter.stage(
        value or shared_conversation(),
        TargetRequest(adapter.allocate_identity(), roots, PROFILE, "Portable example"),
    )


def ordinary_export(roots, *, compaction=False):
    session = "ses_native"
    rows = []

    def add(key, role, parts, **extra):
        info = {
            "id": key,
            "sessionID": session,
            "role": role,
            "agent": "build",
            "time": {"created": len(rows) + 1},
            **extra,
        }
        if role == "assistant":
            info.update(
                parentID=rows[-1]["info"]["id"] if rows[-1]["info"]["role"] == "user" else "msg_1",
                finish="stop",
                providerID="anthropic",
                modelID="claude",
                mode="build",
                path={"cwd": str(roots.cwd), "root": str(roots.cwd)},
                cost=0,
                tokens={"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            )
            info["time"]["completed"] = len(rows) + 2
        else:
            info["model"] = {"providerID": "anthropic", "modelID": "claude"}
        rows.append(
            {
                "info": info,
                "parts": [
                    {"id": f"prt_{key}_{index}", "sessionID": session, "messageID": key, **p}
                    for index, p in enumerate(parts)
                ],
            }
        )

    add("msg_1", "user", [{"type": "text", "text": "Read the full file"}])
    add(
        "msg_2",
        "assistant",
        [
            {"type": "text", "text": "Checking."},
            {
                "type": "tool",
                "callID": "call-1",
                "tool": "read",
                "state": {
                    "status": "completed",
                    "input": {"path": "a"},
                    "output": "full output\n" * 100,
                    "title": "a",
                    "metadata": {},
                    "time": {"start": 2, "end": 3},
                },
            },
        ],
    )
    add("msg_3", "assistant", [{"type": "text", "text": "Read complete."}])
    if compaction:
        add("msg_4", "user", [{"type": "compaction", "auto": True, "tail_start_id": "msg_1"}])
        add("msg_5", "assistant", [{"type": "text", "text": "We read a."}], summary=True)
        add("msg_6", "user", [{"type": "text", "text": "Continue"}])
    return {
        "info": {
            "id": session,
            "slug": "native",
            "projectID": "global",
            "directory": str(roots.cwd),
            "title": "Native",
            "version": "1.18.30",
            "time": {"created": 1, "updated": 6},
        },
        "messages": rows,
    }


def seed(roots, export):
    key = export["info"]["id"]
    with sqlite3.connect(roots.store / "opencode.db") as connection:
        if not connection.execute("SELECT 1 FROM project WHERE id='global'").fetchone():
            insert(
                connection,
                "project",
                {
                    "id": "global",
                    "worktree": "/",
                    "sandboxes": "[]",
                    "time_created": 0,
                    "time_updated": 0,
                },
            )
        insert(
            connection,
            "session",
            {
                "id": key,
                "project_id": "global",
                "slug": key,
                "directory": str(roots.cwd),
                "title": export["info"]["title"],
                "version": "1.18.30",
                "time_created": 1,
                "time_updated": 6,
            },
        )
        for row in export["messages"]:
            info = row["info"]
            insert(
                connection,
                "message",
                {
                    "id": info["id"],
                    "session_id": key,
                    "time_created": info["time"]["created"],
                    "time_updated": 6,
                    "data": json.dumps(
                        {k: v for k, v in info.items() if k not in ("id", "sessionID")}
                    ),
                },
            )
            for part in row["parts"]:
                insert(
                    connection,
                    "part",
                    {
                        "id": part["id"],
                        "session_id": key,
                        "message_id": info["id"],
                        "time_created": 1,
                        "time_updated": 6,
                        "data": json.dumps(
                            {
                                k: v
                                for k, v in part.items()
                                if k not in ("id", "sessionID", "messageID")
                            }
                        ),
                    },
                )


def snapshot(roots):
    return OpenCodeAdapter().snapshot(
        SourceRequest(Identity("opencode", "ses_native"), roots, "opencode.db")
    )


def test_exact_schema_and_pure_stage_full_compact(roots):
    adapter = OpenCodeAdapter()
    before = {p.name: p.read_bytes() for p in roots.store.iterdir()}
    assert adapter.inspect_target(roots, None).schema_identity == SCHEMA_ID
    for value in (shared_conversation(), compact(shared_conversation()).conversation):
        result = stage(roots, value)
        compare(value, adapter.read_stage(result), adapter.normalizations(PROFILE))
        assert result.files == ()
        export = _payload_export(decode(result.database_payload))
        assert export["info"]["time"]["created"] > 1_000_000
        assert export["info"]["metadata"]["ringleader"]["registration"]
        assert all(m["info"].get("cost", 0) == 0 for m in export["messages"])
    assert {p.name: p.read_bytes() for p in roots.store.iterdir()} == before


def test_ordinary_source_full_outputs_and_attachments(roots):
    export = ordinary_export(roots)
    attachment = roots.cwd / "image.png"
    attachment.write_bytes(b"native image")
    export["messages"][0]["parts"].append(
        {
            "id": "prt_image",
            "messageID": "msg_1",
            "sessionID": "ses_native",
            "type": "file",
            "url": attachment.as_uri(),
            "mime": "image/png",
            "filename": "photo.png",
        }
    )
    seed(roots, export)
    value = snapshot(roots)
    attachment.unlink()
    assert value.conversation.attachments[0].data == b"native image"
    assert value.conversation.history[2].content == (
        ToolResult("call-1", (Text("full output\n" * 100),)),
    )
    assert value.conversation.history[0].timestamp.value == "1"
    assert value.conversation.context == value.conversation.history
    assert value.identity() == value.identity()


def test_ordinary_compaction_retained_tail_and_roundtrip(roots):
    export = ordinary_export(roots, compaction=True)
    seed(roots, export)
    value = snapshot(roots).conversation
    assert [m.content[0] for m in value.context[:2]] == [Text(QUESTION), Text("We read a.")]
    assert value.checkpoints[0].retained_message_ids == ("msg_1", "msg_2", "msg_2:results", "msg_3")
    adapter = OpenCodeAdapter()
    result = adapter.stage(
        value, TargetRequest(adapter.allocate_identity(), roots, PROFILE, value.title)
    )
    compare(value, adapter.read_stage(result), adapter.normalizations(PROFILE))
    with pytest.raises(PreservationError, match="independent checkpoint"):
        stage(roots, compact(value).conversation)


def test_completed_tool_error_preserves_status(roots):
    export = ordinary_export(roots)
    tool = export["messages"][1]["parts"][1]["state"]
    tool.update(status="error", error="permission denied")
    tool.pop("output")
    seed(roots, export)
    assert snapshot(roots).conversation.history[2].content == (
        ToolResult("call-1", (Text("permission denied"),), True),
    )


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        (
            lambda x: x["messages"][1]["info"].update(
                error={"name": "MessageAbortedError", "data": {"message": "aborted"}}
            ),
            "error/aborted",
        ),
        (lambda x: x["messages"][0]["parts"][0].update(ignored=True), "ignored text"),
        (
            lambda x: x["messages"][1]["parts"][1]["state"]["time"].update(compacted=1),
            "time.compacted",
        ),
        (
            lambda x: x["messages"][1]["parts"][1]["state"]["metadata"].update(
                interrupted=True, output="partial"
            ),
            "interrupted",
        ),
        (
            lambda x: x["messages"][1]["parts"][1]["state"].update(status="running"),
            "Incomplete OpenCode tool",
        ),
        (
            lambda x: x["messages"][1]["info"]["time"].pop("completed"),
            "Incomplete OpenCode assistant",
        ),
        (lambda x: x["messages"][0]["parts"][0].update(type="subtask"), "conversational part"),
        (
            lambda x: x["messages"][0]["info"].update(system="historical system prompt"),
            "historical user.system",
        ),
        (lambda x: x["messages"][0]["info"].update(unknownConversation="text"), "fields"),
        (lambda x: x["info"].update(revert={"messageID": "msg_2"}), "session revert"),
        (
            lambda x: x["messages"][0]["parts"][0].update(
                type="file", mime="text/plain", url="data:text/plain;base64,eA=="
            ),
            "file fields",
        ),
    ],
)
def test_specific_unsupported_semantics(roots, mutation, diagnostic):
    value = ordinary_export(roots)
    mutation(value)
    with pytest.raises(PreservationError, match=diagnostic):
        parse(value, Identity("opencode", "ses_native"))


def test_registration_atomic_collision_recovery_and_native_edits(roots):
    adapter = OpenCodeAdapter()
    result = stage(roots)
    assert adapter.register_stage(result) is True
    with sqlite3.connect(roots.store / "opencode.db") as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute(
            "UPDATE session SET title=?,time_updated=? WHERE id=?",
            ("native rename", 999, result.identity.native_id),
        )
        first = connection.execute(
            "SELECT id FROM message WHERE session_id=? ORDER BY id", (result.identity.native_id,)
        ).fetchone()[0]
        connection.execute("UPDATE message SET data=? WHERE id=?", ('{"native":"edit"}', first))
    assert adapter.register_stage(result, recover=True) is False
    with sqlite3.connect(roots.store / "opencode.db") as connection:
        assert connection.execute(
            "SELECT title,time_updated FROM session WHERE id=?", (result.identity.native_id,)
        ).fetchone() == ("native rename", 999)
        assert (
            connection.execute("SELECT data FROM message WHERE id=?", (first,)).fetchone()[0]
            == '{"native":"edit"}'
        )
    with pytest.raises(PreservationError, match="collision"):
        adapter.register_stage(result)
    second = stage(roots)
    payload = decode(second.database_payload)
    payload["message"][0]["id"] = first
    previous = decode(second.database_payload)["message"][0]["id"]
    for part in payload["part"]:
        if part["message_id"] == previous:
            part["message_id"] = first
    for row in payload["message"]:
        data = decode(row["data"])
        if data.get("parentID") == previous:
            data["parentID"] = first
            row["data"] = encode(data).decode()
    meta = decode(payload["session"]["metadata"])
    meta["ringleader"]["messages"][first] = meta["ringleader"]["messages"].pop(previous)
    payload["session"]["metadata"] = encode(meta).decode()
    with pytest.raises(PreservationError, match="UNIQUE"):
        adapter.register_stage(replace(second, database_payload=encode(payload)))
    with sqlite3.connect(roots.store / "opencode.db") as connection:
        assert connection.execute("SELECT id FROM session").fetchall() == [
            (result.identity.native_id,)
        ]


def test_schema_guards_and_missing_native_database(roots):
    with sqlite3.connect(roots.store / "opencode.db") as connection:
        assert schema_identity(connection) == SCHEMA_ID
        connection.execute("CREATE TABLE unsupported (id TEXT)")
    with pytest.raises(PreservationError, match="schema/migrations"):
        stage(roots)
    (roots.store / "opencode.db").unlink()
    assert OpenCodeAdapter().inspect_target(roots, None).supported is False
    assert list(roots.store.iterdir()) == []


@pytest.mark.parametrize("table", ["session_message", "session_input", "session_context_epoch"])
def test_v2_rows_never_masquerade_as_legacy(roots, table):
    seed(roots, ordinary_export(roots))
    with sqlite3.connect(roots.store / "opencode.db") as connection:
        if table == "session_message":
            connection.execute(
                "INSERT INTO session_message VALUES ('v2','ses_native','user',1,1,1,'{}')"
            )
        elif table == "session_input":
            connection.execute(
                "INSERT INTO session_input VALUES ('v2','ses_native','{}','next',1,NULL,1)"
            )
        else:
            connection.execute(
                "INSERT INTO session_context_epoch VALUES ('ses_native','baseline','{}',1)"
            )
    with pytest.raises(PreservationError, match="active V2"):
        snapshot(roots)


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://GitHub.COM/Owner/Repo.git/", "github.com/Owner/Repo"),
        ("git@GITHUB.com:Owner/Repo.git", "github.com/Owner/Repo"),
        ("file:///tmp/a", None),
        ("/tmp/a", None),
    ],
)
def test_remote_normalization(remote, expected):
    assert normalized_remote(remote) == expected


def test_project_native_resolution(roots, monkeypatch):
    import hashlib

    from ringleader.portability import opencode_state

    assert resolve_project(roots.cwd) == ("global", "/", None)
    git = roots.cwd / ".git"
    git.mkdir()
    (git / "opencode").write_text("native-cached")
    values = {
        ("rev-parse", "--git-common-dir"): ".git",
        ("rev-parse", "--git-dir"): ".git",
        ("rev-parse", "--show-toplevel"): str(roots.cwd),
        ("remote", "get-url", "origin"): "git@GITHUB.com:Owner/Repo.git",
        ("rev-list", "--max-parents=0", "HEAD"): "bbb\naaa",
    }
    monkeypatch.setattr(opencode_state, "_git", lambda cwd, *args: values.get(args))
    assert resolve_project(roots.cwd) == (
        hashlib.sha1(b"git-remote:github.com/Owner/Repo").hexdigest(),
        str(roots.cwd),
        "git",
    )
    values[("remote", "get-url", "origin")] = None
    assert resolve_project(roots.cwd)[0] == "native-cached"
    (git / "opencode").unlink()
    assert resolve_project(roots.cwd)[0] == "aaa"
    assert list(git.iterdir()) == []


def test_native_ids_launch_and_capability(roots):
    adapter = builtin_adapters().get("opencode")
    identity = adapter.identity_allocator.allocate_identity()
    assert re.fullmatch(r"ses_[a-f0-9]{12}[A-Za-z0-9]{14}", identity.native_id)
    request = adapter.launcher.launch_request(identity, roots)
    assert request.argv == ("opencode", "--session", identity.native_id)
    assert request.cwd == roots.cwd
    assert dict(request.env)["XDG_DATA_HOME"] == str(roots.store.parent)


def test_snapshot_uses_one_consistent_sqlite_read_transaction(roots, monkeypatch):
    from ringleader.portability import opencode_state
    from ringleader.portability import snapshot as snapshot_module

    seed(roots, ordinary_export(roots))
    path = roots.store / "opencode.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    original_connect = sqlite3.connect
    trace = []
    changed = False

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        if kwargs.get("uri"):

            def on_sql(sql):
                nonlocal changed
                trace.append(sql)
                if sql.startswith("SELECT * FROM message") and not changed:
                    changed = True
                    with original_connect(path) as writer:
                        data = json.loads(
                            writer.execute(
                                "SELECT data FROM part WHERE id='prt_msg_2_1'"
                            ).fetchone()[0]
                        )
                        data["state"]["output"] = "concurrent output"
                        writer.execute(
                            "UPDATE part SET data=? WHERE id='prt_msg_2_1'", (json.dumps(data),)
                        )

            connection.set_trace_callback(on_sql)
        return connection

    monkeypatch.setattr(snapshot_module.sqlite3, "connect", traced_connect)
    first = snapshot(roots).conversation
    assert first.history[2].content[0].output == (Text("full output\n" * 100),)
    assert trace.count("BEGIN") == 1
    assert trace.count("ROLLBACK") == 1
    assert snapshot(roots).conversation.history[2].content[0].output == (Text("concurrent output"),)
    assert opencode_state.SCHEMA_ID == SCHEMA_ID


def test_registration_rolls_back_new_project_and_graph(roots, monkeypatch):
    from ringleader.portability import opencode_state

    result = stage(roots)
    original = opencode_state.insert

    def fail_part(connection, table, row):
        if table == "part":
            raise sqlite3.IntegrityError("injected part failure")
        original(connection, table, row)

    monkeypatch.setattr(opencode_state, "insert", fail_part)
    with pytest.raises(PreservationError, match="injected part failure"):
        OpenCodeAdapter().register_stage(result)
    with sqlite3.connect(roots.store / "opencode.db") as connection:
        assert {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("project", "session", "message", "part")
        } == {"project": 0, "session": 0, "message": 0, "part": 0}


def test_attachments_are_target_owned_after_source_removal(roots):
    export = ordinary_export(roots)
    image = {
        "id": "prt_tool_attachment",
        "sessionID": "ses_native",
        "messageID": "msg_2",
        "type": "file",
        "mime": "image/png",
        "filename": "original.png",
        "url": "data:image/png;base64," + base64.b64encode(b"complete image bytes").decode(),
    }
    export["messages"][1]["parts"][1]["state"]["attachments"] = [image]
    seed(roots, export)
    original = snapshot(roots).conversation
    adapter = OpenCodeAdapter()
    result = adapter.stage(
        original, TargetRequest(adapter.allocate_identity(), roots, PROFILE, original.title)
    )
    with sqlite3.connect(roots.store / "opencode.db") as connection:
        connection.execute("DELETE FROM part WHERE session_id='ses_native'")
        connection.execute("DELETE FROM message WHERE session_id='ses_native'")
        connection.execute("DELETE FROM session WHERE id='ses_native'")
    assert adapter.register_stage(result)
    copied = adapter.snapshot(SourceRequest(result.identity, roots, "opencode.db")).conversation
    compare(original, copied, adapter.normalizations(PROFILE))
    assert copied.attachments[0].data == b"complete image bytes"
    assert copied.history[2].content[0].output[1] == AttachmentRef(copied.attachments[0].id)


def test_target_model_config_is_scoped_and_revalidated(roots):
    adapter = OpenCodeAdapter()
    ready = adapter.inspect_target(roots, None)
    assert ready.target_configuration == (
        ("providerID", "anthropic"),
        ("modelID", "offline-fixture"),
    )
    target = TargetRequest(
        adapter.allocate_identity(), roots, PROFILE, "Portable example", ready.target_configuration
    )
    config = roots.config / "opencode"
    (config / "config.json").write_text('{"model":"ignored/earlier"}')
    assert adapter.target_model(roots) == ("anthropic", "offline-fixture")
    (config / "opencode.jsonc").write_text('{"model":"openai/changed"}')
    assert adapter.target_model(roots) == ("openai", "changed")
    with pytest.raises(PreservationError, match="changed since inspection"):
        adapter.stage(shared_conversation(), target)
    (config / "opencode.jsonc").unlink()
    result = adapter.stage(shared_conversation(), target)
    payload = decode(result.database_payload)
    assert decode(payload["session"]["model"]) == {
        "providerID": "anthropic",
        "id": "offline-fixture",
    }
    assert all(
        row["info"]["model"] == {"providerID": "anthropic", "modelID": "offline-fixture"}
        for row in _payload_export(payload)["messages"]
        if row["info"]["role"] == "user"
    )


@pytest.mark.parametrize(
    "condition", ["no-model", "jsonc", "environment", "project", "interpolation"]
)
def test_target_model_uncertain_defaults_fail_before_writes(roots, monkeypatch, condition):
    path = roots.config / "opencode" / "opencode.json"
    if condition == "no-model":
        path.write_text("{}")
    elif condition == "jsonc":
        path.write_text('{/* not strict JSON */ "model":"a/b"}')
    elif condition == "environment":
        monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"model":"a/b"}')
    elif condition == "project":
        (roots.cwd / "opencode.json").write_text("{}")
    else:
        path.write_text('{"model":"{env:MODEL}"}')
    before = {p.name: p.read_bytes() for p in roots.store.iterdir()}
    assert OpenCodeAdapter().inspect_target(roots, None).supported is False
    with pytest.raises(PreservationError):
        stage(roots)
    assert {p.name: p.read_bytes() for p in roots.store.iterdir()} == before


def test_source_does_not_depend_on_target_model_config(roots):
    seed(roots, ordinary_export(roots))
    (roots.config / "opencode" / "opencode.json").unlink()
    adapter = OpenCodeAdapter()
    assert adapter.inspect_source(
        SourceRequest(Identity("opencode", "ses_native"), roots, "opencode.db")
    ).supported
    assert snapshot(roots).conversation.history[0].content == (Text("Read the full file"),)


def test_unknown_fields_and_annotation_tampering_fail(roots):
    result = stage(roots)
    export = _payload_export(decode(result.database_payload))
    export["messages"][0]["parts"][0]["text"] = "rewritten historical instruction"
    with pytest.raises(PreservationError, match="annotation disagrees"):
        parse(export, result.identity)
    export = ordinary_export(roots)
    export["messages"][1]["parts"][1]["state"]["future_output"] = "must preserve"
    with pytest.raises(PreservationError, match="future_output"):
        parse(export, Identity("opencode", "ses_native"))


def test_wal_database_with_absent_sidecars_stays_untouched(roots):
    seed(roots, ordinary_export(roots))
    path = roots.store / "opencode.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.close()
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in roots.store.iterdir()}
    assert set(before) == {"opencode.db"}
    adapter = OpenCodeAdapter()
    assert adapter.inspect_target(roots, None).supported
    source = adapter.snapshot(
        SourceRequest(Identity("opencode", "ses_native"), roots, "opencode.db")
    )
    assert source.conversation.history[0].content == (Text("Read the full file"),)
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in roots.store.iterdir()} == before


def test_uncheckpointed_wal_content_is_included_without_source_mutation(roots):
    seed(roots, ordinary_export(roots))
    path = roots.store / "opencode.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE session SET title='uncheckpointed title' WHERE id='ses_native'")
    writer.commit()
    try:
        before = {p.name: p.read_bytes() for p in roots.store.iterdir()}
        assert "opencode.db-wal" in before
        assert snapshot(roots).conversation.title == "uncheckpointed title"
        assert {p.name: p.read_bytes() for p in roots.store.iterdir()} == before
    finally:
        writer.close()


def test_native_append_remains_readable_after_registration(roots):
    result = stage(roots)
    export = _payload_export(decode(result.database_payload))
    extra = ordinary_export(roots)["messages"][0]
    extra["info"].update(
        id="msg_new_native_turn",
        sessionID=result.identity.native_id,
        time={"created": export["info"]["time"]["created"] + 1},
    )
    extra["parts"][0].update(
        id="prt_new_native_turn",
        sessionID=result.identity.native_id,
        messageID="msg_new_native_turn",
        text="A new native question",
    )
    export["messages"].append(extra)
    value = parse(export, result.identity)
    assert value.history[-1].content == (Text("A new native question"),)
    assert value.context[-1].content == (Text("A new native question"),)
    assert value.branch_id == "msg_new_native_turn"


def test_native_tool_and_message_timestamps_are_verified(roots):
    original = ordinary_export(roots)
    original["messages"][1]["parts"][1]["state"]["time"] = {"start": 10, "end": 20}
    value = parse(original, Identity("opencode", "ses_native"))
    adapter = OpenCodeAdapter()
    result = adapter.stage(
        value, TargetRequest(adapter.allocate_identity(), roots, PROFILE, value.title)
    )
    export = _payload_export(decode(result.database_payload))
    tool = next(p for row in export["messages"] for p in row["parts"] if p["type"] == "tool")
    assert tool["state"]["time"] == {"start": 10, "end": 20}
    tool["state"]["time"]["start"] = 11
    with pytest.raises(PreservationError, match="original tool timestamps"):
        parse(export, result.identity)
    export = _payload_export(decode(result.database_payload))
    export["messages"][0]["info"]["time"]["created"] = 100
    with pytest.raises(PreservationError, match="historical timestamp"):
        parse(export, result.identity)


@pytest.mark.parametrize("status", ["completed", "error"])
@pytest.mark.parametrize(
    "metadata",
    [
        {"output": "VISIBLE METADATA OUTPUT"},
        {"filediff": {"patch": "@@ -1 +1 @@\n-old\n+new", "before": "old", "after": "new"}},
        {"diagnostics": {"a.py": [{"message": "type error"}]}},
        {"files": [{"filePath": "a.py", "before": "old", "after": "new"}]},
        {"answers": [["user-selected answer"]]},
        {"future_conversation": "must not disappear"},
    ],
)
def test_tool_state_metadata_requires_proven_presentation_semantics(roots, status, metadata):
    export = ordinary_export(roots)
    tool = export["messages"][1]["parts"][1]["state"]
    tool["status"] = status
    tool["metadata"] = metadata
    if status == "error":
        tool.pop("output")
        tool["error"] = ""
    else:
        tool["output"] = ""
    seed(roots, export)
    before = {p.name: p.read_bytes() for p in roots.store.iterdir()}
    with pytest.raises(PreservationError, match=r"tool-state metadata.*visible/context"):
        snapshot(roots)
    assert {p.name: p.read_bytes() for p in roots.store.iterdir()} == before


@pytest.mark.parametrize(
    "location",
    [
        "home-json",
        "home-jsonc",
        "build-model",
        "default-agent",
        "legacy-mode",
        "global-agent-file",
        "home-agent-file",
        "project-agent-file",
    ],
)
def test_native_model_override_sources_fail_at_inspection_and_staging(roots, location):
    adapter = OpenCodeAdapter()
    ready = adapter.inspect_target(roots, None)
    target = TargetRequest(
        adapter.allocate_identity(), roots, PROFILE, "Portable example", ready.target_configuration
    )
    config = roots.config / "opencode" / "opencode.json"
    data = json.loads(config.read_text())
    if location.startswith("home-json"):
        directory = roots.home / ".opencode"
        directory.mkdir()
        (
            directory / ("opencode.jsonc" if location.endswith("jsonc") else "opencode.json")
        ).write_text('{"model":"openai/home-override"}')
    elif location == "build-model":
        data["agent"] = {"build": {"model": "openai/agent-override"}}
    elif location == "default-agent":
        data["default_agent"] = "custom"
    elif location == "legacy-mode":
        data["mode"] = {"build": {"model": "openai/mode-override"}}
    else:
        directory = {
            "global-agent-file": roots.config / "opencode",
            "home-agent-file": roots.home / ".opencode",
            "project-agent-file": roots.cwd / ".opencode",
        }[location] / "agents"
        directory.mkdir(parents=True)
        (directory / "build.md").write_text("---\nmodel: openai/file-override\n---\nCustom agent")
    config.write_text(json.dumps(data))
    before = {p.name: p.read_bytes() for p in roots.store.iterdir()}
    inspected = adapter.inspect_target(roots, None)
    assert inspected.supported is False
    assert any(
        "config" in diagnostic or "definitions" in diagnostic
        for diagnostic in inspected.diagnostics
    )
    with pytest.raises(PreservationError, match=r"(config|definitions)"):
        adapter.stage(shared_conversation(), target)
    assert {p.name: p.read_bytes() for p in roots.store.iterdir()} == before
