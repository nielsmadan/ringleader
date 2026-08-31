import json
import sqlite3

import pytest
from ringleader.opencode_db import connect_read_only
from ringleader.usage.claude import ClaudeUsageReader
from ringleader.usage.opencode import OpenCodeUsageReader
from ringleader.usage.pi import PiUsageReader

PI_SESSION = "019f9b5d-a83f-7f7f-aae9-b65fc6f1a65a"


def _oc_db(tmp_path):
    path = tmp_path / "opencode.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL);
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            parent_id TEXT,
            directory TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            model TEXT,
            cost REAL NOT NULL,
            tokens_input INTEGER NOT NULL,
            tokens_output INTEGER NOT NULL,
            tokens_reasoning INTEGER NOT NULL,
            tokens_cache_read INTEGER NOT NULL,
            tokens_cache_write INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    connection.execute("INSERT INTO project VALUES (?, ?)", ("opaque-project-id", "/repo"))
    connection.commit()
    connection.close()
    return path


def _oc_session(
    path,
    session="ses_1",
    *,
    project_id="opaque-project-id",
    parent_id=None,
    model=None,
    tokens=(26865, 275, 5, 100, 20),
):
    model = model or json.dumps({"id": "kimi-k2.5", "providerID": "opencode", "variant": "default"})
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session,
            project_id,
            parent_id,
            "/repo/worktree",
            1770998000000,
            1770998211275,
            model,
            0.25,
            *tokens,
        ),
    )
    connection.commit()
    connection.close()
    try:
        parsed_model = json.loads(model)
    except json.JSONDecodeError:
        parsed_model = {}
    _oc_message(
        path,
        session,
        f"msg_{session}",
        timestamp=1770998211275,
        model=(
            parsed_model.get("id") if parsed_model else model,
            parsed_model.get("providerID"),
        ),
        tokens=tokens,
    )


def _oc_message(
    path,
    session,
    message,
    *,
    timestamp,
    model=("kimi-k2.5", "opencode"),
    tokens=(1, 2, 0, 0, 0),
):
    inp, output, reasoning, cache_read, cache_write = tokens
    model_id, provider_id = model
    body = {
        "role": "assistant",
        "time": {"created": timestamp},
        "modelID": model_id,
        "providerID": provider_id,
        "cost": 0.25,
        "tokens": {
            "input": inp,
            "output": output,
            "reasoning": reasoning,
            "cache": {"read": cache_read, "write": cache_write},
        },
    }
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        (message, session, timestamp, timestamp, json.dumps(body)),
    )
    connection.commit()
    connection.close()


def test_opencode_maps_buckets_and_uses_on_disk_cost(tmp_path):
    db = _oc_db(tmp_path)
    _oc_session(db)

    recs = list(OpenCodeUsageReader(db_path=db).records())

    assert len(recs) == 1
    r = recs[0]
    assert (r.input, r.output, r.cache_read, r.cache_write) == (26865, 280, 100, 20)
    assert r.reasoning == 5
    assert r.total == 26865 + 280 + 100 + 20
    assert r.cost == 0.25
    assert r.source == "opencode" and r.session_id == "ses_1"
    assert r.model == "kimi-k2.5" and r.provider == "opencode"
    assert r.timestamp.startswith("2026-")  # epoch ms -> ISO

    assert r.record_id == "msg_ses_1"


def test_opencode_reasoning_is_counted_when_it_exceeds_visible_output(tmp_path):
    db = _oc_db(tmp_path)
    _oc_session(db, tokens=(1, 2, 9, 0, 0))

    record = next(OpenCodeUsageReader(db_path=db).records())

    assert record.output == 11
    assert record.reasoning == 9
    assert record.total == 12


def test_opencode_emits_immutable_per_message_records(tmp_path):
    db = _oc_db(tmp_path)
    _oc_session(db, tokens=(0, 0, 0, 0, 0))
    _oc_message(
        db,
        "ses_1",
        "msg_first",
        timestamp=1770940800000,
        model=("model-a", "opencode"),
        tokens=(3, 2, 0, 0, 0),
    )
    _oc_message(
        db,
        "ses_1",
        "msg_second",
        timestamp=1771027200000,
        model=("model-b", "opencode"),
        tokens=(5, 4, 0, 0, 0),
    )

    records = list(OpenCodeUsageReader(db_path=db).records())

    assert [record.record_id for record in records] == ["msg_first", "msg_second"]
    assert [record.timestamp[:10] for record in records] == ["2026-02-13", "2026-02-14"]
    assert [record.model for record in records] == ["model-a", "model-b"]
    assert [record.total for record in records] == [5, 9]


def test_opencode_skips_zero_usage_and_degrades_malformed_model(tmp_path):
    db = _oc_db(tmp_path)
    _oc_session(db, "ses_zero", tokens=(0, 0, 0, 0, 0))
    _oc_session(db, "ses_used", model="not-json", tokens=(5, 0, 0, 0, 0))

    records = list(OpenCodeUsageReader(db_path=db).records())

    assert [record.session_id for record in records] == ["ses_used"]
    assert records[0].model == "not-json"
    assert records[0].provider is None


