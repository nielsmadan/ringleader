import pytest
from ringleader.overview import (
    age_days,
    age_human,
    compute_metrics,
    effective_status,
    is_stale,
    newer_than,
    registry_age_human,
    render,
    staleness_banner,
    tilde,
)

NOW = "2026-06-30T00:00:00+00:00"


def test_age_human():
    assert age_human(None, NOW) == "never"
    assert age_human("2026-06-29T23:59:30+00:00", NOW) == "just now"
    assert age_human("2026-06-29T20:00:00+00:00", NOW) == "4h ago"
    assert age_human("2026-06-18T00:00:00+00:00", NOW) == "12d ago"


def test_newer_than():
    assert newer_than("2026-06-20T00:00:01+00:00", "2026-06-20T00:00:00+00:00") is True
    assert newer_than("2026-06-19T00:00:00+00:00", "2026-06-20T00:00:00+00:00") is False


def test_tilde():
    h = "/Users/me"
    assert tilde("/Users/me/wrksp/ff", home=h) == "~/wrksp/ff"
    assert tilde("/Users/me", home=h) == "~"
    assert tilde("/opt/other", home=h) == "/opt/other"  # not under home
    assert tilde("/Users/meta/x", home=h) == "/Users/meta/x"  # no false prefix match
    assert tilde(None, home=h) == ""


def test_registry_age_human():
    assert registry_age_human({"lastScan": None}, NOW) == "never"
    assert registry_age_human({"lastScan": "2026-06-29T23:59:30+00:00"}, NOW) == "just now"
    assert registry_age_human({"lastScan": "2026-06-29T23:30:00+00:00"}, NOW) == "30m ago"
    assert registry_age_human({"lastScan": "2026-06-29T20:00:00+00:00"}, NOW) == "4h ago"
    assert registry_age_human({"lastScan": "2026-06-18T00:00:00+00:00"}, NOW) == "12d ago"


def test_staleness_banner_fresh_returns_none():
    assert staleness_banner({"lastScan": "2026-06-29T23:30:00+00:00"}, NOW) is None


def test_staleness_banner_stale_mentions_age_and_scan():
    b = staleness_banner({"lastScan": "2026-06-18T00:00:00+00:00"}, NOW)
    assert b is not None and "12d ago" in b and "rl session refresh" in b


def test_staleness_banner_never_scanned():
    b = staleness_banner({"lastScan": None}, NOW)
    assert b is not None and "never scanned" in b


def _thread(  # noqa: PLR0913, PLR0917 -- test data-builder helper, one arg per fixture field
    sid, status, mtime, decision=None, project="/p", git_key=None, git_label=None
):
    return {
        "sessionId": sid,
        "project": project,
        "status": status,
        "mtime": mtime,
        "decision": decision,
        "whatItIs": f"about {sid}",
        "title": None,
        "suggestedName": f"{sid}-name",
        "lastPrompt": "lp",
        "gitProjectKey": git_key,
        "gitProjectLabel": git_label,
        "gitBranch": None,
    }


def _reg(threads, last_scan=NOW):
    return {"version": 1, "lastScan": last_scan, "threads": threads, "archived": []}


def test_age_days():
    assert round(age_days("2026-06-20T00:00:00+00:00", NOW)) == 10


def test_effective_status_derives_abandoned_from_stale_unfinished():
    fresh = _thread("a", "unfinished", "2026-06-29T00:00:00+00:00")
    stale = _thread("b", "unfinished", "2026-06-01T00:00:00+00:00")  # 29d old
    assert effective_status(fresh, NOW) == "unfinished"
    assert effective_status(stale, NOW) == "abandoned"


def test_effective_status_respects_decisions():
    t = _thread("c", "unfinished", NOW, decision="abandon")
    assert effective_status(t, NOW) == "abandoned"
    t2 = _thread("d", "unfinished", NOW, decision="harvested")
    assert effective_status(t2, NOW) == "harvested"


