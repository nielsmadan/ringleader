import json

import pytest
from ringleader import search, searchview, tty
from ringleader.commands import search as search_cmd

OLDER = "2026-08-01T00:00:00+00:00"
NEWER = "2026-09-01T00:00:00+00:00"


def _t(sid, **kw):
    base = {
        "sessionId": sid,
        "source": "claude-code",
        "project": "/w/api",
        "gitProjectLabel": "api",
        "status": "unfinished",
        "decision": None,
        "mtime": NEWER,
        "title": None,
        "suggestedName": None,
        "aiTitle": None,
        "whatItIs": None,
        "summary": None,
        "lastPrompt": None,
        "sizeBytes": 10,
        "filteredBy": None,
    }
    base.update(kw)
    return base


def _reg(*threads, archived=(), deleted=()):
    return {"threads": list(threads), "archived": list(archived), "deleted": list(deleted)}


def _ids(matches):
    return [m.thread["sessionId"] for m in matches]


# --- matching ---------------------------------------------------------------


def test_a_name_match_is_a_case_insensitive_substring():
    reg = _reg(_t("s1", title="Refactor-Auth-Flow"))
    [m] = search.search(reg, "auth")
    assert m.thread["sessionId"] == "s1"
    assert m.matched_on == "name"
    assert m.value == "Refactor-Auth-Flow"
    assert m.span == (9, 13)


def test_a_wildcard_query_is_an_anchored_glob():
    reg = _reg(_t("s1", title="auth-token-expiry"), _t("s2", title="refactor-auth-flow"))
    assert _ids(search.search(reg, "auth-*")) == ["s1"]


def test_a_glob_match_spans_the_whole_field():
    reg = _reg(_t("s1", title="auth-token-expiry"))
    [m] = search.search(reg, "auth-*")
    assert m.span == (0, len("auth-token-expiry"))


def test_the_suggested_name_is_matched_when_there_is_no_title():
    reg = _reg(_t("s1", title=None, suggestedName="auth-flow"))
    [m] = search.search(reg, "auth")
    assert m.matched_on == "name"
    assert m.value == "auth-flow"


def test_a_session_id_prefix_matches():
    reg = _reg(_t("3f2a91c7-aaaa"))
    [m] = search.search(reg, "3f2A")
    assert m.matched_on == "id"
    assert m.span == (0, 4)


def test_an_id_match_needs_a_prefix_not_a_substring():
    reg = _reg(_t("3f2a91c7-aaaa"))
    assert search.search(reg, "91c7") == []


def test_a_blurb_match_records_which_field_matched():
    reg = _reg(
        _t("s1", title="login-page-tweak", summary="swapped the auth middleware for a new one")
    )
    [m] = search.search(reg, "auth")
    assert m.matched_on == "summary"
    assert m.value == "swapped the auth middleware for a new one"
    assert m.span == (12, 16)


def test_every_blurb_field_is_searched():
    reg = _reg(
        _t("s1", aiTitle="auth work"),
        _t("s2", whatItIs="about auth"),
        _t("s3", summary="auth again"),
        _t("s4", lastPrompt="fix auth"),
    )
    assert sorted(_ids(search.search(reg, "auth"))) == ["s1", "s2", "s3", "s4"]


def test_a_name_match_outranks_a_blurb_match_on_a_newer_session():
    reg = _reg(
        _t("blurb", title="unrelated", summary="auth stuff", mtime=NEWER),
        _t("named", title="auth-flow", mtime=OLDER),
    )
    assert _ids(search.search(reg, "auth")) == ["named", "blurb"]


def test_a_name_match_outranks_an_id_match():
    reg = _reg(
        _t("authbeef-1111", mtime=NEWER),
        _t("s2", title="auth-flow", mtime=OLDER),
    )
    assert _ids(search.search(reg, "auth")) == ["s2", "authbeef-1111"]


def test_within_a_tier_the_most_recent_session_comes_first():
    reg = _reg(_t("old", title="auth-a", mtime=OLDER), _t("new", title="auth-b", mtime=NEWER))
    assert _ids(search.search(reg, "auth")) == ["new", "old"]


def test_no_match_returns_no_results():
    assert search.search(_reg(_t("s1", title="unrelated")), "auth") == []


# --- scope ------------------------------------------------------------------


