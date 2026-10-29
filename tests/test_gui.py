import json
from datetime import UTC, datetime, timedelta

from ringleader import gui

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
    row = gui.session_row(_thread("a", title="named"), NOW, frozenset())
    assert set(row) == set(gui.SESSION_ROW_KEYS)
    assert list(gui.SESSION_ROW_KEYS) == sorted(gui.SESSION_ROW_KEYS)


def test_session_row_maps_registry_fields():
    titled = gui.session_row(_thread("a", title="named"), NOW, frozenset())
    ai = gui.session_row(_thread("b", aiTitle="ai named"), NOW, frozenset())
    git = gui.session_row(_thread("c", gitProjectLabel="juggler"), NOW, frozenset())
    assert (titled["label"], ai["label"], git["project_label"]) == ("named", "ai named", "juggler")
    assert titled["id"] == "claude-code:a"
    assert (titled["source"], titled["agent"]) == ("claude-code", "claude")
    assert titled["message_count"] == 0


def test_session_row_clips_long_text():
    row = gui.session_row(_thread("a", lastPrompt="x" * 2000), NOW, frozenset())
    assert len(row["last_prompt"]) == gui.LIST_TEXT_MAX_CHARS
    assert row["last_prompt"].endswith("…")


def test_clip_keeps_none_and_short_text():
    assert gui.clip(None, 5) is None
    assert gui.clip("short", 5) == "short"
    assert gui.clip("longer", 5) == "long…"


def test_is_running_reads_the_live_set():
    thread = _thread("s1")
    assert gui.is_running(thread, frozenset({("claude", "s1")})) is True
    assert gui.is_running(thread, frozenset({("claude", "other")})) is False
    assert gui.is_running(thread, None) is None


def test_lifecycle_block_names_sources_without_a_filesystem_lifecycle():
    block = gui.lifecycle_block(_thread("o", source="opencode", file="/x/o.json"))
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
    assert _ids(gui.inbox_threads(_reg(threads), NOW)) == ["oldest", "woke", "recent"]


def test_inbox_threads_breaks_mtime_ties_by_qualified_id():
    threads = [_thread("b"), _thread("a", source="codex"), _thread("a")]
    ordered = gui.inbox_threads(_reg(threads), NOW)
    assert [(t.get("source"), t["sessionId"]) for t in ordered] == [
        ("claude-code", "a"),
        ("claude-code", "b"),
        ("codex", "a"),
    ]


NO_ONE_LIVE = gui.PresenceView(frozenset(), None)


def _payload(reg, presence=NO_ONE_LIVE, stale_hours=8):
    return gui.list_payload(
        reg, NOW, registry_path="/fixture/registry.json", presence=presence, stale_hours=stale_hours
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


def test_list_payload_reports_unknown_presence():
    doc = _payload(_reg([_thread("a"), _thread("b")]), presence=gui.PresenceView(None, "x"))
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


def test_scan_payload_counts_membership_changes():
    a, b, c = ("claude-code", "a"), ("claude-code", "b"), ("claude-code", "c")
    doc = gui.scan_payload(NOW, {a, b}, {b, c}, frozenset({"pi", "codex"}))
    assert doc == {
        "version": 1,
        "kind": "scan",
        "scanned_at": NOW,
        "added": 1,
        "removed": 1,
        "unavailable_sources": ["codex", "pi"],
    }


def test_encode_document_replaces_lone_surrogates():
    encoded = gui.encode_document({"t": "a\ud83db"})
    assert json.loads(encoded.decode("utf-8")) == {"t": "a?b"}
    assert encoded.endswith(b"\n")


def test_tail_item_carries_the_thread_stamp():
    thread = _thread("a", fileFingerprint="1:2:3:4:5")
    item = gui.tail_item("claude-code:a", thread, "xyz", 1000, None)
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
    item = gui.tail_item("claude-code:a", None, None, 1000, "no longer in the registry")
    assert (item["text"], item["mtime"], item["file_fingerprint"]) == ("", None, None)
    assert item["unavailable"] == "no longer in the registry"


def test_tail_item_marks_a_full_window_as_truncated():
    item = gui.tail_item("claude-code:a", _thread("a"), "x" * 1000, 1000, None)
    assert item["truncated"] is True


def test_tails_and_error_payloads():
    items = [gui.tail_item("claude-code:a", None, None, 1000, "no longer in the registry")]
    tails = gui.tails_payload(items)
    assert set(tails) == {"version", "kind", "items"}
    assert (tails["kind"], tails["items"]) == ("tails", items)
    assert gui.error_payload("boom") == {"version": 1, "kind": "error", "message": "boom"}