def test_compute_metrics_counts_live_and_spread():
    reg = _reg(
        [
            _thread("a", "unfinished", "2026-06-29T00:00:00+00:00", project="/p1"),
            _thread("b", "blocked", "2026-06-28T00:00:00+00:00", project="/p2"),
            _thread("c", "finished", "2026-06-10T00:00:00+00:00", project="/p1"),
            _thread("d", "unfinished", "2026-06-01T00:00:00+00:00", project="/p3"),  # abandoned
        ]
    )
    m = compute_metrics(reg, NOW)
    assert m["live_count"] == 2  # a + b (c finished, d abandoned)
    assert m["spread_projects"] == 2  # /p1, /p2
    assert m["blocked_count"] == 1
    assert m["by_status"]["abandoned"] == 1
    assert m["by_status"]["finished"] == 1


def test_render_groups_live_threads_and_flags_blocked(tmp_path):
    reg = _reg(
        [
            _thread("a", "blocked", "2026-06-28T00:00:00+00:00", project="/p1"),
            _thread("b", "unfinished", "2026-06-27T00:00:00+00:00", project="/p1"),
        ]
    )
    out = render(reg, NOW)
    assert "/p1" in out
    assert "blocked" in out.lower()
    assert "a-name" in out  # suggestedName shown when no title
    assert "Live threads: 2" in out


def test_render_groups_two_cwds_under_one_git_project():
    # two different working dirs that resolve to the same repo collapse into one group
    reg = _reg(
        [
            _thread(
                "a",
                "unfinished",
                "2026-06-29T00:00:00+00:00",
                project="/checkoutA",
                git_key="github.com/me/repo",
                git_label="repo",
            ),
            _thread(
                "b",
                "unfinished",
                "2026-06-28T00:00:00+00:00",
                project="/checkoutB",
                git_key="github.com/me/repo",
                git_label="repo",
            ),
        ]
    )
    out = render(reg, NOW)
    assert out.count("## repo") == 1
    m = compute_metrics(reg, NOW)
    assert m["live_count"] == 2
    assert m["spread_projects"] == 1  # one git project despite two cwds


def test_is_stale_based_on_last_scan():
    assert is_stale(_reg([], last_scan=None), NOW) is True
    assert (
        is_stale(_reg([], last_scan="2026-06-29T23:30:00+00:00"), NOW, max_age_minutes=60) is False
    )
    assert (
        is_stale(_reg([], last_scan="2026-06-20T00:00:00+00:00"), NOW, max_age_minutes=60) is True
    )


def test_age_days_tz_naive_mtime_does_not_raise():
    """Fix 2: a tz-naive mtime string must not crash when compared against a tz-aware now."""
    tz_aware_now = "2026-06-30T00:00:00+00:00"
    tz_naive_mtime = "2026-06-20T00:00:00"  # no offset
    result = age_days(tz_naive_mtime, tz_aware_now)
    assert result == pytest.approx(10.0, abs=0.01)


def test_effective_status_keep_decision_blocks_abandoned():
    """Fix 4: decision='keep' must prevent the age-based abandonment rule."""
    # 29 days old — stale enough to normally become 'abandoned'
    stale_kept = _thread("x", "unfinished", "2026-06-01T00:00:00+00:00", decision="keep")
    assert effective_status(stale_kept, NOW) == "unfinished"


from ringleader.overview import project_detail


def test_project_detail_reports_checkouts_statuses_and_callouts():
    reg = _reg(
        [
            # two checkouts of one repo, varied statuses
            _thread("a", "blocked", "2026-05-20T00:00:00+00:00", project="/co/c1"),  # 41d → stale
            _thread("b", "unfinished", "2026-06-29T00:00:00+00:00", project="/co/c2"),
            _thread("c", "finished", "2026-06-10T00:00:00+00:00", project="/co/c1"),
        ]
    )
    out = project_detail(reg, NOW)
    assert "checkouts (2)" in out
    assert "/co/c1" in out and "/co/c2" in out
    assert "blocked:1" in out  # status breakdown
    assert "finished:1" in out
    assert "<- BLOCKED" in out  # blocked thread flagged
    assert "stale >21d: 1" in out  # the 41d blocked one
    assert "a-name" in out  # live thread shown by name
    assert "1 finished not shown" in out  # finished collapsed


