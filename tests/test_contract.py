import ast
import inspect
import json
import textwrap
from datetime import UTC, datetime, timedelta

import pytest
from ringleader import codes, contract, handoff, resume, revalidate
from ringleader.decisions import Outcome
from ringleader.handoff import HandoffReady, HandoffRefused
from ringleader.home import state as home_state
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


def _thread(sid, *, days=30.0, project="/w/proj", **extra):
    return {
        "sessionId": sid,
        "source": "claude-code",
        "project": project,
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


NO_ONE_LIVE = contract.PresenceView.ok(frozenset())


def _payload(reg, presence=NO_ONE_LIVE, stale_hours=8, harvests=0, agents=()):
    return contract.list_payload(
        reg,
        NOW,
        registry_path="/fixture/registry.json",
        scan=None,
        surroundings=contract.Surroundings(presence, stale_hours, harvests, agents),
    )


def _soon(sid, days):
    return _thread(
        sid,
        days=days,
        snoozedAt=_ago(1),
        snoozedUntil=(NOW_DT + timedelta(days=2)).isoformat(),
    )


def test_home_counts_mirror_the_home_state():
    reg = _reg(
        [
            _thread("a", days=1),
            _thread("b", days=40, project="/w/other", status="blocked"),
            _thread("k", days=2, decision="keep", keptAt=_ago(1)),
            _woke("w"),
        ]
    )
    state = home_state.build_home_state(reg, NOW, pending_harvests=5, stale_hours=8)
    home = _payload(reg, harvests=5)["home"]
    assert home["counts"] == {
        "triage_total": 3,
        "live": state.live_count,
        "unclassified": state.unclassified,
        "blocked": 1,
        "stale": state.stale_count,
        "kept": 1,
        "projects": 2,
        "spread_projects": state.spread_projects,
    }
    assert home["pending_harvests"] == 5


def test_home_projects_follow_the_queue_order():
    reg = _reg([_thread("a", days=3), _thread("b", days=40, project="/w/other")])
    projects = _payload(reg)["home"]["projects"]
    assert projects == [
        {
            "key": "/w/other",
            "label": "/w/other",
            "pending": 1,
            "unclassified": 1,
            "oldest_mtime": _ago(40),
            "newest_mtime": _ago(40),
        },
        {
            "key": "/w/proj",
            "label": "/w/proj",
            "pending": 1,
            "unclassified": 1,
            "oldest_mtime": _ago(3),
            "newest_mtime": _ago(3),
        },
    ]


def test_home_next_steps_carry_structure_only():
    reg = _reg([_woke("w"), _thread("r", days=2), _thread("o", days=30, project="/w/old")])
    steps = _payload(reg, harvests=4)["home"]["next_steps"]
    assert steps == [
        {
            "kind": "roundup",
            "deletes": True,
            "costs_model": True,
            "count": None,
            "project_key": None,
            "id": None,
        },
        {
            "kind": "woke",
            "deletes": False,
            "costs_model": False,
            "count": 1,
            "project_key": None,
            "id": None,
        },
        {
            "kind": "recent",
            "deletes": False,
            "costs_model": False,
            "count": 1,
            "project_key": None,
            "id": None,
        },
        {
            "kind": "publish",
            "deletes": False,
            "costs_model": False,
            "count": 4,
            "project_key": None,
            "id": None,
        },
        {
            "kind": "project",
            "deletes": False,
            "costs_model": False,
            "count": 1,
            "project_key": "/w/old",
            "id": None,
        },
    ]


def test_home_resume_step_names_the_session_by_qualified_id():
    live = _thread("live", days=2, status="active")
    live["reviewedAtMtime"] = live["mtime"]
    reg = _reg([live], lastRoundupAt=_ago(hours=1))
    steps = _payload(reg)["home"]["next_steps"]
    assert steps == [
        {
            "kind": "resume",
            "deletes": False,
            "costs_model": False,
            "count": None,
            "project_key": None,
            "id": "claude-code:live",
        }
    ]


def test_home_rows_hold_referenced_sessions_missing_from_the_list():
    reviewed = _thread("flying", days=0.5, status="active")
    reviewed["reviewedAtMtime"] = reviewed["mtime"]
    doc = _payload(_reg([_soon("sleeper", 5), reviewed, _thread("open", days=1)]))
    home = doc["home"]
    assert home["waking_soon"] == ["claude-code:sleeper"]
    assert home["in_flight"] == ["claude-code:flying", "claude-code:open"]
    assert [row["id"] for row in doc["sessions"]] == ["claude-code:open"]
    assert [row["id"] for row in home["rows"]] == ["claude-code:sleeper", "claude-code:flying"]
    assert set(home["rows"][0]) == set(contract.SESSION_ROW_KEYS)
    assert home["rows"][0]["snoozed_until"] == (NOW_DT + timedelta(days=2)).isoformat()


def test_list_payload_lists_launchable_agents_with_their_install_state():
    agents = (contract.AgentView("claude-code", True), contract.AgentView("codex", False))
    assert _payload(_reg([]), agents=agents)["agents"] == [
        {"source": "claude-code", "agent": "claude", "installed": True},
        {"source": "codex", "agent": "codex", "installed": False},
    ]


def test_list_payload_flags_recent_and_woke_rows():
    doc = _payload(_reg([_thread("recent", days=1), _woke("woke"), _thread("old", days=60)]))
    rows = {row["session_id"]: row for row in doc["sessions"]}
    assert (doc["version"], doc["kind"], doc["registry"]) == (1, "list", "/fixture/registry.json")
    assert doc["generated_at"] == NOW
    assert doc["recent_days"] == 7
    assert [rows[s]["in_recent"] for s in ("recent", "woke", "old")] == [True, False, False]
    assert [rows[s]["woke"] for s in ("recent", "woke", "old")] == [False, True, False]
    assert doc["presence"] == {"state": "ok", "problem": None, "code": None}
    assert doc["scan"] is None


def test_list_payload_reports_unknown_presence():
    view = contract.PresenceView.unknown("x", "not_installed")
    doc = _payload(_reg([_thread("a"), _thread("b")]), presence=view)
    assert [row["running"] for row in doc["sessions"]] == [None, None]
    assert doc["presence"] == {"state": "unknown", "problem": "x", "code": "not_installed"}


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
        scan=summary,
        surroundings=contract.Surroundings(NO_ONE_LIVE, 8, 0, ()),
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
    with pytest.raises(contract.UntilError, match="timezone offset") as raised:
        contract.normalize_until("2026-10-20T09:00:00", NOW_DT)
    assert raised.value.code == "until_naive"


def test_normalize_until_rejects_a_past_time():
    with pytest.raises(contract.UntilError, match="in the future") as raised:
        contract.normalize_until("2026-10-14T09:00:00+00:00", NOW_DT)
    assert raised.value.code == "until_past"


def test_normalize_until_rejects_text_that_is_not_a_time():
    with pytest.raises(contract.UntilError, match="not an ISO 8601 time") as raised:
        contract.normalize_until("next tuesday", NOW_DT)
    assert raised.value.code == "until_invalid"


SEEN = Seen(("claude-code", "s1"), "s1-title", NOW, None)


def test_from_outcome_reports_the_first_refusal_reason():
    outcome = Outcome(
        ok=False,
        message="s1-title: changed since you looked",
        wrote=False,
        refused=((SEEN, "changed since you looked"),),
        code="changed",
    )
    result = contract.from_outcome(outcome)
    assert (result.status, result.reason, result.code) == (
        "refused",
        "changed since you looked",
        "changed",
    )


def test_from_outcome_reports_a_partial_write_as_failed():
    outcome = Outcome(ok=False, message="archived x on disk, but …", wrote=True, path="/a/x")
    result = contract.from_outcome(outcome)
    assert (result.status, result.wrote, result.path, result.code) == (
        "failed",
        True,
        "/a/x",
        "failed",
    )


def test_from_outcome_carries_the_code_the_decision_set():
    outcome = Outcome(
        ok=False, message="claude-code:s1: no longer in the registry", wrote=False, code="gone"
    )
    assert contract.from_outcome(outcome).code == "gone"


def test_from_outcome_codes_a_refusal_without_a_code_as_other():
    outcome = Outcome(ok=False, message="m", wrote=False, refused=((SEEN, "disk said no"),))
    assert contract.from_outcome(outcome).code == "other"


@pytest.mark.parametrize(
    ("status", "code"), [("refused", None), ("failed", None), ("ok", "gone"), ("empty", "other")]
)
def test_an_outcome_has_a_code_exactly_when_it_refused_or_failed(status, code):
    with pytest.raises(ValueError, match="code"):
        contract.GuiOutcome(status, None, "m", code=code)


@pytest.mark.parametrize(("live", "code"), [(None, None), (frozenset(), "failed")])
def test_presence_has_a_code_exactly_when_it_is_unknown(live, code):
    with pytest.raises(ValueError, match="code"):
        contract.PresenceView(live, "problem", code)


def test_from_outcome_copies_an_ok_outcome():
    outcome = Outcome(ok=True, message="archived x", wrote=True, warnings=("w",), path="/a/x")
    assert contract.from_outcome(outcome) == contract.GuiOutcome(
        "ok", None, "archived x", True, "/a/x", ("w",)
    )


def test_refused_defaults_its_message_to_the_reason():
    assert contract.refused("gone", "gone") == contract.GuiOutcome(
        "refused", "gone", "gone", code="gone"
    )


def test_outcome_codes_are_a_closed_set():
    assert codes.OUTCOME_CODES == (
        "changed",
        "no_fingerprint",
        "gone",
        "transcript_missing",
        "running",
        "presence_unknown",
        "lifecycle_blocked",
        "until_invalid",
        "until_naive",
        "until_past",
        "no_harvest_model",
        "harvest_failed",
        "failed",
        "other",
    )
    assert contract.PRESENCE_CODES == ("not_installed", "failed")
    assert handoff.HANDOFF_CODES == ("nothing_to_triage", "agent_not_installed")


def test_revalidation_refusals_carry_their_code():
    thread = _thread("s1", title="named")
    live = frozenset({("claude", "s1")})
    seen = Seen(("claude-code", "s1"), "named", "other-mtime", None)
    refusals = [
        revalidate.changed_refusal(None, seen),
        revalidate.changed_refusal(thread, seen),
        revalidate.running_refusal(thread, live, "refuse"),
        revalidate.running_refusal(thread, None, "refuse"),
    ]
    assert [r.code for r in refusals if r is not None] == [
        "gone",
        "changed",
        "running",
        "presence_unknown",
    ]


def test_a_running_session_named_like_a_presence_warning_is_still_running():
    thread = _thread("s1", title="can't tell whether this works")
    refusal = revalidate.running_refusal(thread, frozenset({("claude", "s1")}), "refuse")
    assert refusal is not None
    assert refusal.code == "running"


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
        "code",
        "message",
        "wrote",
        "path",
        "warnings",
    }
    assert (doc["kind"], doc["action"], doc["warnings"]) == ("outcome", "archive", [])


