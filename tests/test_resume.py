from __future__ import annotations

import pytest
from ringleader import resume
from ringleader.resume import Scope

NOW = "2026-09-01T00:00:00+00:00"
NEWER = "2026-08-31T00:00:00+00:00"
OLDER = "2026-08-01T00:00:00+00:00"

CLAUDE_DIR = "/home/u/.claude/projects"
DROID_DIR = "/home/u/.factory/sessions"


def _thread(sid, **kw):
    base = {
        "sessionId": sid,
        "source": "claude-code",
        "project": "/w/api",
        "file": f"{CLAUDE_DIR}/-w-api/{sid}.jsonl",
        "gitProjectLabel": "api",
        "gitRoot": "/w/api",
        "status": "unfinished",
        "decision": None,
        "mtime": NEWER,
        "title": None,
        "suggestedName": None,
        "aiTitle": None,
        "whatItIs": None,
        "summary": None,
        "lastPrompt": None,
        "filteredBy": None,
        "snoozedAt": None,
        "snoozedUntil": None,
        "keptAt": None,
    }
    base.update(kw)
    return base


def _reg(*threads, archived=()):
    return {"threads": list(threads), "archived": list(archived), "deleted": []}


def _ids(threads):
    return [t["sessionId"] for t in threads]


# --- scope ------------------------------------------------------------------


def test_cwd_scope_takes_only_sessions_recorded_at_that_exact_directory():
    reg = _reg(_thread("s1", project="/w/api"), _thread("s2", project="/w/api/sub"))
    assert _ids(resume.candidates(reg, scope=Scope.CWD, cwd="/w/api")) == ["s1"]


def test_recursive_scope_takes_the_directory_and_everything_beneath_it():
    reg = _reg(
        _thread("s1", project="/w/api"),
        _thread("s2", project="/w/api/sub"),
        _thread("s3", project="/w/other"),
    )
    got = resume.candidates(reg, scope=Scope.RECURSIVE, cwd="/w/api")
    assert sorted(_ids(got)) == ["s1", "s2"]


def test_recursive_scope_stops_at_the_directory_boundary():
    thread = _thread("s1", project="/w/apiary")
    assert resume.in_scope(thread, Scope.RECURSIVE, "/w/api") is False


def test_a_trailing_slash_on_the_cwd_is_ignored():
    thread = _thread("s1", project="/w/api")
    assert resume.in_scope(thread, Scope.CWD, "/w/api/") is True
    assert resume.in_scope(thread, Scope.RECURSIVE, "/w/api/") is True


def test_all_scope_includes_every_directory_and_sessions_with_no_directory():
    reg = _reg(_thread("s1", project="/w/api"), _thread("s2", project=None))
    got = resume.candidates(reg, scope=Scope.ALL, cwd="/w/other")
    assert sorted(_ids(got)) == ["s1", "s2"]


def test_a_session_with_no_recorded_directory_is_out_of_a_directory_scope():
    thread = _thread("s1", project=None)
    assert resume.in_scope(thread, Scope.CWD, "/w/api") is False
    assert resume.in_scope(thread, Scope.RECURSIVE, "/w/api") is False


# --- eligibility ------------------------------------------------------------


def test_filtered_snoozed_and_kept_sessions_stay_eligible():
    rows = [
        _thread("s1", filteredBy="scratch-*"),
        _thread("s2", snoozedAt=NEWER, snoozedUntil="2026-12-01T00:00:00+00:00"),
        _thread("s3", decision="keep", keptAt=NEWER),
        _thread("s4", decision="harvested"),
        _thread("s5", decision="abandon"),
        _thread("s6", finishOnExit=True),
    ]
    assert [resume.eligible(t) for t in rows] == [True] * 6


def test_a_thread_without_a_session_id_is_not_eligible():
    assert resume.eligible(_thread("")) is False


def test_candidates_leaves_out_a_thread_without_a_session_id():
    reg = _reg(_thread(""), _thread("s2"))
    assert _ids(resume.candidates(reg, scope=Scope.ALL, cwd="/w/api")) == ["s2"]