def _archived(sid, thread, **kw):
    rec = {
        "sessionId": sid,
        "archivedAt": OLDER,
        "archivePath": f"/arch/{sid}.jsonl",
        "summary": None,
        "project": "/w/api",
        "thread": thread,
    }
    rec.update(kw)
    return rec


def test_archived_sessions_are_searched_and_flagged():
    reg = _reg(archived=[_archived("a1", _t("a1", title="auth-spike"))])
    [m] = search.search(reg, "auth")
    assert m.archived is True
    assert m.matched_on == "name"


def test_an_archived_record_without_an_embedded_thread_matches_its_own_summary():
    reg = _reg(archived=[_archived("a2", None, summary="auth spike")])
    [m] = search.search(reg, "auth")
    assert m.archived is True
    assert m.matched_on == "summary"
    assert m.thread["sessionId"] == "a2"


def test_name_filtered_sessions_are_searched_and_flagged():
    reg = _reg(_t("s1", title="auth-probe", filteredBy="[cadet]*"))
    [m] = search.search(reg, "auth")
    assert m.filtered is True


def test_deleted_tombstones_are_not_searched():
    reg = _reg(deleted=[{"sessionId": "authbeef-1111", "harvestPath": None}])
    assert search.search(reg, "auth") == []


def test_scope_live_excludes_archived_sessions():
    reg = _reg(_t("s1", title="auth-flow"), archived=[_archived("a1", _t("a1", title="auth-old"))])
    assert _ids(search.search(reg, "auth", scope=search.Scope.LIVE)) == ["s1"]


def test_scope_archived_excludes_live_sessions():
    reg = _reg(_t("s1", title="auth-flow"), archived=[_archived("a1", _t("a1", title="auth-old"))])
    assert _ids(search.search(reg, "auth", scope=search.Scope.ARCHIVED)) == ["a1"]


# --- rendering --------------------------------------------------------------

NOW = "2026-09-04T00:00:00+00:00"


def _render(reg, query, **kw):
    return searchview.render_search(search.search(reg, query), NOW, query, **kw)


def test_a_row_shows_the_name_source_age_status_and_project():
    reg = _reg(_t("s1", title="refactor-auth-flow", project="/w/api"))
    out = _render(reg, "auth")
    assert "refactor-auth-flow · claude · 3d · unfinished · /w/api" in out


def test_a_row_shows_the_full_session_id():
    reg = _reg(_t("3f2a91c7-8b4e-4c1a-9f3d-2e5a7c9b1d40", title="auth-flow"))
    assert "    3f2a91c7-8b4e-4c1a-9f3d-2e5a7c9b1d40" in _render(reg, "auth")


def test_an_archived_row_is_labeled_instead_of_showing_a_status():
    reg = _reg(archived=[_archived("a1", _t("a1", title="auth-spike", mtime=OLDER))])
    out = _render(reg, "auth")
    assert "auth-spike · claude · 34d · [archived] · /w/api" in out


def test_an_archived_rows_age_is_last_activity_not_the_archive_date():
    reg = _reg(archived=[_archived("a1", _t("a1", title="auth-spike", mtime=NEWER))])
    assert "auth-spike · claude · 3d · [archived]" in _render(reg, "auth")


def test_a_tombstone_without_a_thread_ages_from_its_archive_date():
    reg = _reg(archived=[_archived("a2", None, summary="auth spike")])
    assert "(unnamed) · 34d · [archived]" in _render(reg, "auth")


def test_a_filtered_row_is_labeled():
    reg = _reg(_t("s1", title="auth-probe", filteredBy="[cadet]*"))
    assert "[filtered]" in _render(reg, "auth")


def test_a_blurb_match_adds_a_snippet_line_naming_the_field():
    reg = _reg(_t("s1", title="login-page-tweak", summary="swapped the auth middleware"))
    assert "    ↳ summary: swapped the auth middleware" in _render(reg, "auth")


def test_a_long_blurb_snippet_is_trimmed_around_the_match():
    summary = "x" * 200 + " auth " + "y" * 200
    reg = _reg(_t("s1", title="unrelated", summary=summary))
    line = next(x for x in _render(reg, "auth").splitlines() if "↳" in x)
    assert "auth" in line
    assert line.startswith("    ↳ summary: …") and line.endswith("…")
    # 60 chars of context plus an ellipsis at each end
    assert len(line) == len("    ↳ summary: ") + 62


