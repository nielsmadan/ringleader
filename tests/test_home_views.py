import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ringleader.home.state import handoff_key, scope_rows, scope_title
from ringleader.home.views import (
    FolderScope,
    PeriodScope,
    TreeScope,
    date_buckets,
    folder_groups,
    folder_tree,
    matches,
)

HOME = "/home/me"
BERLIN = ZoneInfo("Europe/Berlin")
NOW_DT = datetime(2026, 11, 12, 15, 0, tzinfo=BERLIN)
NOW = NOW_DT.astimezone(UTC).isoformat()


def _thread(sid, project="/home/me/w/a", *, mtime=None, status="unclassified", **extra):
    return {
        "sessionId": sid,
        "source": "claude-code",
        "project": project,
        "title": f"title {sid}",
        "mtime": (mtime or NOW_DT - timedelta(hours=1)).isoformat(),
        "status": status,
        "decision": None,
        **extra,
    }


def _at(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=BERLIN)


def _ids(rows):
    return [t["sessionId"] for t in rows]


def _shape(nodes):
    return [(n.name, n.path, n.own, n.total, _shape(n.children)) for n in nodes]


def test_folder_groups_are_one_per_cwd_labelled_with_the_home_path_and_sorted_by_label():
    threads = [
        _thread("b1", "/home/me/w/beta", mtime=_at(2026, 11, 1), status="active"),
        _thread("b2", "/home/me/w/beta", mtime=_at(2026, 11, 3)),
        _thread("a", "/home/me/w/Alpha"),
        _thread("o", "/opt/tool"),
        _thread("u", None),
    ]
    groups = folder_groups(threads, HOME)
    assert [(g.key, g.label) for g in groups] == [
        ("/opt/tool", "/opt/tool"),
        ("(unknown)", "Unknown folder"),
        ("/home/me/w/Alpha", "~/w/Alpha"),
        ("/home/me/w/beta", "~/w/beta"),
    ]
    beta = groups[-1]
    assert (beta.pending, beta.unclassified) == (2, 1)
    assert (beta.oldest_mtime, beta.newest_mtime) == (
        _at(2026, 11, 1).isoformat(),
        _at(2026, 11, 3).isoformat(),
    )


def test_folder_groups_with_equal_labels_sort_by_key():
    groups = folder_groups([_thread("x", "/x"), _thread("y", "/X")], HOME)
    assert [g.key for g in groups] == ["/X", "/x"]


def test_folder_tree_compacts_a_chain_with_no_sessions_of_its_own():
    threads = [
        _thread("c1", "/home/me/wrksp/circus/dev1"),
        _thread("c2", "/home/me/wrksp/circus/dev1"),
        _thread("r", "/home/me/wrksp/ringleader/dev1"),
    ]
    assert _shape(folder_tree(threads, HOME)) == [
        (
            "~/wrksp",
            "/home/me/wrksp",
            0,
            3,
            [
                ("circus/dev1", "/home/me/wrksp/circus/dev1", 2, 2, []),
                ("ringleader/dev1", "/home/me/wrksp/ringleader/dev1", 1, 1, []),
            ],
        )
    ]


def test_folder_tree_chain_stops_at_a_folder_with_its_own_sessions():
    threads = [_thread("w", "/home/me/wrksp"), _thread("d", "/home/me/wrksp/circus/dev1")]
    assert _shape(folder_tree(threads, HOME)) == [
        (
            "~/wrksp",
            "/home/me/wrksp",
            1,
            2,
            [("circus/dev1", "/home/me/wrksp/circus/dev1", 1, 1, [])],
        )
    ]


def test_folder_tree_puts_home_first_then_slash_then_unknown_last():
    threads = [
        _thread("u", None),
        _thread("t", "/tmp/scratch"),
        _thread("o", "/opt/tool"),
        _thread("h", "/home/me"),
    ]
    assert _shape(folder_tree(threads, HOME)) == [
        ("~", "/home/me", 1, 1, []),
        (
            "/",
            "/",
            0,
            2,
            [("opt/tool", "/opt/tool", 1, 1, []), ("tmp/scratch", "/tmp/scratch", 1, 1, [])],
        ),
        ("Unknown folder", "(unknown)", 1, 1, []),
    ]