def test_candidates_draws_only_on_the_live_threads_never_the_archived_ones():
    reg = _reg(_thread("live"), archived=[_thread("archived")])
    assert _ids(resume.candidates(reg, scope=Scope.ALL, cwd="/w/api")) == ["live"]


# --- ordering ---------------------------------------------------------------


def test_candidates_come_back_most_recent_first():
    reg = _reg(_thread("old", mtime=OLDER), _thread("new", mtime=NEWER))
    assert _ids(resume.candidates(reg, scope=Scope.ALL, cwd="/w/api")) == ["new", "old"]


def test_a_session_without_an_mtime_sorts_last():
    reg = _reg(_thread("none", mtime=None), _thread("old", mtime=OLDER))
    assert _ids(resume.candidates(reg, scope=Scope.ALL, cwd="/w/api")) == ["old", "none"]


def test_sessions_sharing_an_mtime_keep_their_registry_order():
    reg = _reg(_thread("a"), _thread("b"), _thread("c"))
    assert _ids(resume.candidates(reg, scope=Scope.ALL, cwd="/w/api")) == ["a", "b", "c"]


def test_candidates_leaves_the_registry_list_in_the_order_it_found_it():
    reg = _reg(_thread("old", mtime=OLDER), _thread("new", mtime=NEWER))
    resume.candidates(reg, scope=Scope.ALL, cwd="/w/api")
    assert _ids(reg["threads"]) == ["old", "new"]


def test_rank_leaves_the_callers_list_in_the_order_it_found_it():
    rows = [_thread("old", title="auth", mtime=OLDER), _thread("new", title="auth", mtime=NEWER)]
    resume.rank(rows, "auth")
    resume.rank(rows, "")
    assert _ids(rows) == ["old", "new"]


# --- scoring ----------------------------------------------------------------


def test_the_scorer_accepts_a_subsequence():
    assert resume.score("ath", "auth-flow") is not None


def test_the_scorer_returns_none_when_the_characters_are_out_of_order():
    assert resume.score("hta", "auth-flow") is None


def test_the_scorer_returns_none_when_a_character_is_missing():
    assert resume.score("authz", "auth-flow") is None


def test_the_scorer_is_case_insensitive():
    assert resume.score("AUTH", "auth-flow") == resume.score("auth", "AUTH-FLOW")


def test_an_empty_query_scores_zero_against_anything():
    assert resume.score("", "auth-flow") == 0
    assert resume.score("", "") == 0


def test_a_consecutive_run_beats_a_scattered_match():
    # same length, same first-match index, same boundary count: only the run differs
    run = resume.score("abc", "xxabcxxx")
    scattered = resume.score("abc", "xxaxbxcx")
    assert run > scattered


def test_a_literal_run_late_in_the_text_beats_an_earlier_scattered_match():
    literal = resume.score("auth", "a full rewrite of the auth layer")
    scattered = resume.score("auth", "a u t h")
    assert literal > scattered


def test_a_literal_run_early_in_the_text_beats_a_later_scattered_match():
    """The mirror of the case above. Scanning from one end only is blind to a run at the
    other, so both directions are tried and the better alignment wins."""
    literal = resume.score("auth", "auth layer and then a u t h")
    scattered = resume.score("auth", "a u t h and then auth layer")
    assert literal > scattered


def test_a_word_boundary_match_beats_a_mid_word_match():
    boundary = resume.score("flow", "auth flowers")
    midword = resume.score("flow", "authxflowers")
    assert boundary > midword


def test_an_earlier_first_match_wins():
    early = resume.score("auth", "auth rewrite xxxxxxxxxx")
    late = resume.score("auth", "xxxxxxxxxx rewrite auth")
    assert early > late


def test_a_shorter_text_wins_when_otherwise_equal():
    assert resume.score("auth", "auth") > resume.score("auth", "auth-flow-rewrite")


# --- match tiers ------------------------------------------------------------


