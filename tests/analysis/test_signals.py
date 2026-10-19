from dataclasses import replace

from ringleader.analysis.signals import detect, fingerprint_material, inferred_compactions

from .factories import (
    bash,
    calls,
    compaction,
    edit,
    facts,
    fail,
    grep,
    message,
    read,
    request,
    rewind,
)
from .test_claude_extract import _main, assistant, chain, result, text, tool_use, user


def spans(transcript, kind):
    return [(h.start_seq, h.end_seq) for h in detect(transcript) if h.kind == kind]


def mixed(size, errors):
    return calls(*(fail(f"c{i}") if i in errors else bash(f"c{i}") for i in range(size)))


def fillers(count, start=0):
    return [bash(f"f{start + i}") for i in range(count)]


def test_tool_failure_is_one_hit_per_error_call():
    transcript = facts(calls(bash("a"), fail("b"), bash("c"), fail("d")))
    assert spans(transcript, "tool_failure") == [(1, 1), (3, 3)]


def test_retry_run_fires_at_three_identical_calls():
    same = bash("make", input_hash="same")
    assert spans(facts(calls(bash("ls"), same, same, same)), "retry_run") == [(1, 3)]
    assert spans(facts(calls(bash("ls"), same, same)), "retry_run") == []


def test_retry_run_skips_codex_polling_tools():
    wait = {"tool": "wait_agent", "input_hash": "same"}
    make = bash("make", input_hash="same")
    found = spans(facts(calls(wait, wait, wait, make, make, make)), "retry_run")
    assert found == [(3, 5)]


def test_runaway_window_fires_at_six_errors_in_twelve_calls():
    assert spans(facts(mixed(12, {0, 2, 4, 6, 8, 11})), "runaway_window") == [(0, 11)]
    assert spans(facts(mixed(13, {0, 2, 4, 6, 8, 12})), "runaway_window") == []
    assert spans(facts(mixed(12, {0, 2, 4, 6, 8})), "runaway_window") == []


def test_runaway_window_merges_overlapping_windows():
    transcript = facts(mixed(20, {0, 2, 4, 6, 8, 10, 12, 14, 16, 18}))
    assert spans(transcript, "runaway_window") == [(0, 18)]


def test_runaway_command_class_branch_fires():
    def pytest_run(errors, size):
        return [fail("pytest -q") if i in errors else bash("pytest -q") for i in range(size)]

    fires = facts(calls(*pytest_run({1, 5, 9}, 10), bash("ls"), bash("pwd")))
    assert spans(fires, "runaway_window") == [(1, 9)]
    two_errors = facts(calls(*pytest_run({1, 5}, 10), bash("ls"), bash("pwd")))
    assert spans(two_errors, "runaway_window") == []
    nine_calls = facts(calls(*pytest_run({1, 5, 8}, 9), bash("ls"), bash("pwd"), bash("w")))
    assert spans(nine_calls, "runaway_window") == []


def test_edit_churn_fires_with_an_error_between_edits():
    transcript = facts(calls(edit("a.py"), fail(), edit("a.py"), edit("a.py")))
    assert spans(transcript, "edit_churn") == [(0, 3)]
    [hit] = [h for h in detect(transcript) if h.kind == "edit_churn"]
    assert hit.detail == "3 edits to a.py"


def test_edit_churn_needs_three_edits_within_ten_calls():
    within = facts(
        calls(edit("a.py"), fail(), *fillers(3), edit("a.py"), *fillers(3, 3), edit("a.py"))
    )
    assert spans(within, "edit_churn") == [(0, 9)]
    beyond = facts(
        calls(edit("a.py"), fail(), *fillers(3), edit("a.py"), *fillers(4, 3), edit("a.py"))
    )
    assert spans(beyond, "edit_churn") == []
    two = facts(calls(edit("a.py"), fail(), edit("a.py")))
    assert spans(two, "edit_churn") == []


def test_edit_churn_split_across_two_files_is_silent():
    transcript = facts(calls(edit("a.py"), fail(), edit("b.py"), edit("a.py")))
    assert spans(transcript, "edit_churn") == []


def test_edit_pass_edit_pass_edit_is_silent():
    passing = bash("pytest -q")
    transcript = facts(calls(edit("a.py"), passing, edit("a.py"), passing, edit("a.py")))
    assert spans(transcript, "edit_churn") == []


def test_edit_churn_fires_on_an_old_new_swap_revert():
    transcript = facts(
        calls(
            edit("a.py", old="x", new="y"),
            edit("a.py", old="p", new="q"),
            edit("a.py", old="y", new="x"),
        )
    )
    assert spans(transcript, "edit_churn") == [(0, 2)]


