import json

from ringleader.usage.base import UsageRecord
from ringleader.usage.transfer import dedupe, load_exports, machine_id, write_export


def _rec(session="s1", ts="2026-08-17T10:00:00Z", machine=None, inp=100, record_id=None):
    return UsageRecord(
        source="claude",
        session_id=session,
        project="p",
        timestamp=ts,
        provider=None,
        model="claude-opus-5",
        input=inp,
        output=0,
        cache_read=0,
        cache_write=0,
        reasoning=0,
        total=inp,
        machine=machine,
        record_id=record_id,
    )


def test_machine_id_is_stable():
    assert machine_id() == machine_id()
    assert machine_id()


def test_write_and_load_roundtrip(tmp_path):
    p = write_export([_rec()], tmp_path / "laptop.json", machine="laptop")

    body = json.loads(p.read_text(encoding="utf-8"))
    assert body["machine"] == "laptop" and len(body["records"]) == 1

    loaded = load_exports(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].machine == "laptop"
    assert loaded[0].total == 100


def test_load_ignores_non_export_json(tmp_path):
    (tmp_path / "junk.json").write_text(json.dumps({"nope": 1}), encoding="utf-8")
    assert load_exports(tmp_path) == []


def test_dedupe_collapses_same_machine_session_timestamp():
    a = _rec(machine="laptop")
    b = _rec(machine="laptop")
    c = _rec(machine="desktop")
    assert len(dedupe([a, b, c])) == 2


def test_dedupe_treats_local_none_as_this_machine():
    local = _rec(machine=None)
    reimported = _rec(machine=machine_id())
    assert len(dedupe([local, reimported])) == 1


def test_dedupe_keeps_distinct_turns_that_share_a_timestamp():
    a = _rec(record_id="rollout.jsonl|4")
    b = _rec(record_id="rollout.jsonl|5")
    assert len(dedupe([a, b])) == 2


def test_dedupe_collapses_a_reimported_export_of_this_machine():
    local = _rec(record_id="rollout.jsonl|4")
    reimported = _rec(record_id="rollout.jsonl|4", machine=machine_id())
    assert len(dedupe([local, reimported])) == 1


def test_dedupe_falls_back_to_the_composite_key_without_a_record_id():
    assert len(dedupe([_rec(machine="laptop"), _rec(machine="laptop")])) == 1


def test_record_id_survives_the_export_roundtrip(tmp_path):
    write_export([_rec(record_id="rollout.jsonl|4")], tmp_path / "laptop.json", machine="laptop")
    assert load_exports(tmp_path)[0].record_id == "rollout.jsonl|4"


def test_load_accepts_an_export_written_before_record_id_existed(tmp_path):
    body = {
        "version": 1,
        "machine": "laptop",
        "records": [
            {
                "source": "claude",
                "session_id": "s1",
                "project": "p",
                "timestamp": "2026-08-17T10:00:00Z",
                "provider": None,
                "model": "claude-opus-5",
                "input": 1,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "reasoning": 0,
                "total": 1,
            }
        ],
    }
    (tmp_path / "old.json").write_text(json.dumps(body), encoding="utf-8")

    loaded = load_exports(tmp_path)

    assert len(loaded) == 1
    assert loaded[0].record_id is None