def test_a_name_match_outranks_a_better_blurb_match():
    thread = _thread("s1", title="a much longer u name t with h scattered", summary="auth")
    got = resume.match(thread, "auth")
    assert got is not None
    assert got[1] == "name"


def test_an_id_match_outranks_a_better_blurb_match():
    thread = _thread("auth-deadbeef", title="zzz", summary="auth")
    got = resume.match(thread, "auth")
    assert got is not None
    assert got[1] == "id"


def test_a_blurb_match_reports_the_field_it_hit():
    thread = _thread("s1", title="zzz", whatItIs="rewrites the auth flow")
    got = resume.match(thread, "auth")
    assert got is not None
    assert got[1] == "whatItIs"


def test_the_earlier_blurb_field_wins_an_otherwise_equal_match():
    thread = _thread("s1", title="zzz", aiTitle="auth", whatItIs="auth")
    got = resume.match(thread, "auth")
    assert got is not None
    assert got[1] == "aiTitle"


def test_match_returns_none_when_no_field_matches():
    assert resume.match(_thread("s1", title="zzz"), "auth") is None


def test_the_unknown_placeholder_name_is_not_matchable():
    assert resume.label({}) == "(unknown)"
    assert resume.match({}, "unknown") is None


def test_a_blurb_carrying_control_characters_still_matches():
    thread = _thread("s1", title="zzz", summary="rewrites\x1b[31m the auth flow")
    got = resume.match(thread, "auth")
    assert got is not None
    assert got[1] == "summary"


# --- rank -------------------------------------------------------------------


def test_rank_without_a_query_is_most_recent_first():
    rows = [_thread("old", mtime=OLDER), _thread("new", mtime=NEWER)]
    assert _ids(resume.rank(rows, "")) == ["new", "old"]


def test_rank_treats_a_blank_query_as_no_query():
    rows = [_thread("old", mtime=OLDER), _thread("new", mtime=NEWER)]
    assert _ids(resume.rank(rows, "   ")) == ["new", "old"]


def test_rank_keeps_only_matching_threads():
    rows = [_thread("hit", title="auth flow"), _thread("miss", title="zzz")]
    assert _ids(resume.rank(rows, "auth")) == ["hit"]


def test_rank_puts_a_name_match_above_a_blurb_match_however_old():
    rows = [
        _thread("blurb", summary="auth", mtime=NEWER),
        _thread("name", title="auth", mtime=OLDER),
    ]
    assert _ids(resume.rank(rows, "auth")) == ["name", "blurb"]


def test_rank_keeps_a_literal_blurb_run_and_drops_a_scattered_one():
    rows = [
        _thread("real", summary="a full rewrite of the auth layer", mtime=OLDER),
        _thread("noise", summary="a u t h", mtime=NEWER),
    ]
    assert _ids(resume.rank(rows, "auth")) == ["real"]


def test_an_exact_name_ranks_first_however_old():
    rows = [
        _thread("blurb", title="zzz", summary="follow up on sd-review", mtime=NEWER),
        _thread("longer", title="sd-review-followup", mtime=NEWER),
        _thread("fuzzy", title="sd-xx-review", mtime=NEWER),
        _thread("exact", title="SD-Review", mtime=OLDER),
    ]
    assert _ids(resume.rank(rows, "sd-review")) == ["exact", "longer", "fuzzy", "blurb"]


def test_a_name_prefix_outranks_a_name_substring_which_outranks_a_fuzzy_name():
    rows = [
        _thread("fuzzy", title="a-u-t-h", mtime=NEWER),
        _thread("substring", title="oauth-fix", mtime=NEWER),
        _thread("prefix", title="auth-fix", mtime=OLDER),
    ]
    assert _ids(resume.rank(rows, "auth")) == ["prefix", "substring", "fuzzy"]


def test_letters_scattered_through_a_blurb_do_not_match():
    thread = _thread("s1", title="zzz", summary="give the view a walkthrough", gitProjectLabel="x")
    assert resume.match(thread, "gvw") is None