def test_project_detail_flags_duplicate_names():
    reg = _reg(
        [
            # two distinct sessions whose label collides
            {**_thread("x", "blocked", "2026-06-20T00:00:00+00:00"), "suggestedName": "dup"},
            {**_thread("y", "blocked", "2026-06-21T00:00:00+00:00"), "suggestedName": "dup"},
        ]
    )
    out = project_detail(reg, NOW)
    assert "duplicate names: 1" in out
    assert "dup" in out


def test_project_detail_empty():
    assert "no sessions" in project_detail(_reg([]), NOW).lower()


from ringleader.overview import _human_size


def test_human_size():
    assert _human_size(0) == "0B"
    assert _human_size(512) == "512B"
    assert _human_size(2048) == "2.0KB"
    assert round(float(_human_size(6_000_000)[:-2])) == 6  # ~5.7MB
    assert _human_size(6_000_000).endswith("MB")


def test_render_includes_size_when_present():
    t = _thread("a", "blocked", "2026-06-20T00:00:00+00:00")
    t["sizeBytes"] = 2_500_000
    out = render(_reg([t]), NOW)
    assert "2.4MB" in out


def test_project_detail_shows_per_session_size_and_total():
    a = _thread("a", "blocked", "2026-06-20T00:00:00+00:00")
    a["sizeBytes"] = 1_048_576  # 1.0MB
    b = _thread("b", "unfinished", "2026-06-25T00:00:00+00:00")
    b["sizeBytes"] = 1_048_576  # 1.0MB
    out = project_detail(_reg([a, b]), NOW)
    assert "1.0MB" in out  # per-session size
    assert "total size: 2.0MB" in out  # repo total


def test_render_all_flag_lists_finished():
    fin = _thread("f", "finished", "2026-06-20T00:00:00+00:00")
    live = _thread("b", "blocked", "2026-06-21T00:00:00+00:00")
    reg = _reg([fin, live])
    default = render(reg, NOW)
    allout = render(reg, NOW, show_all=True)
    assert "f-name" not in default  # finished hidden by default
    assert "f-name" in allout  # listed with --all
    assert "b-name" in default and "b-name" in allout


def test_project_detail_all_flag_lists_finished_and_unclassified():
    fin = _thread("f", "finished", "2026-06-20T00:00:00+00:00")
    unc = _thread("u", "unclassified", "2026-06-22T00:00:00+00:00")
    blk = _thread("b", "blocked", "2026-06-21T00:00:00+00:00")
    reg = _reg([fin, unc, blk])
    default = project_detail(reg, NOW)
    allout = project_detail(reg, NOW, show_all=True)
    assert "f-name" not in default and "not shown" in default
    assert "f-name" in allout and "u-name" in allout  # both now listed
    assert "not shown" not in allout


def test_render_cleaned_annotation():
    t = _thread("a", "active", "2026-06-29T00:00:00+00:00", git_key="k1", git_label="repo1")
    reg = {"threads": [t], "lastScan": NOW}
    out = render(reg, NOW, cleaned={"k1": "2026-06-27T00:00:00+00:00"})
    assert "## repo1" in out
    assert "(cleaned" in out  # annotated with review age
    # without the cleaned map, no annotation
    assert "(cleaned" not in render(reg, NOW)


from ringleader.overview import render_triage, triage_count, triage_inbox


def _t(sid, **kw):
    base = {
        "sessionId": sid,
        "mtime": "2026-07-01T00:00:00+00:00",
        "status": "active",
        "title": sid,
        "whatItIs": "w",
        "gitProjectLabel": "repo",
        "project": "/p",
        "snoozedAt": None,
        "decision": None,
        "reviewedAtMtime": None,
    }
    base.update(kw)
    return base