def test_opencode_includes_child_session_usage(tmp_path):
    db = _oc_db(tmp_path)
    _oc_session(db, "ses_parent", tokens=(5, 0, 0, 0, 0))
    _oc_session(db, "ses_child", parent_id="ses_parent", tokens=(7, 0, 0, 0, 0))

    records = list(OpenCodeUsageReader(db_path=db).records())

    assert {record.session_id: record.total for record in records} == {
        "ses_parent": 5,
        "ses_child": 7,
    }


def test_opencode_available_is_false_without_the_directory(tmp_path):
    assert OpenCodeUsageReader(db_path=tmp_path / "nope.db").available() is False


def test_opencode_available_is_false_for_wrong_schema(tmp_path):
    db = tmp_path / "opencode.db"
    sqlite3.connect(db).close()

    reader = OpenCodeUsageReader(db_path=db)

    assert reader.available() is False
    assert list(reader.records()) == []


def test_opencode_available_is_false_when_database_is_locked(tmp_path):
    db = _oc_db(tmp_path)
    lock = sqlite3.connect(db)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        assert OpenCodeUsageReader(db_path=db).available() is False
    finally:
        lock.rollback()
        lock.close()


def test_opencode_connection_cannot_mutate_the_database(tmp_path):
    db = _oc_db(tmp_path)
    connection = connect_read_only(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM project")
    finally:
        connection.close()


def _pi_session(sessions, project, records):
    d = sessions / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"2026-07-25T22-20-34-239Z_{PI_SESSION}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _pi_assistant(**usage):
    u = {
        "input": 22338,
        "output": 74,
        "cacheRead": 10,
        "cacheWrite": 5,
        "reasoning": 15,
        "totalTokens": 22412,
        "cost": {"total": 0.056955},
    }
    u.update(usage)
    return {
        "type": "message",
        "timestamp": "2026-07-25T22:21:13.315Z",
        "message": {
            "role": "assistant",
            "model": "gpt-5.6-terra",
            "provider": "openai-codex",
            "usage": u,
        },
    }


def test_pi_maps_buckets_and_cost(tmp_path):
    _pi_session(tmp_path, "--Users-x-wrksp-splash--", [_pi_assistant()])

    recs = list(PiUsageReader(sessions_dir=tmp_path).records())

    assert len(recs) == 1
    r = recs[0]
    assert (r.input, r.output, r.cache_read, r.cache_write) == (22338, 74, 10, 5)
    assert r.reasoning == 15
    assert r.total == 22338 + 74 + 10 + 5
    assert r.cost == 0.056955
    assert r.source == "pi" and r.session_id == PI_SESSION
    assert r.model == "gpt-5.6-terra" and r.provider == "openai-codex"


def test_pi_skips_records_without_usage(tmp_path):
    _pi_session(
        tmp_path,
        "--Users-x-wrksp-splash--",
        [{"type": "message", "message": {"role": "user", "content": "hi"}}],
    )

    assert list(PiUsageReader(sessions_dir=tmp_path).records()) == []


def test_pi_missing_fields_degrade_to_zero(tmp_path):
    _pi_session(
        tmp_path,
        "--Users-x-wrksp-splash--",
        [
            {
                "type": "message",
                "timestamp": "2026-07-25T22:21:13.315Z",
                "message": {"role": "assistant", "usage": {"input": 5}},
            }
        ],
    )

    r = next(iter(PiUsageReader(sessions_dir=tmp_path).records()))

    assert r.input == 5 and r.output == 0 and r.total == 5 and r.cost is None


def test_opencode_project_is_joined_through_the_opaque_project_id(tmp_path):
    db = _oc_db(tmp_path)
    _oc_session(db)

    r = next(iter(OpenCodeUsageReader(db_path=db).records()))

    assert r.project == "/repo"


def test_opencode_project_falls_back_to_session_directory_for_missing_project(tmp_path):
    db = _oc_db(tmp_path)
    _oc_session(db, project_id="missing")

    assert next(iter(OpenCodeUsageReader(db_path=db).records())).project == "/repo/worktree"


def test_pi_project_is_an_absolute_path(tmp_path):
    _pi_session(tmp_path, "--Users-x-wrksp-splash--", [_pi_assistant()])

    r = next(iter(PiUsageReader(sessions_dir=tmp_path).records()))

    assert r.project == "/Users/x/wrksp/splash"
    assert r.record_id.endswith("|0")


def test_one_project_gets_one_label_across_readers(tmp_path):
    _pi_session(tmp_path / "pi", "--Users-x-wrksp-foo--", [_pi_assistant()])
    claude_dir = tmp_path / "claude" / "-Users-x-wrksp-foo"
    claude_dir.mkdir(parents=True)
    (claude_dir / "sess1.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "requestId": "r1",
                "timestamp": "2026-08-17T10:00:00.000Z",
                "message": {
                    "id": "m1",
                    "model": "claude-opus-5",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    pi = next(iter(PiUsageReader(sessions_dir=tmp_path / "pi").records()))
    claude = next(iter(ClaudeUsageReader(projects_dir=tmp_path / "claude").records()))

    assert pi.project == claude.project == "/Users/x/wrksp/foo"