def test_a_real_word_from_the_summary_still_matches():
    thread = _thread("s1", title="zzz", summary="give the view a walkthrough", gitProjectLabel="x")
    got = resume.match(thread, "walk")
    assert got is not None
    assert got[1] == "summary"


def test_every_word_of_a_blurb_query_must_appear_but_in_any_order_or_field():
    thread = _thread("s1", title="zzz", whatItIs="login flow", summary="add a retry")
    assert resume.match(thread, "retry login") is not None
    assert resume.match(thread, "retry logout") is None


def test_a_multi_word_query_whose_words_are_all_in_the_name_is_a_name_match():
    got = resume.match(_thread("s1", title="sd-review"), "review sd")
    assert got is not None
    assert got[1] == "name"


def test_the_session_id_matches_by_prefix_only():
    thread = _thread("deadbeef-cafe", title="zzz", gitProjectLabel="x")
    assert resume.match(thread, "deadb") == resume.match(thread, "DEADB")
    assert resume.match(thread, "deadb")[1] == "id"
    assert resume.match(thread, "cafe") is None


def test_the_project_label_matches_fuzzily():
    got = resume.match(_thread("s1", title="zzz", gitProjectLabel="ringleader"), "rgldr")
    assert got is not None
    assert got[1] == "project"


def test_rank_reads_text_through_the_supplied_haystack_factory():
    thread = _thread("s1", title="zzz")
    haystack = resume.Haystack.of(name="Cached", sid="s1", project="", blurbs=())
    assert _ids(resume.rank([thread], "cached", lambda _: haystack)) == ["s1"]


def test_haystack_of_lowercases_every_field():
    got = resume.Haystack.of(name="AbC", sid="DEF", project="GhI", blurbs=[("summary", "JkL")])
    assert (got.name, got.sid, got.project, got.blurbs) == (
        "abc",
        "def",
        "ghi",
        (("summary", "jkl"),),
    )


@pytest.mark.parametrize("field", ["name", "sid", "project"])
def test_haystack_refuses_unlowered_text(field):
    fields = {"name": "", "sid": "", "project": "", field: "Mixed"}
    with pytest.raises(ValueError, match="lowercased"):
        resume.Haystack(**fields, blurbs=())


def test_haystack_refuses_unlowered_blurbs():
    with pytest.raises(ValueError, match="lowercased"):
        resume.Haystack(name="", sid="", project="", blurbs=(("summary", "Mixed"),))


def test_within_a_tier_the_most_recent_session_comes_first():
    rows = [
        _thread("old", title="auth flow", mtime=OLDER),
        _thread("new", title="auth flow", mtime=NEWER),
    ]
    assert _ids(resume.rank(rows, "auth")) == ["new", "old"]


# --- label ------------------------------------------------------------------


def test_label_prefers_the_title():
    assert resume.label(_thread("s1", title="Auth Flow", suggestedName="other")) == "Auth Flow"


def test_label_falls_back_to_the_suggested_name_then_the_ai_title():
    assert resume.label(_thread("s1", suggestedName="suggested")) == "suggested"
    assert resume.label(_thread("s1", aiTitle="ai title")) == "ai title"


def test_label_falls_back_to_the_first_eight_characters_of_the_id():
    assert resume.label(_thread("0123456789abcdef")) == "01234567"


def test_label_strips_control_characters_and_collapses_whitespace():
    assert resume.label(_thread("s1", title="auth\x1b\n  flow")) == "auth flow"


# --- badges -----------------------------------------------------------------


def test_a_filtered_session_is_badged_filtered():
    assert resume.badges(_thread("s1", filteredBy="scratch-*"), NOW) == ["filtered"]


def test_a_snoozed_session_is_badged_snoozed():
    thread = _thread("s1", snoozedAt=OLDER, snoozedUntil="2026-12-01T00:00:00+00:00")
    assert resume.badges(thread, NOW) == ["snoozed"]


