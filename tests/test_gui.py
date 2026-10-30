import json
from datetime import UTC, datetime, timedelta

import pytest
from ringleader import contract
from ringleader.decisions import Outcome
from ringleader.revalidate import Seen

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)
NOW = NOW_DT.isoformat()

TAIL_ITEM_KEYS = {
    "id",
    "text",
    "max_chars",
    "truncated",
    "unavailable",
    "mtime",
    "file_fingerprint",
}


def _ago(days: float = 0, hours: float = 0) -> str:
    return (NOW_DT - timedelta(days=days, hours=hours)).isoformat()


def _thread(sid, *, days=30.0, **extra):
    return {
        "sessionId": sid,
        "source": "claude-code",
        "project": "/w/proj",
        "mtime": _ago(days),
        "status": "unclassified",
        "decision": None,
        **extra,
    }


def _woke(sid):
    return _thread(sid, days=20, snoozedAt=_ago(9), snoozedUntil=_ago(1))


def _reg(threads, **extra):
    return {"threads": threads, "lastRoundupAt": None, **extra}


def _ids(threads):
    return [t["sessionId"] for t in threads]


def test_session_row_has_exactly_the_contract_keys():
    row = contract.session_row(_thread("a", title="named"), NOW, frozenset())
    assert set(row) == set(contract.SESSION_ROW_KEYS)
    assert list(contract.SESSION_ROW_KEYS) == sorted(contract.SESSION_ROW_KEYS)


def test_session_row_maps_registry_fields():
    titled = contract.session_row(_thread("a", title="named"), NOW, frozenset())
    ai = contract.session_row(_thread("b", aiTitle="ai named"), NOW, frozenset())
    git = contract.session_row(_thread("c", gitProjectLabel="juggler"), NOW, frozenset())
    assert (titled["label"], ai["label"], git["project_label"]) == ("named", "ai named", "juggler")
    assert titled["id"] == "claude-code:a"
    assert (titled["source"], titled["agent"]) == ("claude-code", "claude")
    assert titled["message_count"] == 0


def test_session_row_clips_long_text():
    row = contract.session_row(_thread("a", lastPrompt="x" * 2000), NOW, frozenset())
    assert len(row["last_prompt"]) == contract.LIST_TEXT_MAX_CHARS
    assert row["last_prompt"].endswith("…")


def test_clip_keeps_none_and_short_text():
    assert contract.clip(None, 5) is None
    assert contract.clip("short", 5) == "short"
    assert contract.clip("longer", 5) == "long…"


def test_is_running_reads_the_live_set():
    thread = _thread("s1")
    assert contract.is_running(thread, frozenset({("claude", "s1")})) is True
    assert contract.is_running(thread, frozenset({("claude", "other")})) is False
    assert contract.is_running(thread, None) is None


def test_lifecycle_block_names_sources_without_a_filesystem_lifecycle():
    block = contract.lifecycle_block(_thread("o", source="opencode", file="/x/o.json"))
    assert block is not None
    assert "no filesystem transcript lifecycle" in block


def test_inbox_threads_selects_and_orders_the_triage_inbox():
    threads = [
        _thread("recent", days=1),
        _woke("woke"),
        _thread("oldest", days=60),
        _thread("kept", decision="keep"),
        _thread("harvested", decision="harvested"),
        _thread("reviewed", reviewedAtMtime=_ago(30)),
        _thread(
            "snoozed", snoozedAt=_ago(2), snoozedUntil=(NOW_DT + timedelta(days=3)).isoformat()
        ),
        _thread("hidden", filteredBy="zz*"),
    ]
    assert _ids(contract.inbox_threads(_reg(threads), NOW)) == ["oldest", "woke", "recent"]


def test_inbox_threads_breaks_mtime_ties_by_qualified_id():
    threads = [_thread("b"), _thread("a", source="codex"), _thread("a")]
    ordered = contract.inbox_threads(_reg(threads), NOW)
    assert [(t.get("source"), t["sessionId"]) for t in ordered] == [
        ("claude-code", "a"),
        ("claude-code", "b"),
        ("codex", "a"),
    ]


NO_ONE_LIVE = contract.PresenceView(frozenset(), None)


def _payload(reg, presence=NO_ONE_LIVE, stale_hours=8):
    return contract.list_payload(
        reg,
        NOW,
        registry_path="/fixture/registry.json",
        presence=presence,
        stale_hours=stale_hours,
        scan=None,
    )


def test_list_payload_flags_recent_and_woke_rows():
    doc = _payload(_reg([_thread("recent", days=1), _woke("woke"), _thread("old", days=60)]))
    rows = {row["session_id"]: row for row in doc["sessions"]}
    assert (doc["version"], doc["kind"], doc["registry"]) == (1, "list", "/fixture/registry.json")
    assert doc["generated_at"] == NOW
    assert doc["recent_days"] == 7
    assert [rows[s]["in_recent"] for s in ("recent", "woke", "old")] == [True, False, False]
    assert [rows[s]["woke"] for s in ("recent", "woke", "old")] == [False, True, False]
    assert doc["presence"] == {"state": "ok", "problem": None}
    assert doc["scan"] is None


def test_list_payload_reports_unknown_presence():
    doc = _payload(_reg([_thread("a"), _thread("b")]), presence=contract.PresenceView(None, "x"))
    assert [row["running"] for row in doc["sessions"]] == [None, None]
    assert doc["presence"] == {"state": "unknown", "problem": "x"}