def cancelled(count):
    return [fail(f"s{i}", category="sibling_cancelled") for i in range(count)]


def test_cancelled_siblings_are_not_agent_failures():
    transcript = facts(calls(fail("make"), *cancelled(6), bash("ls")))
    assert spans(transcript, "tool_failure") == [(0, 0)]
    assert {h.kind for h in detect(transcript)} == {"tool_failure"}


def test_real_failures_around_cancelled_siblings_still_streak():
    transcript = facts(calls(fail("a"), *cancelled(2), fail("b"), fail("c"), fail("d")))
    assert spans(transcript, "failure_streak") == [(3, 5)]
    assert spans(transcript, "tool_failure") == [(0, 0), (3, 3), (4, 4), (5, 5)]


def test_failure_streak_is_one_hit_per_maximal_streak():
    transcript = facts(calls(fail("a"), fail("b"), fail("c"), fail("d"), bash("ok")))
    assert spans(transcript, "failure_streak") == [(0, 3)]
    assert spans(facts(calls(fail("a"), fail("b"), bash("ok"))), "failure_streak") == []


def rereads(*gaps):
    return [call for i, gap in enumerate(gaps) for call in (*fillers(gap, 10 * i), read("a.py"))]


def test_reread_churn_fires_at_five_reads_within_ten_calls():
    within = facts(calls(*rereads(0, 1, 1, 1, 2)))
    assert spans(within, "reread_churn") == [(0, 9)]
    beyond = facts(calls(*rereads(0, 1, 1, 1, 3)))
    assert spans(beyond, "reread_churn") == []
    four = facts(calls(*rereads(0, 1, 1, 1)))
    assert spans(four, "reread_churn") == []


def pages(*ranges):
    return [
        read("a.py", input_hash=f"r{i}", read_offset=offset, read_limit=limit)
        for i, (offset, limit) in enumerate(ranges)
    ]


def test_paging_through_a_file_is_not_reread_churn():
    paging = facts(calls(*pages((1, 100), (101, 100), (201, 100), (301, 100), (401, 100))))
    assert spans(paging, "reread_churn") == []


def test_five_overlapping_reads_are_reread_churn():
    overlapping = facts(calls(*pages((0, 100), (50, 100), (20, 100), (60, 100), (40, 100))))
    assert spans(overlapping, "reread_churn") == [(0, 4)]
    four_and_a_page = facts(calls(*pages((0, 100), (50, 100), (20, 100), (60, 100), (300, 50))))
    assert spans(four_and_a_page, "reread_churn") == []


def test_paging_a_pdf_without_offsets_is_not_reread_churn():
    pdf_pages = [read("doc.pdf", input_hash=f"p{i}") for i in range(5)]
    assert spans(facts(calls(*pdf_pages)), "reread_churn") == []
    same = read("doc.pdf", input_hash="p0")
    assert spans(facts(calls(same, same, same, same, same)), "reread_churn") == [(0, 4)]


def test_identical_reads_overlap_whatever_their_range():
    same = read("a.py", input_hash="same", read_offset=900, read_limit=1)
    assert spans(facts(calls(same, same, same, same, same)), "reread_churn") == [(0, 4)]


def test_a_whole_file_read_overlaps_a_page_inside_its_default_limit():
    transcript = facts(calls(read("a.py"), *pages((10, 20), (12, 20), (14, 20), (16, 20))))
    assert spans(transcript, "reread_churn") == [(0, 4)]
    beyond = facts(calls(read("a.py"), *pages((10, 20), (12, 20), (14, 20), (2000, 20))))
    assert spans(beyond, "reread_churn") == []


def test_a_write_between_rereads_resets_reread_churn():
    transcript = facts(calls(read("a.py"), read("a.py"), edit("a.py"), read("a.py")))
    assert spans(transcript, "reread_churn") == []


def looks(count, start=0):
    return [read(f"f{start + i}.py") if i % 2 else grep(f"g{start + i}") for i in range(count)]


def test_search_loop_fires_at_twenty_consecutive_reads_and_searches():
    assert spans(facts(calls(edit("a.py"), *looks(20))), "search_loop") == [(1, 20)]
    assert spans(facts(calls(edit("a.py"), *looks(19))), "search_loop") == []


def test_a_validate_call_breaks_a_search_loop():
    transcript = facts(calls(edit("a.py"), *looks(10), bash("pytest -q"), *looks(10, 10)))
    assert spans(transcript, "search_loop") == []


