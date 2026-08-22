import json

from ringleader.usage.claude import ClaudeUsageReader
from ringleader.usage.opencode import OpenCodeUsageReader
from ringleader.usage.pi import PiUsageReader

PI_SESSION = "019f9b5d-a83f-7f7f-aae9-b65fc6f1a65a"


def _oc_msg(storage, session, msg_id, *, role="assistant", tokens=None, cost=0.25):
    d = storage / "message" / session
    d.mkdir(parents=True, exist_ok=True)
    body = {
        "id": msg_id,
        "sessionID": session,
        "role": role,
        "time": {"created": 1770998211275},
        "modelID": "kimi-k2.5",
        "providerID": "opencode",
        "cost": cost,
        "tokens": tokens
        if tokens is not None
        else {
            "total": 27140,
            "input": 26865,
            "output": 275,
            "reasoning": 5,
            "cache": {"read": 100, "write": 20},
        },
    }
    (d / f"{msg_id}.json").write_text(json.dumps(body), encoding="utf-8")


def test_opencode_maps_buckets_and_uses_on_disk_cost(tmp_path):
    _oc_msg(tmp_path, "ses_1", "msg_1")

    recs = list(OpenCodeUsageReader(storage_dir=tmp_path).records())

    assert len(recs) == 1
    r = recs[0]
    assert (r.input, r.output, r.cache_read, r.cache_write) == (26865, 275, 100, 20)
    assert r.reasoning == 5
    assert r.total == 26865 + 275 + 100 + 20
    assert r.cost == 0.25
    assert r.source == "opencode" and r.session_id == "ses_1"
    assert r.model == "kimi-k2.5" and r.provider == "opencode"
    assert r.timestamp.startswith("2026-")  # epoch ms -> ISO


def test_opencode_skips_user_messages_and_bad_json(tmp_path):
    _oc_msg(tmp_path, "ses_1", "msg_user", role="user")
    (tmp_path / "message" / "ses_1" / "broken.json").write_text("{nope", encoding="utf-8")

    assert list(OpenCodeUsageReader(storage_dir=tmp_path).records()) == []


def test_opencode_available_is_false_without_the_directory(tmp_path):
    assert OpenCodeUsageReader(storage_dir=tmp_path / "nope").available() is False


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


def _oc_session(storage, session, directory, project="proj1"):
    d = storage / "session" / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session}.json").write_text(
        json.dumps({"id": session, "directory": directory}), encoding="utf-8"
    )


def test_opencode_project_comes_from_the_session_record(tmp_path):
    _oc_msg(tmp_path, "ses_1", "msg_1")
    _oc_session(tmp_path, "ses_1", "/Users/x/wrksp/foo")

    r = next(iter(OpenCodeUsageReader(storage_dir=tmp_path).records()))

    assert r.project == "/Users/x/wrksp/foo"
    assert r.record_id == "msg_1"


def test_opencode_project_is_none_without_a_session_record(tmp_path):
    _oc_msg(tmp_path, "ses_1", "msg_1")

    assert next(iter(OpenCodeUsageReader(storage_dir=tmp_path).records())).project is None


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