def test_triage_inbox_excludes_snoozed_harvested_and_reviewed():
    threads = [
        _t("new"),  # in: undecided
        _t("snz", snoozedAt="2026-07-02T00:00:00+00:00"),  # out: parked
        _t("hv", decision="harvested"),  # out: harvested
        _t("rev", reviewedAtMtime="2026-07-01T00:00:00+00:00"),  # out: reviewed at mtime
        _t("chg", reviewedAtMtime="2026-06-01T00:00:00+00:00"),  # in: changed since review
    ]
    ids = {t["sessionId"] for t in triage_inbox(threads, NOW)}
    assert ids == {"new", "chg"}
    assert triage_count(threads, NOW) == 2


def test_render_triage_lists_inbox_and_empty_message():
    assert "all triaged" in render_triage([_t("hv", decision="harvested")], NOW).lower()
    out = render_triage([_t("new", whatItIs="a thing")], NOW)
    assert "new" in out and "a thing" in out


def test_render_triage_unclassified_only():
    threads = [
        _t("raw", status="unclassified", whatItIs="never looked at"),
        _t(
            "chg", status="active", reviewedAtMtime="2026-06-01T00:00:00+00:00"
        ),  # classified but changed
    ]
    # default inbox shows both (both awaiting a decision)
    full = render_triage(threads, NOW)
    assert "raw" in full and "chg" in full
    # --unclassified narrows to the never-classified one only
    only = render_triage(threads, NOW, unclassified_only=True)
    assert "raw" in only and "chg" not in only
    assert "never triaged" in only
    # empty case has its own message
    assert "every session has been triaged" in render_triage(
        [_t("a", status="active")], NOW, unclassified_only=True
    )


from ringleader.overview import render_archived, render_rollup


def test_render_rollup_lists_projects_and_count():
    reg = {
        "threads": [
            {
                "sessionId": "a",
                "mtime": "2026-06-20T00:00:00+00:00",
                "status": "active",
                "gitProjectKey": "repo-k",
                "gitProjectLabel": "repo",
                "project": "/p",
                "classifiedAtMtime": "2026-06-20T00:00:00+00:00",
                "title": "a",
                "snoozedAt": None,
            },
        ],
        "projectsCleanedAt": {},
    }
    out = render_rollup(reg, NOW)
    assert "repo" in out and "PROJECT" in out
    assert "1 projects/dirs" in out


def _rollup_reg(mem_entries=None, mem_cleaned=None):
    return {
        "threads": [
            {
                "sessionId": "a",
                "mtime": "2026-06-20T00:00:00+00:00",
                "status": "active",
                "gitProjectKey": "repo-k",
                "gitProjectLabel": "repo",
                "project": "/p",
                "classifiedAtMtime": "2026-06-20T00:00:00+00:00",
                "title": "a",
                "snoozedAt": None,
            },
        ],
        "projectsCleanedAt": {},
        "memoryEntries": mem_entries or [],
        "memoryProjectsCleanedAt": mem_cleaned or {},
    }


def test_render_rollup_mem_column_dashes_when_no_memory():
    out = render_rollup(_rollup_reg(), NOW)
    assert "mem" in out.splitlines()[0]
    assert "—" in out


def test_render_rollup_mem_column_never_when_entries_unstamped():
    reg = _rollup_reg(mem_entries=[{"gitProjectKey": "repo-k", "project": "/p"}])
    out = render_rollup(reg, NOW)
    assert out.count("never") == 2  # both the session and the memory cell
    assert "—" not in out


def test_render_rollup_mem_column_shows_age_when_stamped():
    reg = _rollup_reg(
        mem_entries=[{"gitProjectKey": "repo-k", "project": "/p"}],
        mem_cleaned={"repo-k": "2026-06-20T00:00:00+00:00"},
    )
    out = render_rollup(reg, NOW)
    assert "ago" in out


def test_render_archived_empty_and_populated():
    assert "no archived" in render_archived({"archived": []}).lower()
    reg = {
        "archived": [
            {
                "sessionId": "z",
                "archivedAt": "2026-06-01T12:00:00+00:00",
                "project": "/p",
                "summary": "did a thing",
                "archivePath": "/arc/z.jsonl",
            },
        ]
    }
    out = render_archived(reg)
    assert "2026-06-01" in out and "did a thing" in out and "/arc/z.jsonl" in out
    # selector filters
    assert "no archived" in render_archived(reg, selector="other").lower()