def test_a_session_snoozed_past_its_wake_date_carries_no_badge():
    thread = _thread("s1", snoozedAt=OLDER, snoozedUntil=NEWER)
    assert resume.badges(thread, NOW) == []


def test_a_kept_session_is_badged_kept():
    assert resume.badges(_thread("s1", decision="keep", keptAt=NEWER), NOW) == ["kept"]


def test_a_session_marked_to_finish_on_exit_is_badged_finishing():
    assert resume.badges(_thread("s1", finishOnExit=True), NOW) == ["finishing"]


def test_a_harvested_session_is_badged_harvested():
    assert resume.badges(_thread("s1", decision="harvested"), NOW) == ["harvested"]


def test_an_abandoned_session_is_badged_abandoned():
    assert resume.badges(_thread("s1", decision="abandon"), NOW) == ["abandoned"]


def test_a_long_dormant_unfinished_session_is_badged_abandoned():
    assert resume.badges(_thread("s1", status="unfinished", mtime=OLDER), NOW) == ["abandoned"]


def test_a_session_whose_cwd_drifted_from_its_launch_dir_is_badged_drifted():
    thread = _thread("s1", project="/w/api/sub", file=f"{CLAUDE_DIR}/-w-api/s1.jsonl")
    assert resume.badges(thread, NOW) == ["drifted"]


def test_badges_come_back_in_a_fixed_order():
    thread = _thread(
        "s1",
        project="/w/api/sub",
        file=f"{CLAUDE_DIR}/-w-api/s1.jsonl",
        filteredBy="scratch-*",
        snoozedAt=OLDER,
        snoozedUntil="2026-12-01T00:00:00+00:00",
        decision="keep",
        keptAt=NEWER,
        finishOnExit=True,
    )
    assert resume.badges(thread, NOW) == ["filtered", "snoozed", "kept", "finishing", "drifted"]


def test_a_status_badge_sits_between_finishing_and_drifted():
    thread = _thread(
        "s1",
        project="/w/api/sub",
        file=f"{CLAUDE_DIR}/-w-api/s1.jsonl",
        filteredBy="scratch-*",
        snoozedAt=OLDER,
        snoozedUntil="2026-12-01T00:00:00+00:00",
        decision="harvested",
        finishOnExit=True,
    )
    assert resume.badges(thread, NOW) == [
        "filtered",
        "snoozed",
        "finishing",
        "harvested",
        "drifted",
    ]


def test_badges_still_render_for_a_session_with_no_usable_mtime():
    missing = {"sessionId": "s1", "status": "unfinished", "finishOnExit": True}
    assert resume.badges(missing, NOW) == ["finishing"]
    assert resume.badges({**missing, "mtime": None}, NOW) == ["finishing"]


def test_a_decided_session_keeps_its_status_badge_with_no_usable_mtime():
    """`effective_status` settles a harvested or abandoned session from `decision` before it
    ever reads `mtime`, so guarding the crash must not cost these sessions their badge."""
    assert resume.badges({"sessionId": "s1", "decision": "harvested"}, NOW) == ["harvested"]
    assert resume.badges({"sessionId": "s1", "decision": "abandon"}, NOW) == ["abandoned"]


def test_a_quiet_session_carries_no_badges():
    assert resume.badges(_thread("s1"), NOW) == []


# --- drift and launch directory ---------------------------------------------


def test_a_session_stored_under_its_own_cwd_slug_has_not_drifted():
    assert resume.drifted(_thread("s1", project="/w/api")) is False


def test_a_session_whose_agent_files_transcripts_flat_cannot_drift():
    thread = _thread("s1", source="codex", project="/w/api/sub", file="/c/sessions/s1.jsonl")
    assert resume.drifted(thread) is False


def test_a_droid_session_drifts_the_same_way_a_claude_one_does():
    """Droid files a transcript under the launch directory's slug too, so a session
    that cd'd needs the same badge and the same chdir before `droid resume`."""
    thread = _thread(
        "s1", source="droid", project="/w/api/sub", file=f"{DROID_DIR}/-w-api/s1.jsonl"
    )

    assert resume.drifted(thread) is True
    assert resume.badges(thread, NOW) == ["drifted"]