def test_folder_tree_compacts_a_root_outside_home_into_one_path():
    assert _shape(folder_tree([_thread("o", "/opt/tool")], HOME)) == [
        ("/opt/tool", "/opt/tool", 1, 1, [])
    ]


def test_folder_tree_children_sort_by_name_ignoring_case():
    threads = [_thread(n, f"/home/me/{n}") for n in ("beta", "Alpha", "gamma")]
    assert [n.name for n in folder_tree(threads, HOME)[0].children] == ["Alpha", "beta", "gamma"]


def test_folder_tree_places_a_relative_folder_before_unknown():
    roots = folder_tree([_thread("u", None), _thread("r", "relative/dir")], HOME)
    assert [(n.name, n.path, n.total) for n in roots] == [
        ("relative/dir", "relative/dir", 1),
        ("Unknown folder", "(unknown)", 1),
    ]


def test_folder_scope_matches_the_folder_exactly():
    scope = FolderScope("/a/foo")
    assert [matches(scope, _thread(p, p)) for p in ("/a/foo", "/a/foo/sub", "/a/foobar")] == [
        True,
        False,
        False,
    ]


def test_tree_scope_matches_the_folder_and_its_subfolders_not_a_shared_prefix():
    scope = TreeScope("/a/foo")
    assert [matches(scope, _thread(p, p)) for p in ("/a/foo", "/a/foo/sub", "/a/foobar")] == [
        True,
        True,
        False,
    ]


def test_the_slash_root_scope_covers_what_the_row_counts_and_leaves_home_to_the_tilde_root():
    threads = [_thread("o", "/opt/tool"), _thread("t", "/tmp/x"), _thread("h", "/home/me/w")]
    home_root, slash_root = folder_tree(threads, HOME)
    assert [matches(slash_root.scope, t) for t in threads] == [True, True, False]
    assert [matches(home_root.scope, t) for t in threads] == [False, False, True]
    assert slash_root.total == len(scope_rows(slash_root.scope, threads, NOW))


def test_a_folder_that_only_shares_a_prefix_with_home_hangs_from_the_slash_root():
    assert _shape(folder_tree([_thread("o", "/home/meow/x")], HOME)) == [
        ("/home/meow/x", "/home/meow/x", 1, 1, [])
    ]


def test_a_folder_above_home_leaves_home_out_of_its_scope():
    threads = [
        _thread("a", "/home/ann/x"),
        _thread("b", "/home/bob/y"),
        _thread("h", "/home/me/w"),
    ]
    _, home_node = folder_tree(threads, HOME)
    assert (home_node.name, home_node.path, home_node.total) == ("/home", "/home", 2)
    assert home_node.total == len(scope_rows(home_node.scope, threads, NOW))


def test_the_unknown_scope_matches_only_threads_without_a_folder():
    scope = folder_tree([_thread("u", None)], HOME)[0].scope
    assert [matches(scope, t) for t in (_thread("u", None), _thread("o", "/opt"))] == [
        True,
        False,
    ]


def _keys(buckets):
    return [b.key for b in buckets]


def test_date_buckets_run_days_then_weeks_then_months_newest_first():
    days_ago = [0, 1, 6, 7, 14, 45, 62, 90]
    threads = [_thread(str(n), mtime=NOW_DT - timedelta(days=n)) for n in days_ago]
    buckets = date_buckets(threads, NOW_DT, BERLIN)
    assert _keys(buckets) == [
        "day:2026-11-12",
        "day:2026-11-11",
        "day:2026-11-06",
        "week:2026-11-02",
        "week:2026-10-26",
        "week:2026-09-28",
        "month:2026-09",
        "month:2026-08",
    ]
    assert [b.label for b in buckets] == [k.split(":")[1] for k in _keys(buckets)]