def test_an_unnamed_session_renders_as_unnamed():
    reg = _reg(_t("authbeef-1111", title=None, suggestedName=None))
    assert "(unnamed) · claude" in _render(reg, "auth")


def test_the_header_counts_the_matches():
    reg = _reg(_t("s1", title="auth-a"), _t("s2", title="auth-b"))
    assert _render(reg, "auth").splitlines()[0] == '=== search "auth" — 2 matches ==='


def test_the_header_is_singular_for_one_match():
    reg = _reg(_t("s1", title="auth-a"))
    assert _render(reg, "auth").splitlines()[0] == '=== search "auth" — 1 match ==='


def test_no_matches_renders_a_plain_message():
    assert searchview.render_search([], NOW, "auth") == 'no sessions matching "auth"'


def test_limit_keeps_the_highest_ranked_rows_and_reports_the_remainder():
    reg = _reg(
        *[
            _t(f"s{i}", title=f"auth-{i}", mtime=f"2026-09-0{i + 1}T00:00:00+00:00")
            for i in range(5)
        ]
    )
    out = _render(reg, "auth", limit=2)
    assert "auth-4" in out and "auth-3" in out
    assert "auth-2" not in out
    assert "3 more" in out


# --- highlighting -----------------------------------------------------------


def test_color_wraps_the_matched_name_span_in_ansi_bold():
    reg = _reg(_t("s1", title="refactor-auth-flow"))
    assert "refactor-\033[1mauth\033[0m-flow" in _render(reg, "auth", color=True)


def test_without_color_the_name_renders_plain():
    reg = _reg(_t("s1", title="refactor-auth-flow"))
    out = _render(reg, "auth", color=False)
    assert "refactor-auth-flow · claude" in out
    assert "\033[" not in out


def test_an_id_match_highlights_the_id_not_the_name():
    reg = _reg(_t("authbeef-1111"))
    out = _render(reg, "auth", color=True)
    assert "    \033[1mauth\033[0mbeef-1111" in out
    assert "(unnamed)" in out


def test_a_blurb_match_highlights_inside_the_snippet():
    reg = _reg(_t("s1", title="unrelated", summary="swapped the auth middleware"))
    assert "swapped the \033[1mauth\033[0m middleware" in _render(reg, "auth", color=True)


def test_a_glob_match_highlights_the_whole_name():
    reg = _reg(_t("s1", title="auth-token-expiry"))
    assert "\033[1mauth-token-expiry\033[0m" in _render(reg, "auth-*", color=True)


# --- the command ------------------------------------------------------------


def _registry_file(tmp_path, reg):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(reg), encoding="utf-8")
    return str(path)


def _run(tmp_path, capsys, reg, *args):
    rc = search_cmd.cmd_search([*args, "--registry", _registry_file(tmp_path, reg)])
    return rc, capsys.readouterr().out


def test_the_command_prints_matching_rows(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, _reg(_t("s1", title="auth-flow")), "auth")
    assert rc == 0
    assert "auth-flow" in out


def test_no_matches_is_reported_without_failing(tmp_path, capsys):
    rc, out = _run(tmp_path, capsys, _reg(_t("s1", title="unrelated")), "auth")
    assert rc == 0
    assert out.strip() == 'no sessions matching "auth"'


def test_json_output_carries_the_match_metadata(tmp_path, capsys):
    reg = _reg(_t("s1", title="login-page", summary="swapped the auth middleware"))
    _, out = _run(tmp_path, capsys, reg, "auth", "--json")
    payload = json.loads(out)
    assert payload["total"] == 1
    [row] = payload["matches"]
    assert row["session_id"] == "s1"
    assert row["name"] == "login-page"
    assert row["matched_on"] == "summary"
    assert row["matched_text"] == "swapped the auth middleware"
    assert row["archived"] is False
    assert row["filtered"] is False


def test_json_output_stays_free_of_escape_codes_under_color_always(tmp_path, capsys):
    reg = _reg(_t("s1", title="auth-flow"))
    _, out = _run(tmp_path, capsys, reg, "auth", "--json", "--color", "always")
    [row] = json.loads(out)["matches"]
    assert row["name"] == "auth-flow"
    assert row["matched_text"] == "auth-flow"
    assert "\033[" not in out


def test_color_always_highlights_the_match(tmp_path, capsys):
    reg = _reg(_t("s1", title="auth-flow"))
    _, out = _run(tmp_path, capsys, reg, "auth", "--color", "always")
    assert "\033[1mauth\033[0m-flow" in out


