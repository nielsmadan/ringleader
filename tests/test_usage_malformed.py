import json
import sqlite3

from ringleader.usage.claude import ClaudeUsageReader
from ringleader.usage.codex import CodexUsageReader
from ringleader.usage.opencode import OpenCodeUsageReader
from ringleader.usage.pi import PiUsageReader


def _write(dir_, project, session, records):
    d = dir_ / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def test_claude_scalar_message_and_usage_do_not_crash(tmp_path):
    _write(
        tmp_path,
        "-Users-x-wrksp-foo",
        "sess1",
        [
            {"type": "assistant", "timestamp": "2026-08-17T10:00:00.000Z", "message": "oops"},
            {
                "type": "assistant",
                "timestamp": "2026-08-17T10:00:01.000Z",
                "message": {"model": "claude-opus-5", "usage": 5},
            },
        ],
    )

    assert list(ClaudeUsageReader(projects_dir=tmp_path).records()) == []


def test_claude_non_numeric_token_field_degrades_to_zero(tmp_path):
    _write(
        tmp_path,
        "-Users-x-wrksp-foo",
        "sess1",
        [
            {
                "type": "assistant",
                "requestId": "r1",
                "timestamp": "2026-08-17T10:00:00.000Z",
                "message": {
                    "id": "m1",
                    "model": "claude-opus-5",
                    "usage": {"input_tokens": "lots", "output_tokens": 20},
                },
            }
        ],
    )

    r = next(iter(ClaudeUsageReader(projects_dir=tmp_path).records()))

    assert r.input == 0
    assert r.output == 20


def _codex_write(dir_, records):
    d = dir_ / "2026" / "08" / "17"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "rollout-2026-08-17T23-06-19-01a0118b-f3a9-7453-af81-02d2d94a4e64.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def test_codex_scalar_payload_does_not_crash(tmp_path):
    _codex_write(
        tmp_path,
        [
            {
                "type": "event_msg",
                "timestamp": "2026-08-17T21:06:26.967Z",
                "note": "token_count",  # keeps the line-level substring prefilter happy
                "payload": 3,
            }
        ],
    )

    reader = CodexUsageReader(sessions_dir=tmp_path)
    assert list(reader.records()) == []
    assert list(reader.windows()) == []


def test_codex_scalar_rate_limits_does_not_crash(tmp_path):
    _codex_write(
        tmp_path,
        [
            {
                "type": "event_msg",
                "timestamp": "2026-08-17T21:06:26.967Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 0,
                            "output_tokens": 10,
                        }
                    },
                    "rate_limits": "bad",
                },
            }
        ],
    )

    reader = CodexUsageReader(sessions_dir=tmp_path)
    assert len(list(reader.records())) == 1
    assert list(reader.windows()) == []


def _oc_write(
    tmp_path,
    *,
    time_updated,
    tokens_input=0,
    tokens_output=0,
    cost=0,
):
    db = tmp_path / "opencode.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL);
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
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
    connection.execute("INSERT INTO project VALUES (?, ?)", ("opaque", "/repo"))
    connection.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ses_1",
            "opaque",
            "/repo",
            time_updated,
            time_updated,
            None,
            cost,
            tokens_input,
            tokens_output,
            0,
            0,
            0,
        ),
    )
    connection.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        (
            "msg_1",
            "ses_1",
            time_updated,
            time_updated,
            json.dumps(
                {
                    "role": "assistant",
                    "cost": cost,
                    "tokens": {
                        "input": tokens_input,
                        "output": tokens_output,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                }
            ),
        ),
    )
    connection.commit()
    connection.close()
    return db


def test_opencode_scalar_tokens_and_bad_time_do_not_crash(tmp_path):
    db = _oc_write(tmp_path, time_updated="bad", tokens_input="many", tokens_output="many")

    assert list(OpenCodeUsageReader(db_path=db).records()) == []


def test_opencode_scalar_time_with_valid_tokens_degrades_timestamp(tmp_path):
    db = _oc_write(
        tmp_path,
        time_updated="bad",
        tokens_input="many",
        tokens_output=5,
        cost="expensive",
    )

    r = next(iter(OpenCodeUsageReader(db_path=db).records()))

    assert r.input == 0
    assert r.output == 5
    assert r.timestamp == ""
    assert r.cost is None


def _pi_write(sessions, project, records):
    d = sessions / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / "2026-07-25T22-20-34-239Z_019f9b5d-a83f-7f7f-aae9-b65fc6f1a65a.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_pi_scalar_usage_and_scalar_cost_do_not_crash(tmp_path):
    _pi_write(
        tmp_path,
        "--Users-x-wrksp-splash--",
        [
            {
                "type": "message",
                "timestamp": "2026-07-25T22:21:13.315Z",
                "message": {"role": "assistant", "usage": 2},
            },
            {
                "type": "message",
                "timestamp": "2026-07-25T22:21:14.315Z",
                "message": {
                    "role": "assistant",
                    "usage": {"input": 5, "output": 1, "cost": 0.05},
                },
            },
        ],
    )

    recs = list(PiUsageReader(sessions_dir=tmp_path).records())

    assert len(recs) == 1
    assert recs[0].input == 5
    assert recs[0].cost is None  # cost was a bare float, not {"total": ...}