def test_date_buckets_omit_empty_periods_and_carry_counts():
    threads = [
        _thread("a", mtime=_at(2026, 11, 11, 9), status="active"),
        _thread("b", mtime=_at(2026, 11, 11, 18)),
    ]
    (bucket,) = date_buckets(threads, NOW_DT, BERLIN)
    assert (bucket.key, bucket.kind, bucket.pending, bucket.unclassified) == (
        "day:2026-11-11",
        "day",
        2,
        1,
    )
    assert (bucket.oldest_mtime, bucket.newest_mtime) == (
        threads[0]["mtime"],
        threads[1]["mtime"],
    )
    assert (bucket.start, bucket.end) == (_at(2026, 11, 11, 0), _at(2026, 11, 12, 0))


def test_a_week_the_day_range_overlaps_ends_where_the_days_begin():
    (bucket,) = date_buckets([_thread("w", mtime=_at(2026, 11, 3))], NOW_DT, BERLIN)
    assert (bucket.key, bucket.start, bucket.end) == (
        "week:2026-11-02",
        _at(2026, 11, 2, 0),
        _at(2026, 11, 6, 0),
    )


def test_the_oldest_month_ends_where_the_oldest_week_begins():
    (bucket,) = date_buckets([_thread("m", mtime=_at(2026, 9, 2))], NOW_DT, BERLIN)
    assert (bucket.key, bucket.start, bucket.end) == (
        "month:2026-09",
        _at(2026, 9, 1, 0),
        _at(2026, 9, 14, 0),
    )


def test_a_dst_week_runs_from_local_midnight_to_local_midnight():
    late_sunday = _thread("sun", mtime=datetime(2026, 10, 25, 22, 30, tzinfo=UTC))
    early_monday = _thread("mon", mtime=datetime(2026, 10, 25, 23, 30, tzinfo=UTC))
    dst, after = sorted(
        date_buckets([late_sunday, early_monday], NOW_DT, BERLIN), key=lambda b: b.start
    )
    assert (dst.key, after.key) == ("week:2026-10-19", "week:2026-10-26")
    assert dst.start.isoformat() == "2026-10-19T00:00:00+02:00"
    assert dst.end is not None
    assert dst.end.isoformat() == "2026-10-26T00:00:00+01:00"
    assert dst.end.astimezone(UTC) - dst.start.astimezone(UTC) == timedelta(days=7, hours=1)


def test_the_oldest_week_starts_where_the_months_end():
    threads = [_thread("w", mtime=_at(2026, 9, 14)), _thread("m", mtime=_at(2026, 9, 13))]
    assert _keys(date_buckets(threads, NOW_DT, BERLIN)) == ["week:2026-09-14", "month:2026-09"]


def test_a_december_bucket_ends_at_new_year():
    now = datetime(2027, 3, 10, 12, tzinfo=BERLIN)
    (bucket,) = date_buckets([_thread("d", mtime=_at(2026, 12, 5))], now, BERLIN)
    assert (bucket.key, bucket.end) == ("month:2026-12", _at(2027, 1, 1, 0))


def test_an_mtime_in_the_future_counts_as_today():
    threads = [_thread("f", mtime=NOW_DT + timedelta(days=3)), _thread("t")]
    (today,) = date_buckets(threads, NOW_DT, BERLIN)
    assert (today.key, today.pending, today.end) == ("day:2026-11-12", 2, None)
    assert [matches(today.scope, t) for t in threads] == [True, True]


def test_date_buckets_take_their_labels_from_the_formatter():
    def words(kind, first: date) -> str:
        return f"{kind} {first:%d %b}"

    (bucket,) = date_buckets([_thread("w", mtime=_at(2026, 11, 3))], NOW_DT, BERLIN, label=words)
    assert (bucket.key, bucket.label, bucket.scope.label) == (
        "week:2026-11-02",
        "week 02 Nov",
        "week 02 Nov",
    )