def test_color_never_renders_the_same_row_unhighlighted(tmp_path, capsys):
    reg = _reg(_t("s1", title="auth-flow"))
    _, out = _run(tmp_path, capsys, reg, "auth", "--color", "never")
    assert "auth-flow · claude" in out
    assert "\033[" not in out


def test_live_and_archived_flags_narrow_the_scope(tmp_path, capsys):
    reg = _reg(_t("s1", title="auth-live"), archived=[_archived("a1", _t("a1", title="auth-old"))])
    _, live = _run(tmp_path, capsys, reg, "auth", "--live")
    _, archived = _run(tmp_path, capsys, reg, "auth", "--archived")
    assert "auth-live" in live and "auth-old" not in live
    assert "auth-old" in archived and "auth-live" not in archived


def test_limit_caps_the_rows_the_command_prints(tmp_path, capsys):
    reg = _reg(
        *[
            _t(f"s{i}", title=f"auth-{i}", mtime=f"2026-09-0{i + 1}T00:00:00+00:00")
            for i in range(5)
        ]
    )
    _, out = _run(tmp_path, capsys, reg, "auth", "--limit", "2")
    assert "auth-4" in out and "auth-3" in out
    assert "auth-2" not in out
    assert "3 more" in out


@pytest.mark.parametrize(
    "mode,is_tty,env,expected",
    [
        ("always", False, {}, True),
        ("never", True, {}, False),
        ("auto", True, {}, True),
        ("auto", False, {}, False),
        ("auto", True, {"NO_COLOR": "1"}, False),
    ],
)
def test_auto_color_follows_the_tty_and_no_color(mode, is_tty, env, expected):
    class Stream:
        def isatty(self):
            return is_tty

    assert tty.want_color(mode, Stream(), env) is expected


def test_a_blurb_snippet_is_collapsed_onto_one_line():
    reg = _reg(_t("s1", title="unrelated", lastPrompt="fix the auth\nbug\n\n  please"))
    out = _render(reg, "auth")
    assert "    ↳ last prompt: fix the auth bug please" in out
    assert len([x for x in out.splitlines() if "↳" in x]) == 1


# --- review fixes -----------------------------------------------------------


def test_a_span_indexes_the_original_value_not_a_lowercased_copy():
    # "İ".lower() is two code points, so an index into the lowered copy is not an
    # index into the original
    reg = _reg(_t("s1", title="İstanbul-auth-flow"))
    [m] = search.search(reg, "auth")
    assert m.value[m.span[0] : m.span[1]] == "auth"


def test_a_session_in_both_buckets_yields_one_row():
    thread = _t("dup", title="auth-dup")
    reg = _reg(thread, archived=[_archived("dup", dict(thread))])
    matches = search.search(reg, "auth")
    assert _ids(matches) == ["dup"]
    assert matches[0].archived is False


def test_an_explicit_archived_scope_still_finds_a_session_that_is_also_live():
    thread = _t("dup", title="auth-dup")
    reg = _reg(thread, archived=[_archived("dup", dict(thread))])
    assert _ids(search.search(reg, "auth", scope=search.Scope.ARCHIVED)) == ["dup"]


def test_a_tombstone_summary_is_searchable_even_when_a_thread_is_embedded():
    # archive.py derives a summary from a sidecar when whatItIs is absent, and
    # stores it only on the tombstone
    thread = _t("a1", title="unrelated", summary=None)
    reg = _reg(archived=[_archived("a1", thread, summary="swapped the auth middleware")])
    [m] = search.search(reg, "auth")
    assert m.matched_on == "summary"


def test_a_thread_less_tombstone_does_not_invent_a_source():
    reg = _reg(archived=[_archived("a2", None, summary="auth spike")])
    [m] = search.search(reg, "auth")
    assert m.thread.get("source") is None


def test_a_row_omits_the_badge_when_the_source_is_unknown():
    reg = _reg(archived=[_archived("a2", None, summary="auth spike")])
    out = _render(reg, "auth")
    assert "claude" not in out
    assert "(unnamed) · 34d · [archived] · /w/api" in out


def test_a_glob_match_on_a_session_id_prefix_is_supported():
    reg = _reg(_t("authbeef-1111", title=None))
    [m] = search.search(reg, "auth*")
    assert m.matched_on == "id"