def test_a_droid_session_at_the_global_sessions_root_has_no_slug_to_drift_from(
    tmp_path, monkeypatch
):
    """Droid also writes `sessions/<id>.jsonl`, with no launch-dir slug above it. There
    is nothing to compare the recorded cwd against, so the badge would be a guess."""
    monkeypatch.setenv("FACTORY_HOME_OVERRIDE", str(tmp_path))
    root = tmp_path / ".factory" / "sessions"
    thread = _thread("s1", source="droid", project="/w/api/sub", file=str(root / "s1.jsonl"))

    assert resume.drifted(thread) is False
    assert resume.badges(thread, NOW) == []


def test_a_droid_session_stored_under_its_own_cwd_slug_has_not_drifted():
    thread = _thread("s1", source="droid", project="/w/api", file=f"{DROID_DIR}/-w-api/s1.jsonl")

    assert resume.drifted(thread) is False


def test_a_droid_session_keeps_underscores_dots_and_spaces_in_its_slug():
    thread = _thread(
        "s1", source="droid", project="/w/my_api.v2 x", file=f"{DROID_DIR}/-w-my_api.v2 x/s1.jsonl"
    )

    assert resume.drifted(thread) is False


def test_a_drifted_droid_session_recovers_its_launch_directory_from_a_sibling():
    drifted = _thread(
        "s1", source="droid", project="/w/api/sub", file=f"{DROID_DIR}/-w-api/s1.jsonl"
    )
    sibling = _thread("s2", source="droid", project="/w/api", file=f"{DROID_DIR}/-w-api/s2.jsonl")

    assert resume.launch_cwd(drifted, [drifted, sibling]) == "/w/api"


def test_a_claude_session_without_a_transcript_file_has_not_drifted():
    assert resume.drifted(_thread("s1", file=None)) is False


def test_a_claude_session_without_a_recorded_directory_has_not_drifted():
    assert resume.drifted(_thread("s1", project=None)) is False


def test_launch_cwd_is_the_recorded_directory_when_nothing_drifted():
    assert resume.launch_cwd(_thread("s1", project="/w/api")) == "/w/api"


def test_launch_cwd_is_none_when_no_directory_was_recorded():
    assert resume.launch_cwd(_thread("s1", project=None)) is None


def test_a_drifted_session_recovers_its_launch_directory_from_a_sibling():
    drifted = _thread("s1", project="/w/api/sub", file=f"{CLAUDE_DIR}/-w-api/s1.jsonl")
    sibling = _thread("s2", project="/w/api", file=f"{CLAUDE_DIR}/-w-api/s2.jsonl")
    assert resume.launch_cwd(drifted, [drifted, sibling]) == "/w/api"


def test_recovery_ignores_a_sibling_in_a_different_storage_directory():
    drifted = _thread("s1", project="/w/api/sub", file=f"{CLAUDE_DIR}/-w-api/s1.jsonl")
    elsewhere = _thread("s2", project="/w/other", file=f"{CLAUDE_DIR}/-w-other/s2.jsonl")
    assert resume.launch_cwd(drifted, [drifted, elsewhere]) == "/w/api/sub"


def test_a_drifted_session_falls_back_to_its_recorded_directory():
    drifted = _thread("s1", project="/w/api/sub", file=f"{CLAUDE_DIR}/-w-api/s1.jsonl")
    assert resume.launch_cwd(drifted, [drifted]) == "/w/api/sub"


def test_the_agent_and_source_names_of_claude_code_map_onto_each_other():
    """Presence and `--to` speak `claude`; the registry and the sources speak
    `claude-code`. Both halves live here so neither can drift from the other."""
    assert resume.agent_name("claude-code") == "claude"
    assert resume.source_name("claude") == "claude-code"
    for source in resume.launchable_agents():
        assert resume.source_name(resume.agent_name(source)) == source