def test_period_scope_matches_a_half_open_range():
    scope = PeriodScope(_at(2026, 11, 11, 0), _at(2026, 11, 12, 0), "2026-11-11")
    times = [_at(2026, 11, 10, 23, 59), _at(2026, 11, 11, 0), _at(2026, 11, 12, 0)]
    assert [matches(scope, _thread("x", mtime=t)) for t in times] == [False, True, False]


def test_scope_rows_keep_to_the_triage_inbox_oldest_first():
    threads = [
        _thread("new", "/a/foo", mtime=_at(2026, 11, 11)),
        _thread("old", "/a/foo/sub", mtime=_at(2026, 11, 1)),
        _thread("kept", "/a/foo", decision="keep"),
        _thread("other", "/a/foobar"),
    ]
    assert _ids(scope_rows(TreeScope("/a/foo"), threads, NOW)) == ["old", "new"]
    assert _ids(scope_rows(FolderScope("/a/foo"), threads, NOW)) == ["new"]
    period = PeriodScope(_at(2026, 11, 1, 0), None, "2026-11-01")
    assert _ids(scope_rows(period, threads, NOW)) == ["old", "new", "other"]


def test_scope_titles_for_folder_tree_and_period_scopes():
    scopes = [
        FolderScope("/opt/tool"),
        TreeScope("/"),
        FolderScope("(unknown)"),
        PeriodScope(_at(2026, 11, 2, 0), _at(2026, 11, 6, 0), "2026-11-02"),
    ]
    assert [scope_title(s, HOME) for s in scopes] == [
        "/opt/tool",
        "/",
        "Unknown folder",
        "2026-11-02",
    ]
    assert scope_title(TreeScope("/home/me/wrksp"), HOME) == "~/wrksp"
    assert scope_title(TreeScope("/", excluding=HOME), HOME) == "/ except ~"


def test_handoff_key_is_the_one_project_a_scope_holds():
    threads = [
        _thread("a", "/w/repo", gitProjectKey="git:repo"),
        _thread("b", "/w/repo/sub", gitProjectKey="git:repo"),
        _thread("c", "/w/other"),
    ]
    assert handoff_key(TreeScope("/w/repo"), threads, NOW) == "git:repo"
    assert handoff_key(FolderScope("/w/other"), threads, NOW) == "/w/other"


def test_handoff_key_for_a_period_is_the_one_project_active_in_it():
    threads = [
        _thread("a", "/w/repo", mtime=_at(2026, 11, 11), gitProjectKey="git:repo"),
        _thread("b", "/w/other", mtime=_at(2026, 11, 3)),
    ]
    yesterday = PeriodScope(_at(2026, 11, 11, 0), _at(2026, 11, 12, 0), "2026-11-11")
    assert handoff_key(yesterday, threads, NOW) == "git:repo"


def test_handoff_key_is_none_when_a_scope_spans_several_projects():
    threads = [_thread("a", "/w/repo", gitProjectKey="git:repo"), _thread("c", "/w/other")]
    assert handoff_key(TreeScope("/w"), threads, NOW) is None
    assert handoff_key(FolderScope("/w/none"), threads, NOW) is None


GROUPING_BUDGET_SECONDS = 0.5


def test_groupings_stay_fast_at_five_thousand_threads():
    threads = [
        _thread(
            str(i),
            f"/home/me/wrksp/p{i % 300}/sub{i % 7}" if i % 50 else f"/srv/x{i % 13}",
            mtime=NOW_DT - timedelta(hours=i),
        )
        for i in range(5000)
    ]
    times = []
    for _ in range(3):
        start = time.perf_counter()
        folder_groups(threads, HOME)
        folder_tree(threads, HOME)
        date_buckets(threads, NOW_DT, BERLIN)
        scope_rows(TreeScope("/home/me/wrksp"), threads, NOW)
        handoff_key(TreeScope("/home/me/wrksp/p7"), threads, NOW)
        times.append(time.perf_counter() - start)
    assert min(times) <= GROUPING_BUDGET_SECONDS