def test_a_glob_blurb_match_does_not_bold_the_trailing_ellipsis():
    reg = _reg(_t("s1", title="unrelated", summary="z" * 300))
    out = _render(reg, "z*", color=True)
    assert "…\033[0m" not in out
    assert out.rstrip().endswith("…")


def test_a_decision_shows_through_in_the_status_column():
    reg = _reg(_t("s1", title="auth-flow", decision="harvested"))
    assert "auth-flow · claude · 3d · harvested · /w/api" in _render(reg, "auth")


def test_control_characters_never_reach_the_rendered_row():
    reg = _reg(_t("s1", title="unrelated", summary="hi \x1b]0;pwned\x07 auth there"))
    out = _render(reg, "auth")
    assert "    ↳ summary: hi ]0;pwned auth there" in out
    assert "\x1b]" not in out and "\x07" not in out


def test_a_snippet_at_the_start_of_a_long_value_has_no_leading_ellipsis():
    reg = _reg(_t("s1", title="unrelated", summary="auth " + "y" * 300))
    line = next(x for x in _render(reg, "auth").splitlines() if "↳" in x)
    assert line.startswith("    ↳ summary: auth ")
    assert line.endswith("…")


def test_a_snippet_at_the_end_of_a_long_value_has_no_trailing_ellipsis():
    reg = _reg(_t("s1", title="unrelated", summary="x" * 300 + " auth"))
    line = next(x for x in _render(reg, "auth").splitlines() if "↳" in x)
    assert line.startswith("    ↳ summary: …")
    assert line.endswith(" auth")


def test_a_snippet_at_the_end_still_highlights_the_match():
    reg = _reg(_t("s1", title="unrelated", summary="x" * 300 + " auth"))
    assert "\033[1mauth\033[0m" in _render(reg, "auth", color=True)


def test_a_whitespace_only_query_matches_nothing():
    reg = _reg(_t("s1", title="a  b"))
    assert search.search(reg, "   ") == []


def test_live_and_archived_together_are_rejected(tmp_path, capsys):
    with pytest.raises(SystemExit):
        search_cmd.cmd_search(
            ["auth", "--live", "--archived", "--registry", _registry_file(tmp_path, _reg())]
        )
    assert "not allowed with" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_limit_below_one_is_rejected(tmp_path, capsys, bad):
    with pytest.raises(SystemExit):
        search_cmd.cmd_search(
            ["auth", "--limit", bad, "--registry", _registry_file(tmp_path, _reg())]
        )
    assert "must be 1 or greater" in capsys.readouterr().err


def test_json_reports_the_total_even_when_limited(tmp_path, capsys):
    reg = _reg(*[_t(f"s{i}", title=f"auth-{i}") for i in range(5)])
    _, out = _run(tmp_path, capsys, reg, "auth", "--json", "--limit", "2")
    payload = json.loads(out)
    assert payload["total"] == 5
    assert len(payload["matches"]) == 2


def test_json_flags_an_archived_hit(tmp_path, capsys):
    reg = _reg(archived=[_archived("a1", _t("a1", title="auth-spike"))])
    _, out = _run(tmp_path, capsys, reg, "auth", "--json")
    [row] = json.loads(out)["matches"]
    assert row["archived"] is True
    assert row["session_id"] == "a1"


def test_json_flags_a_filtered_hit(tmp_path, capsys):
    reg = _reg(_t("s1", title="auth-probe", filteredBy="[cadet]*"))
    _, out = _run(tmp_path, capsys, reg, "auth", "--json")
    [row] = json.loads(out)["matches"]
    assert row["filtered"] is True


def test_json_reports_blurb_fields_in_snake_case(tmp_path, capsys):
    reg = _reg(_t("s1", title="unrelated", lastPrompt="fix the auth bug"))
    _, out = _run(tmp_path, capsys, reg, "auth", "--json")
    [row] = json.loads(out)["matches"]
    assert row["matched_on"] == "last_prompt"


def test_json_carries_the_full_matched_text_not_a_trimmed_snippet(tmp_path, capsys):
    summary = "x" * 200 + " auth " + "y" * 200
    reg = _reg(_t("s1", title="unrelated", summary=summary))
    _, out = _run(tmp_path, capsys, reg, "auth", "--json")
    [row] = json.loads(out)["matches"]
    assert row["matched_text"] == summary
    assert "…" not in row["matched_text"]
