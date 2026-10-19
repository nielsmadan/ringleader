import json

from ringleader.usage.claude import ClaudeUsageReader, session_of


def _write(dir_, project, session, records):
    d = dir_ / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _assistant(  # noqa: PLR0913 -- test data-builder helper, one arg per fixture field
    msg_id, req_id, *, inp=100, out=20, cr=500, cw=50, model="claude-opus-5"
):
    return {
        "type": "assistant",
        "requestId": req_id,
        "timestamp": "2026-08-17T10:00:00.000Z",
        "cwd": "/Users/x/wrksp/foo",
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw,
            },
        },
    }


def test_maps_claude_buckets_as_disjoint(tmp_path):
    _write(tmp_path, "-Users-x-wrksp-foo", "sess1", [_assistant("m1", "r1")])

    recs = list(ClaudeUsageReader(projects_dir=tmp_path).records())

    assert len(recs) == 1
    r = recs[0]
    assert (r.input, r.output, r.cache_read, r.cache_write) == (100, 20, 500, 50)
    assert r.reasoning == 0  # Claude folds thinking into output
    assert r.total == 670  # all four buckets are disjoint
    assert r.source == "claude"
    assert r.session_id == "sess1"
    assert r.model == "claude-opus-5"


def test_dedupes_repeated_message_id_and_request_id(tmp_path):
    dup = _assistant("m1", "r1")
    _write(tmp_path, "-Users-x-wrksp-foo", "sess1", [dup, dup, _assistant("m2", "r2")])

    recs = list(ClaudeUsageReader(projects_dir=tmp_path).records())

    assert len(recs) == 2


def test_skips_non_claude_and_usageless_records(tmp_path):
    _write(
        tmp_path,
        "-Users-x-wrksp-foo",
        "sess1",
        [
            _assistant("m1", "r1", model="<synthetic>"),
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"id": "m9", "model": "claude-opus-5"}},
        ],
    )

    assert list(ClaudeUsageReader(projects_dir=tmp_path).records()) == []


def test_decodes_project_label(tmp_path):
    _write(tmp_path, "-Users-x-wrksp-foo", "sess1", [_assistant("m1", "r1")])

    r = next(iter(ClaudeUsageReader(projects_dir=tmp_path).records()))

    assert r.project == "/Users/x/wrksp/foo"  # taken from the record's cwd


def test_available_is_false_without_the_directory(tmp_path):
    assert ClaudeUsageReader(projects_dir=tmp_path / "nope").available() is False


def test_malformed_line_is_skipped(tmp_path):
    d = tmp_path / "-Users-x-wrksp-foo"
    d.mkdir(parents=True)
    (d / "sess1.jsonl").write_text(
        "{not json\n" + json.dumps(_assistant("m1", "r1")) + "\n", encoding="utf-8"
    )

    assert len(list(ClaudeUsageReader(projects_dir=tmp_path).records())) == 1


def test_project_fallback_is_an_absolute_path(tmp_path):
    rec = _assistant("m1", "r1")
    del rec["cwd"]
    _write(tmp_path, "-Users-x-wrksp-foo", "sess1", [rec])

    r = next(iter(ClaudeUsageReader(projects_dir=tmp_path).records()))

    assert r.project == "/Users/x/wrksp/foo"  # matches what the cwd branch yields


def test_record_id_pairs_message_id_with_request_id(tmp_path):
    _write(tmp_path, "-Users-x-wrksp-foo", "sess1", [_assistant("m1", "r1")])

    r = next(iter(ClaudeUsageReader(projects_dir=tmp_path).records()))

    assert r.record_id == "m1|r1"


def _write_at(dir_, relpath, records):
    p = dir_ / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def test_session_of_names_the_parent_of_the_nearest_subagents_dir(tmp_path):
    root = tmp_path / "-p"
    assert session_of(root / "sid.jsonl", root) == "sid"
    assert session_of(root / "sid" / "subagents" / "agent-a1.jsonl", root) == "sid"
    assert (
        session_of(root / "sid" / "subagents" / "workflows" / "wf_1" / "agent-b.jsonl", root)
        == "sid"
    )


def test_session_of_ignores_a_subagents_dir_above_the_projects_root(tmp_path):
    root = tmp_path / "subagents" / "x" / "projects"
    assert session_of(root / "-p" / "sid.jsonl", root) == "sid"


