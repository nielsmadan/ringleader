import pytest
from ringleader import jsonout, overview, projects, registry

NOW = "2026-08-17T00:00:00+00:00"


def _t(sid, **kw):
    base = {
        "sessionId": sid,
        "project": "/p",
        "gitProjectKey": "repo-k",
        "gitProjectLabel": "repo",
        "status": "unfinished",
        "mtime": "2026-08-01T00:00:00+00:00",
        "decision": None,
        "keptAt": None,
        "snoozedAt": None,
        "snoozedUntil": None,
        "reviewedAtMtime": None,
        "whatItIs": f"about {sid}",
        "title": sid,
        "suggestedName": None,
        "sizeBytes": 10,
    }
    base.update(kw)
    return base


def _reg(*threads, archived=None):
    return {"threads": list(threads), "archived": list(archived or []), "deleted": []}


# --- registry helpers -------------------------------------------------------


def test_keep_thread_sets_decision_and_timestamp():
    reg = _reg(_t("s1"))
    previous = registry.keep_thread(reg, "s1", NOW)
    t = reg["threads"][0]
    assert t["decision"] == "keep"
    assert t["keptAt"] == NOW
    assert previous is None


def test_keep_thread_reports_the_decision_it_replaced():
    reg = _reg(_t("s1", decision="harvested"))
    assert registry.keep_thread(reg, "s1", NOW) == "harvested"
    assert reg["threads"][0]["decision"] == "keep"


def test_keep_thread_unknown_session_raises():
    with pytest.raises(KeyError):
        registry.keep_thread(_reg(_t("s1")), "nope", NOW)


def test_unkeep_thread_clears_keep():
    reg = _reg(_t("s1", decision="keep", keptAt=NOW))
    assert registry.unkeep_thread(reg, "s1") is True
    t = reg["threads"][0]
    assert t["decision"] is None
    assert t["keptAt"] is None


def test_unkeep_thread_leaves_a_non_keep_decision_alone():
    reg = _reg(_t("s1", decision="harvested"))
    assert registry.unkeep_thread(reg, "s1") is False
    assert reg["threads"][0]["decision"] == "harvested"


def test_restore_session_pops_the_tombstone():
    ts = {"sessionId": "s1", "reason": "archived", "archivePath": "/a/s1.jsonl"}
    reg = _reg(archived=[ts, {"sessionId": "s2"}])
    assert registry.restore_session(reg, "s1", "/orig/s1.jsonl") == ts
    assert [x["sessionId"] for x in reg["archived"]] == ["s2"]


def test_restore_session_unknown_raises():
    with pytest.raises(KeyError):
        registry.restore_session(_reg(), "nope", "/orig/x.jsonl")


# --- triage inbox contract --------------------------------------------------


def test_triage_inbox_excludes_kept():
    inbox = overview.triage_inbox([_t("kept", decision="keep"), _t("open")], NOW)
    assert [t["sessionId"] for t in inbox] == ["open"]


def test_kept_stays_excluded_after_new_activity():
    """The contract that distinguishes keep from `mark reviewed`: a kept session does
    not return to the inbox when the transcript changes."""
    kept = _t("kept", decision="keep", reviewedAtMtime="2026-08-01T00:00:00+00:00")
    kept["mtime"] = "2026-08-17T00:00:00+00:00"  # brand-new activity
    assert overview.triage_inbox([kept], NOW) == []


def test_unkept_session_returns_to_the_inbox():
    reg = _reg(_t("s1", decision="keep", keptAt=NOW))
    registry.unkeep_thread(reg, "s1")
    assert [t["sessionId"] for t in overview.triage_inbox(reg["threads"], NOW)] == ["s1"]


# --- metrics / views --------------------------------------------------------


def test_kept_counted_but_excluded_from_nag_metrics():
    old = "2026-01-01T00:00:00+00:00"  # far past ABANDON_AGE_DAYS
    m = overview.compute_metrics(_reg(_t("kept", decision="keep", mtime=old), _t("fresh")), NOW)
    assert m["kept_count"] == 1
    assert m["stale_count"] == 0  # the kept one must not be nagged about
    assert m["live_count"] == 2  # but it is still live


def test_render_kept_lists_only_kept():
    out = overview.render_kept([_t("kept", decision="keep"), _t("other")], NOW)
    assert "kept" in out
    assert "other" not in out


def test_render_kept_groups_by_project_with_status_and_kept_age():
    rows = [
        _t("a", decision="keep", keptAt="2026-08-10T00:00:00+00:00"),
        _t("b", decision="keep", keptAt=NOW, gitProjectKey="other-k", gitProjectLabel="elsewhere"),
    ]
    out = overview.render_kept(rows, NOW)
    assert out.startswith("=== kept long-term (2)")
    assert "## repo" in out and "## elsewhere" in out
    assert "[unfinished]" in out
    assert "kept 7d ago" in out
    assert "about a" in out
    assert out.endswith("2 kept · `rl session unkeep <id>` returns one to triage")


def test_render_kept_wraps_long_descriptions():
    what = "word " * 60
    out = overview.render_kept([_t("a", decision="keep", whatItIs=what)], NOW)
    body = [ln for ln in out.splitlines() if ln.startswith(overview.KEPT_DESC_INDENT)]
    assert len(body) > 1
    assert max(len(ln) for ln in body) <= len(overview.KEPT_DESC_INDENT) + overview.KEPT_DESC_W


def test_render_kept_empty_state():
    assert overview.render_kept([_t("other")], NOW) == "no kept sessions"


def test_kept_json_carries_kept_at():
    rows = jsonout.kept_json([_t("kept", decision="keep", keptAt=NOW), _t("other")], NOW)
    assert [r["session_id"] for r in rows] == ["kept"]
    assert rows[0]["kept_at"] == NOW
    assert rows[0]["kept"] is True


def test_session_json_exposes_kept_flag():
    assert jsonout.sessions_json([_t("other")], NOW)[0]["kept"] is False


def test_kept_activity_does_not_flag_the_project():
    """A kept session seeing new activity must not re-raise a project's ⚠ after cleaning."""
    cleaned = "2026-08-10T00:00:00+00:00"
    reg = _reg(_t("kept", decision="keep", mtime="2026-08-16T00:00:00+00:00"))
    reg["projectsCleanedAt"] = {"repo-k": cleaned}
    assert projects.project_summary(reg, NOW)[0].needs_attention is False