def test_list_payload_reports_roundup_staleness():
    stale = _payload(_reg([], lastRoundupAt=_ago(hours=10)))["roundup"]
    fresh = _payload(_reg([], lastRoundupAt=_ago(hours=1)))["roundup"]
    assert stale == {
        "last_at": _ago(hours=10),
        "age_human": "10h ago",
        "stale": True,
        "stale_hours": 8,
    }
    assert fresh["stale"] is False


def test_list_payload_without_a_roundup_has_no_age():
    roundup = _payload(_reg([]))["roundup"]
    assert roundup == {"last_at": None, "age_human": None, "stale": True, "stale_hours": 8}


def test_scan_summary_counts_membership_changes():
    a, b, c = ("claude-code", "a"), ("claude-code", "b"), ("claude-code", "c")
    summary = contract.scan_summary(NOW, {a, b}, {b, c}, frozenset({"pi", "codex"}))
    assert summary == {
        "scanned_at": NOW,
        "added": 1,
        "removed": 1,
        "unavailable_sources": ["codex", "pi"],
    }


def test_list_payload_carries_the_scan_summary_it_was_given():
    summary = contract.scan_summary(NOW, set(), set(), frozenset())
    doc = contract.list_payload(
        _reg([]),
        NOW,
        registry_path="/fixture/registry.json",
        presence=NO_ONE_LIVE,
        stale_hours=8,
        scan=summary,
    )
    assert doc["scan"] == {"scanned_at": NOW, "added": 0, "removed": 0, "unavailable_sources": []}


def test_encode_document_replaces_lone_surrogates():
    encoded = contract.encode_document({"t": "a\ud83db"})
    assert json.loads(encoded.decode("utf-8")) == {"t": "a?b"}
    assert encoded.endswith(b"\n")


def test_tail_item_carries_the_thread_stamp():
    thread = _thread("a", fileFingerprint="1:2:3:4:5")
    item = contract.tail_item("claude-code:a", thread, "xyz", 1000, None)
    assert set(item) == TAIL_ITEM_KEYS
    assert item == {
        "id": "claude-code:a",
        "text": "xyz",
        "max_chars": 1000,
        "truncated": False,
        "unavailable": None,
        "mtime": thread["mtime"],
        "file_fingerprint": "1:2:3:4:5",
    }


def test_tail_item_for_a_missing_session_is_empty():
    item = contract.tail_item("claude-code:a", None, None, 1000, "no longer in the registry")
    assert (item["text"], item["mtime"], item["file_fingerprint"]) == ("", None, None)
    assert item["unavailable"] == "no longer in the registry"


def test_tail_item_marks_a_full_window_as_truncated():
    item = contract.tail_item("claude-code:a", _thread("a"), "x" * 1000, 1000, None)
    assert item["truncated"] is True


def test_tails_and_error_payloads():
    items = [contract.tail_item("claude-code:a", None, None, 1000, "no longer in the registry")]
    tails = contract.tails_payload(items)
    assert set(tails) == {"version", "kind", "items"}
    assert (tails["kind"], tails["items"]) == ("tails", items)
    assert contract.error_payload("boom") == {"version": 1, "kind": "error", "message": "boom"}


def test_normalize_until_converts_an_offset_to_utc():
    assert (
        contract.normalize_until("2026-10-20T09:00:00+02:00", NOW_DT) == "2026-10-20T07:00:00+00:00"
    )


def test_normalize_until_accepts_a_zulu_suffix():
    assert contract.normalize_until("2026-10-20T09:00:00Z", NOW_DT) == "2026-10-20T09:00:00+00:00"


def test_normalize_until_rejects_a_naive_time():
    with pytest.raises(ValueError, match="timezone offset"):
        contract.normalize_until("2026-10-20T09:00:00", NOW_DT)


def test_normalize_until_rejects_a_past_time():
    with pytest.raises(ValueError, match="in the future"):
        contract.normalize_until("2026-10-14T09:00:00+00:00", NOW_DT)


def test_normalize_until_rejects_text_that_is_not_a_time():
    with pytest.raises(ValueError, match="not an ISO 8601 time"):
        contract.normalize_until("next tuesday", NOW_DT)


SEEN = Seen(("claude-code", "s1"), "s1-title", NOW, None)


def test_from_outcome_reports_the_first_refusal_reason():
    outcome = Outcome(
        ok=False,
        message="s1-title: changed since you looked",
        wrote=False,
        refused=((SEEN, "changed since you looked"),),
    )
    result = contract.from_outcome(outcome)
    assert (result.status, result.reason) == ("refused", "changed since you looked")


def test_from_outcome_reports_a_partial_write_as_failed():
    outcome = Outcome(ok=False, message="archived x on disk, but …", wrote=True, path="/a/x")
    result = contract.from_outcome(outcome)
    assert (result.status, result.wrote, result.path) == ("failed", True, "/a/x")


def test_from_outcome_copies_an_ok_outcome():
    outcome = Outcome(ok=True, message="archived x", wrote=True, warnings=("w",), path="/a/x")
    assert contract.from_outcome(outcome) == contract.GuiOutcome(
        "ok", None, "archived x", True, "/a/x", ("w",)
    )


def test_refused_defaults_its_message_to_the_reason():
    assert contract.refused("gone") == contract.GuiOutcome("refused", "gone", "gone")


def test_outcome_payload_has_exactly_the_contract_keys():
    doc = contract.outcome_payload(
        "archive", "claude-code:s1", contract.GuiOutcome("ok", None, "m", True)
    )
    assert set(doc) == {
        "version",
        "kind",
        "action",
        "id",
        "status",
        "reason",
        "message",
        "wrote",
        "path",
        "warnings",
    }
    assert (doc["kind"], doc["action"], doc["warnings"]) == ("outcome", "archive", [])