def test_subagent_and_workflow_subagent_records_are_credited_to_the_parent(tmp_path):
    _write(tmp_path, "-Users-x-wrksp-foo", "sid", [_assistant("m1", "r1")])
    _write_at(tmp_path, "-Users-x-wrksp-foo/sid/subagents/agent-a1.jsonl", [_assistant("m2", "r2")])
    _write_at(
        tmp_path,
        "-Users-x-wrksp-foo/sid/subagents/workflows/wf_1/agent-b2.jsonl",
        [_assistant("m3", "r3", inp=7)],
    )

    recs = list(ClaudeUsageReader(projects_dir=tmp_path).records())

    assert sorted((r.session_id, r.record_id) for r in recs) == [
        ("sid", "m1|r1"),
        ("sid", "m2|r2"),
        ("sid", "m3|r3"),
    ]


def test_subagent_project_falls_back_to_the_project_dir_name(tmp_path):
    rec = _assistant("m2", "r2")
    del rec["cwd"]
    _write_at(tmp_path, "-Users-x-wrksp-foo/sid/subagents/agent-a1.jsonl", [rec])

    r = next(iter(ClaudeUsageReader(projects_dir=tmp_path).records()))

    assert r.project == "/Users/x/wrksp/foo"


def test_request_duplicated_across_main_and_fork_counts_once_in_str_order(tmp_path):
    dup = _assistant("m1", "r1")
    _write(tmp_path, "-Users-x-wrksp-foo", "sid", [dup])
    _write_at(tmp_path, "-Users-x-wrksp-foo/sid/subagents/agent-a1.jsonl", [dup])
    _write(tmp_path, "-Users-x-wrksp-foo", "aaa-fork", [dup])

    recs = list(ClaudeUsageReader(projects_dir=tmp_path).records())

    assert [r.session_id for r in recs] == ["aaa-fork"]


def test_session_prefix_includes_the_sessions_subagent_records(tmp_path):
    _write(tmp_path, "-Users-x-wrksp-foo", "sid", [_assistant("m1", "r1")])
    _write_at(tmp_path, "-Users-x-wrksp-foo/sid/subagents/agent-a1.jsonl", [_assistant("m2", "r2")])
    _write(tmp_path, "-Users-x-wrksp-foo", "other", [_assistant("m3", "r3")])

    recs = list(ClaudeUsageReader(projects_dir=tmp_path).records(session_prefix="sid"))

    assert sorted(r.record_id for r in recs) == ["m1|r1", "m2|r2"]


def test_latest_day_sees_a_subagent_only_newest_day(tmp_path):
    _write(tmp_path, "-Users-x-wrksp-foo", "sid", [_assistant("m1", "r1")])
    late = _assistant("m2", "r2")
    late["timestamp"] = "2026-08-19T10:00:00.000Z"
    _write_at(tmp_path, "-Users-x-wrksp-foo/sid/subagents/agent-a1.jsonl", [late])

    assert ClaudeUsageReader(projects_dir=tmp_path).latest_day() == "2026-08-19"


def test_one_hour_cache_writes_are_a_subset_of_cache_write(tmp_path):
    rec = _assistant("m1", "r1", cw=50)
    rec["message"]["usage"]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 20,
        "ephemeral_1h_input_tokens": 30,
    }
    _write(tmp_path, "-Users-x-wrksp-foo", "sid", [rec])

    r = next(iter(ClaudeUsageReader(projects_dir=tmp_path).records()))

    assert (r.cache_write, r.cache_write_1h) == (50, 30)


def test_session_report_grows_over_the_parent_turns_and_totals_include_the_subagent(tmp_path):
    from ringleader.usage.session import analyze

    parent = [
        {**_assistant("m1", "r1", inp=1000, cr=0), "timestamp": "2026-08-17T10:00:00Z"},
        {**_assistant("m2", "r2", inp=2000, cr=0), "timestamp": "2026-08-17T10:02:00Z"},
    ]
    child = [{**_assistant("m3", "r3", inp=10, cr=0), "timestamp": "2026-08-17T10:01:00Z"}]
    _write(tmp_path, "-Users-x-wrksp-foo", "sid", parent)
    _write_at(tmp_path, "-Users-x-wrksp-foo/sid/subagents/agent-a1.jsonl", child)

    recs = list(ClaudeUsageReader(projects_dir=tmp_path).records())
    rep = analyze(recs)

    assert [r.nested for r in sorted(recs, key=lambda r: r.timestamp)] == [False, True, False]
    assert rep.context_growth == [1000, 2000]
    assert [t for _, t in rep.top_turns] == [2070, 1070]
    assert rep.turns == 3
    assert rep.totals.input == 3010