ROW_BADGES = {"filtered", "snoozed", "kept", "finishing", "harvested", "abandoned", "drifted"}


def test_list_row_badges_come_from_a_closed_set():
    tree = ast.parse(textwrap.dedent(inspect.getsource(resume.badges)))
    appended = [
        node.args[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append"
    ]
    literals = {arg.value for arg in appended if isinstance(arg, ast.Constant)}
    narrowed = {
        element.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        for options in node.comparators
        if isinstance(options, ast.Tuple)
        for element in options.elts
        if isinstance(element, ast.Constant)
    }
    assert all(isinstance(arg, ast.Constant | ast.Name) for arg in appended)
    assert literals | narrowed == ROW_BADGES


def test_handoff_payload_for_a_launchable_plan():
    plan = HandoffReady("claude-code", "/w/a", "/w/a", ("claude", "go"))
    assert contract.handoff_payload(plan) == {
        "version": 1,
        "kind": "handoff",
        "purpose": "triage",
        "status": "ok",
        "code": None,
        "reason": None,
        "source": "claude-code",
        "agent": "claude",
        "project_key": "/w/a",
        "cwd": "/w/a",
        "argv": ["claude", "go"],
    }


def test_handoff_payload_for_a_refused_plan_has_a_code_a_reason_and_no_launch():
    plan = HandoffRefused("codex", "/w/a", "nothing_to_triage", "no sessions of /w/a are waiting")
    doc = contract.handoff_payload(plan)
    assert (doc["status"], doc["code"], doc["reason"], doc["cwd"], doc["argv"]) == (
        "refused",
        "nothing_to_triage",
        "no sessions of /w/a are waiting",
        None,
        None,
    )