def test_a_human_message_resets_a_search_loop():
    split = facts(calls(edit("a.py"), *looks(10), *looks(10, 10)), user_messages=(message(11),))
    assert spans(split, "search_loop") == []
    after = facts(calls(edit("a.py"), *looks(10), *looks(20, 10)), user_messages=(message(11),))
    assert spans(after, "search_loop") == [(11, 30)]


def test_an_agent_message_does_not_reset_a_search_loop():
    transcript = facts(
        calls(edit("a.py"), *looks(10), *looks(10, 10)),
        user_messages=(message(11, origin="task-notification"),),
    )
    assert spans(transcript, "search_loop") == [(1, 20)]


def test_the_run_before_the_first_write_is_not_a_search_loop():
    before = facts(calls(*looks(25), edit("a.py"), *looks(19, 30)))
    assert spans(before, "search_loop") == []
    after = facts(calls(*looks(25), edit("a.py"), *looks(20, 30)))
    assert spans(after, "search_loop") == [(26, 45)]


def test_a_heredoc_file_write_through_bash_breaks_a_search_loop():
    heredoc = bash("cat > notes.py <<'EOF'\nx = 1\nEOF")
    transcript = facts(calls(edit("a.py"), *looks(10), heredoc, *looks(10, 10)))
    assert spans(transcript, "search_loop") == []


def test_the_first_write_may_be_a_bash_redirect():
    transcript = facts(calls(*looks(5), bash("echo x > out.txt"), *looks(20, 10)))
    assert spans(transcript, "search_loop") == [(6, 25)]


def test_zero_write_subagent_thread_has_no_search_loop():
    transcript = facts(
        calls(*looks(25)), thread_id="agent-a1", thread_key="s/agent-a1", subagent_type="Explore"
    )
    assert spans(transcript, "search_loop") == []


def test_unsupported_completion_spans_last_write_to_end():
    transcript = facts(
        calls(bash("pytest -q"), edit("src/a.py"), bash("ls")), final_text="All done."
    )
    assert spans(transcript, "unsupported_completion") == [(1, 2)]


def test_later_passing_pytest_supports_the_completion():
    transcript = facts(calls(edit("src/a.py"), bash("pytest -q")), final_text="Fixed it.")
    assert spans(transcript, "unsupported_completion") == []


def test_docs_only_writes_after_validation_do_not_fire():
    transcript = facts(
        calls(edit("src/a.py"), bash("pytest -q"), edit("docs/notes.py"), edit("README.md")),
        final_text="Implemented.",
    )
    assert spans(transcript, "unsupported_completion") == []


def test_negated_claim_does_not_fire():
    transcript = facts(calls(edit("src/a.py")), final_text="This is not done yet.")
    assert spans(transcript, "unsupported_completion") == []


def test_point_detectors_for_interruptions_rejections_and_denials():
    transcript = facts(
        calls(
            bash("ls"),
            fail("rm x", category="user_rejected"),
            fail("curl y", category="permission_denied"),
        ),
        user_messages=(
            message(2, "[Request interrupted by user]", is_interrupt=True),
            message(3, "[Request interrupted by user]", origin="agent", is_interrupt=True),
        ),
    )
    assert spans(transcript, "user_interruption") == [(2, 2)]
    assert spans(transcript, "rejected_tool_use") == [(1, 1)]
    assert spans(transcript, "permission_denial") == [(2, 2)]


def test_recorded_compaction_is_a_hit():
    transcript = facts(calls(*fillers(4)), compactions=(compaction(2),))
    assert spans(transcript, "compaction") == [(2, 2)]


def test_forty_percent_context_drop_infers_a_compaction():
    tool_calls = calls(*fillers(5))
    [inferred] = inferred_compactions((request(3, 1000), request(7, 600)), tool_calls)
    assert (inferred.trigger, inferred.before_call) == ("inferred", 3)
    assert (inferred.pre_tokens, inferred.post_tokens) == (1000, 600)
    transcript = facts(tool_calls, requests=(request(3, 1000), request(7, 600)))
    assert spans(transcript, "compaction") == [(3, 3)]
    assert inferred_compactions((request(3, 1000), request(7, 700)), tool_calls) == ()


def test_mid_task_compaction_fires_on_a_shared_file():
    tool_calls = calls(edit("a.py"), read("b.py"), bash("ls"), read("a.py"), bash("pwd"))
    transcript = facts(tool_calls, compactions=(compaction(3),))
    assert spans(transcript, "mid_task_compaction") == [(0, 4)]


def test_mid_task_compaction_is_silent_with_a_human_message_between():
    tool_calls = calls(edit("a.py"), read("b.py"), bash("ls"), read("a.py"), bash("pwd"))
    transcript = facts(tool_calls, compactions=(compaction(3),), user_messages=(message(3),))
    assert spans(transcript, "mid_task_compaction") == []
    unshared = calls(edit("a.py"), bash("ls"), read("c.py"))
    assert spans(facts(unshared, compactions=(compaction(2),)), "mid_task_compaction") == []


