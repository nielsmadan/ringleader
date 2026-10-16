from datetime import UTC, datetime, timedelta

from ringleader import registry
from ringleader.home.state import (
    ProjectScope,
    RecentScope,
    WokeScope,
    build_home_state,
    is_woke,
    next_steps,
    project_queue,
    scope_rows,
    scope_title,
)

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)
NOW = NOW_DT.isoformat()


def _iso(days: float) -> str:
    return (NOW_DT - timedelta(days=days)).isoformat()


def _thread(sid, *, days=1.0, project="/w/a", status="unclassified", **extra):
    return {
        "sessionId": sid,
        "source": "claude-code",
        "project": project,
        "title": f"title {sid}",
        "mtime": _iso(days),
        "status": status,
        "decision": None,
        **extra,
    }


def _woke(sid, *, days=30.0):
    return _thread(sid, days=days, snoozedAt=_iso(days - 1), snoozedUntil=_iso(1))


def _reg(*threads, last_roundup=NOW):
    return {**registry.empty_registry(), "threads": list(threads), "lastRoundupAt": last_roundup}


def _state(reg, *, harvests=0):
    return build_home_state(reg, NOW, pending_harvests=harvests, stale_hours=8)


def _kinds(reg, *, harvests=0):
    return [step.kind for step in next_steps(_state(reg, harvests=harvests))]


def test_a_stale_roundup_alone_recommends_the_roundup():
    steps = next_steps(_state(_reg(last_roundup=None)))
    assert [(s.kind, s.title, s.deletes, s.costs_model) for s in steps] == [
        ("roundup", "Run the roundup (never run)", True, True)
    ]


def test_an_expired_snooze_alone_asks_for_a_review():
    steps = next_steps(_state(_reg(_woke("w1"))))
    assert [(s.kind, s.title) for s in steps] == [("woke", "Review 1 snoozed session that woke up")]


def test_recent_undecided_sessions_alone_ask_for_triage():
    steps = next_steps(_state(_reg(_thread("r1", days=2), _thread("r2", days=3))))
    assert [(s.kind, s.title) for s in steps] == [
        ("recent", "Triage 2 sessions from the last 7 days")
    ]


def test_pending_harvests_alone_ask_for_a_publish():
    steps = next_steps(_state(_reg(), harvests=3))
    assert [(s.kind, s.title, s.detail) for s in steps] == [
        (
            "publish",
            "Publish 3 pending harvests with an agent",
            "hands off to your agent's ringleader-publish skill",
        )
    ]


def test_an_old_backlog_alone_names_the_next_project():
    steps = next_steps(_state(_reg(_thread("o1", days=30), _thread("o2", days=40))))
    assert [(s.kind, s.title) for s in steps] == [
        ("project", "Triage /w/a next (2 waiting, oldest 40d)")
    ]
    assert steps[0].project is not None
    assert steps[0].project.key == "/w/a"


def test_every_step_appears_in_the_fixed_order_with_the_roundup_first():
    reg = _reg(_woke("w1"), _thread("r1", days=2), _thread("o1", days=30), last_roundup=None)
    assert _kinds(reg, harvests=1) == ["roundup", "woke", "recent", "publish", "project"]


def test_resume_is_offered_only_when_nothing_else_applies():
    reviewed = _thread("live", days=2, status="active")
    reviewed["reviewedAtMtime"] = reviewed["mtime"]
    steps = next_steps(_state(_reg(reviewed)))
    assert [(s.kind, s.title, s.session) for s in steps] == [
        ("resume", "Resume title live", ("claude-code", "live"))
    ]
    assert _kinds(_reg(reviewed, _thread("r1", days=2))) == ["recent"]


def test_nothing_waiting_gives_no_steps():
    assert next_steps(_state(_reg())) == ()


def test_project_queue_orders_by_oldest_activity_and_counts_unclassified():
    threads = [
        _thread("a1", days=30, project="/w/a"),
        _thread("a2", days=10, project="/w/a", status="active"),
        _thread("b1", days=50, project="/w/b"),
        _thread("c1", days=30, project="/w/c"),
    ]
    queue = project_queue(threads, NOW)
    assert [(p.key, p.pending, p.unclassified) for p in queue] == [
        ("/w/b", 1, 1),
        ("/w/a", 2, 1),
        ("/w/c", 1, 1),
    ]
    assert (queue[1].oldest_mtime, queue[1].newest_mtime) == (_iso(30), _iso(10))


def test_is_woke_holds_for_an_expired_snooze_until_it_is_reviewed():
    woke = _woke("w1")
    assert is_woke(woke, NOW) is True
    assert is_woke({**woke, "reviewedAtMtime": woke["mtime"]}, NOW) is False


def test_filtered_threads_count_nowhere():
    plain = _reg(_thread("r1", days=2), _thread("k1", days=3, status="active", decision="keep"))
    hidden = [
        _thread("f1", days=1, status="active", filteredBy="zz*"),
        _woke("f2") | {"filteredBy": "zz*"},
        _thread("f3", days=2, status="active", decision="keep", filteredBy="zz*"),
    ]
    with_filtered = _reg(*plain["threads"], *hidden)
    assert _state(with_filtered) == _state(plain)


def test_scope_rows_list_each_scope_oldest_first():
    threads = [
        _thread("a-new", days=2, project="/w/a"),
        _thread("a-old", days=20, project="/w/a"),
        _thread("b", days=4, project="/w/b"),
        _woke("w-new", days=12),
        _woke("w-old", days=60),
    ]
    a = next(p for p in project_queue(threads, NOW) if p.key == "/w/a")
    assert [t["sessionId"] for t in scope_rows(ProjectScope(a), threads, NOW)] == [
        "w-old",
        "a-old",
        "w-new",
        "a-new",
    ]
    assert [t["sessionId"] for t in scope_rows(RecentScope(7), threads, NOW)] == ["b", "a-new"]
    assert [t["sessionId"] for t in scope_rows(WokeScope(), threads, NOW)] == ["w-old", "w-new"]


def test_scope_titles():
    a = project_queue([_thread("a", days=2)], NOW)[0]
    assert [scope_title(s) for s in (ProjectScope(a), RecentScope(7), WokeScope())] == [
        "/w/a",
        "Last 7 days",
        "Woke from snooze",
    ]


def test_home_state_lists_in_flight_and_waking_soon():
    soon = _thread(
        "s1", days=3, snoozedAt=_iso(2), snoozedUntil=(NOW_DT + timedelta(days=2)).isoformat()
    )
    later = _thread(
        "s2", days=3, snoozedAt=_iso(2), snoozedUntil=(NOW_DT + timedelta(days=20)).isoformat()
    )
    finishing = _thread("fx", days=1, status="active", finishOnExit=True)
    state = _state(
        _reg(_thread("i1", days=1, status="active"), _thread("i2", days=2), soon, later, finishing)
    )
    assert [t["sessionId"] for t in state.waking_soon] == ["s1"]
    assert [t["sessionId"] for t in state.in_flight] == ["i1", "i2"]


def test_a_bare_cwd_project_is_labelled_by_its_last_path_components():
    from ringleader.home.state import project_label

    assert project_label(_thread("a", project="/var/folders/x/work/proj-01")) == "…/work/proj-01"
    assert project_label(_thread("a", project="/w/a")) == "/w/a"
    assert project_label(_thread("a", gitProjectLabel="juggler")) == "juggler"