def test_rewind_fact_is_a_hit():
    transcript = facts(calls(*fillers(3)), rewinds=(rewind(2, 4),))
    [hit] = [h for h in detect(transcript) if h.kind == "rewind"]
    assert (hit.start_seq, hit.end_seq, hit.detail) == (2, 2, "user rewound, abandoning 4 calls")


def test_abandoned_branch_calls_produce_no_signals(tmp_path):
    first = chain(user("start"), assistant(text("ok"), mid="m1", rid="r1"))
    root = first[-1]["uuid"]
    abandoned = chain(
        {**user("try A"), "parentUuid": root},
        assistant(
            tool_use("a1", command="make"),
            tool_use("a2", command="make"),
            tool_use("a3", command="make"),
            mid="m2",
            rid="r2",
        ),
        result("a1", "Exit code 2\nmake: *** boom", is_error=True),
        result("a2", "Exit code 2\nmake: *** boom", is_error=True),
        result("a3", "Exit code 2\nmake: *** boom", is_error=True),
    )
    live = chain(
        {**user("try B"), "parentUuid": root},
        assistant(tool_use("b1", command="ls"), mid="m3", rid="r3"),
        result("b1", "ok"),
    )
    transcript = _main(tmp_path, [*first, *abandoned, *live])
    assert [c.status for c in transcript.tool_calls if not c.on_main_branch] == ["error"] * 3
    assert [(h.kind, h.start_seq) for h in detect(transcript)] == [("rewind", 3)]


def test_fingerprint_material_names_thresholds():
    assert '"runaway_window": 12' in fingerprint_material()


def hit_keys(transcript):
    return [(h.kind, h.start_seq, h.detail) for h in detect(transcript)]


def test_point_hits_sharing_an_anchor_collapse_into_one_hit_with_a_count():
    transcript = facts(
        calls(*fillers(3)),
        user_messages=(
            message(1, is_interrupt=True),
            replace(message(1, is_interrupt=True), line=99, message_id="u1b", dedupe_key="u1b"),
        ),
        compactions=(compaction(2), compaction(2)),
        rewinds=(rewind(3, 1), rewind(3, 2)),
    )
    keys = [(h.kind, h.start_seq) for h in detect(transcript)]
    assert len(keys) == len(set(keys))
    assert ("user_interruption", 1, "user interrupted (x2)") in hit_keys(transcript)
    assert [h.detail for h in detect(transcript) if h.kind == "rewind"] == [
        "user rewound, abandoning 1 calls (x2)"
    ]


def interrupted_trio():
    interrupted = {"error_category": "interruption", "error_text": "interrupted"}
    return calls(*(bash(f"c{i}", "error", **interrupted) for i in range(3)))


def test_interrupted_calls_count_only_as_a_user_interruption():
    transcript = facts(interrupted_trio(), user_messages=(message(1, is_interrupt=True),))
    assert {h.kind for h in detect(transcript)} == {"user_interruption"}


def test_rejected_calls_count_only_as_rejected_tool_use():
    rejected = {"error_category": "user_rejected", "error_text": "no"}
    transcript = facts(calls(*(bash(f"c{i}", "error", **rejected) for i in range(3))))
    assert {h.kind for h in detect(transcript)} == {"rejected_tool_use"}


def test_a_shell_call_inside_a_search_loop_does_not_break_it():
    transcript = facts(calls(edit("a.py"), *looks(10), bash("git log"), *looks(10, 10)))
    assert spans(transcript, "search_loop") == [(1, 21)]


def test_mid_task_before_window_is_ten_calls():
    tail = fillers(5, 20)
    inside = facts(
        calls(edit("a.py"), *fillers(9), *tail[:4], read("a.py")), compactions=(compaction(10),)
    )
    assert spans(inside, "mid_task_compaction") == [(0, 14)]
    outside = facts(
        calls(edit("a.py"), *fillers(10), *tail[:4], read("a.py")), compactions=(compaction(11),)
    )
    assert spans(outside, "mid_task_compaction") == []


def test_mid_task_after_window_is_five_calls():
    head = [edit("a.py"), *fillers(9)]
    inside = facts(calls(*head, *fillers(4, 20), read("a.py")), compactions=(compaction(10),))
    assert spans(inside, "mid_task_compaction") == [(0, 14)]
    outside = facts(calls(*head, *fillers(5, 20), read("a.py")), compactions=(compaction(10),))
    assert spans(outside, "mid_task_compaction") == []
