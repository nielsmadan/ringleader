import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ringleader import paths
from ringleader.analysis import codex, ingest, store
from ringleader.analysis.codex import extract_codex
from ringleader.analysis.episodes import episode_anchor
from ringleader.analysis.facts import MAIN_THREAD, TranscriptRef
from ringleader.sources.base import stat_fingerprint
from ringleader.usage import codex as usage_codex
from ringleader.usage.base import total_of
from ringleader.usage.codex import REPLAY_VERSION, CodexUsageReader

ROOT = "01a00000-0000-7000-8000-00000000000a"
CHILD = "01a00000-0000-7000-8000-00000000000b"
GRANDCHILD = "01a00000-0000-7000-8000-00000000000c"
FORK = "01a00000-0000-7000-8000-00000000000d"
START = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def at(seconds):
    return (START + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def meta(own, *, seconds=0, source="cli", root=None, forked_from=None, ordinal=None, **extra):
    payload = {
        "id": own,
        "session_id": root or own,
        "timestamp": at(seconds),
        "cwd": "/w/a",
        "source": source,
        "forked_from_id": forked_from,
        "git": {"branch": "main"},
        **extra,
    }
    if ordinal is not None:
        payload["subagent_history_start_ordinal"] = ordinal
    return {"timestamp": at(seconds), "type": "session_meta", "payload": payload}


def spawn(parent, depth=1):
    return {
        "subagent": {
            "thread_spawn": {
                "parent_thread_id": parent,
                "depth": depth,
                "agent_path": "/root/reviewer",
                "agent_role": None,
            }
        }
    }


def turn_context(model, seconds, cwd=None):
    payload = {"model": model, **({"cwd": cwd} if cwd else {})}
    return {"timestamp": at(seconds), "type": "turn_context", "payload": payload}


def tokens(seconds, *, inp=1000, cached=400, out=50, cw=0, reasoning=0):
    last = {
        "input_tokens": inp,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cw,
        "output_tokens": out,
        "reasoning_output_tokens": reasoning,
    }
    info = {
        "last_token_usage": last,
        "total_token_usage": last,
        "model_context_window": 258400,
    }
    return {
        "timestamp": at(seconds),
        "type": "event_msg",
        "payload": {"type": "token_count", "info": info},
    }


def event(seconds, **payload):
    return {"timestamp": at(seconds), "type": "event_msg", "payload": payload}


def item(seconds, body, thread=None):
    payload = {"type": "item_completed", "item": body}
    if thread:
        payload["thread_id"] = thread
    return event(seconds, **payload)


def exec_item(call_id, command, *, exit_code=0, status="completed", output="", parsed=None):
    return {
        "type": "CommandExecution",
        "id": call_id,
        "command": ["/bin/zsh", "-lc", command],
        "parsed_cmd": parsed or [{"type": "unknown", "cmd": command}],
        "status": status,
        "aggregated_output": output,
        "exit_code": exit_code,
    }


def response(seconds, **payload):
    return {"timestamp": at(seconds), "type": "response_item", "payload": payload}


def function_call(seconds, call_id, name, arguments):
    return response(
        seconds,
        type="function_call",
        name=name,
        arguments=json.dumps(arguments),
        call_id=call_id,
    )


def function_output(seconds, call_id, output):
    return response(seconds, type="function_call_output", call_id=call_id, output=output)


def script(seconds, call_id, source="text(await tools.exec_command({cmd:'ls'}))"):
    return response(seconds, type="custom_tool_call", name="exec", input=source, call_id=call_id)


def script_output(seconds, call_id, first_line):
    output = [{"type": "input_text", "text": f"{first_line}\nWall time 0.1 seconds\nOutput:\n"}]
    return response(seconds, type="custom_tool_call_output", call_id=call_id, output=output)


def user_item(seconds, text=None, skill=None):
    content = [{"type": "text", "text": text}] if text else []
    if skill:
        content.append({"type": "skill", "name": skill, "path": f"/s/{skill}/SKILL.md"})
    return item(seconds, {"type": "UserMessage", "id": f"u{seconds}", "content": content})


def write(directory, own, records, day="02"):
    folder = directory / "2026" / "10" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-10-{day}T10-00-00-{own}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def extract(path):
    return extract_codex(TranscriptRef(path, "codex", "codex", False, "f"))


def session(tmp_path, *records, own=ROOT):
    return extract(write(tmp_path, own, [meta(own), *records]))


def only_call(facts):
    [call] = facts.tool_calls
    return call


def forked_records(own_id=FORK):
    replay = [
        meta(ROOT, seconds=0),
        {"timestamp": at(0), "type": "compacted", "payload": {"message": "earlier"}},
        turn_context("gpt-parent", 0),
        user_item(0, "parent prompt"),
        item(0, exec_item("p1", "make parent"), thread=ROOT),
        tokens(0, inp=9000, cached=100),
        event(0, type="task_complete", last_agent_message="parent done"),
    ]
    head = meta(own_id, root=ROOT, forked_from=ROOT, ordinal=len(replay) + 1, source=spawn(ROOT))
    own = [
        event(5, type="thread_settings_applied", thread_id=own_id),
        turn_context("gpt-child", 5),
        item(6, exec_item("c1", "make child"), thread=own_id),
        tokens(7, inp=2000, cached=500),
        event(8, type="task_complete", last_agent_message="child done"),
    ]
    return [head, *replay, *own]


def test_main_rollout_identity_and_tracking(tmp_path):
    facts = session(
        tmp_path,
        turn_context("gpt-6", 1, cwd="file:///w/b"),
        tokens(2),
        event(3, type="task_complete", last_agent_message="all done"),
    )
    assert (facts.session_id, facts.thread_id, facts.thread_key) == (ROOT, MAIN_THREAD, ROOT)
    assert (facts.cwd, facts.git_branch) == ("/w/b", "main")
    assert (facts.final_text, facts.last_event) == ("all done", "turn_complete")
    assert facts.first_ts == at(0)


def test_model_attribution_follows_turn_context(tmp_path):
    facts = session(
        tmp_path,
        turn_context("gpt-a", 1),
        tokens(2),
        turn_context("gpt-b", 3),
        tokens(4, inp=1200),
    )
    assert [r.model for r in facts.requests] == ["gpt-a", "gpt-b"]


def test_cached_tokens_are_split_from_input_and_context_includes_them(tmp_path):
    facts = session(tmp_path, tokens(1, inp=1000, cached=400, out=50, reasoning=7))
    [request] = facts.requests
    assert (request.input, request.cache_read, request.output) == (600, 400, 50)
    assert (request.context_tokens, request.reasoning, request.context_window) == (1000, 7, 258400)
    assert request.dedupe_key.endswith("|2")


def test_duplicate_token_count_lines_are_counted_once(tmp_path):
    facts = session(tmp_path, tokens(1), tokens(1), tokens(2, inp=1500))
    assert [r.input for r in facts.requests] == [600, 1100]


def test_gap_and_rebuild_follow_the_claude_rule(tmp_path):
    facts = session(
        tmp_path,
        tokens(1, inp=50_000, cached=49_000),
        tokens(7201, inp=50_000, cached=1_000, cw=48_000),
    )
    second = facts.requests[1]
    assert (second.gap_seconds, second.rebuild) == (7200.0, True)


def test_a_forked_rollout_skips_the_replayed_parent_history(tmp_path):
    facts = extract(write(tmp_path, FORK, forked_records()))
    assert [r.model for r in facts.requests] == ["gpt-child"]
    assert [c.command for c in facts.tool_calls] == ["make child"]
    assert (facts.user_messages, facts.compactions) == ((), ())
    assert facts.final_text == "child done"


def test_extraction_serves_the_cached_start_and_leaves_the_store_alone(tmp_path):
    path = write(tmp_path, FORK, forked_records())
    store = paths.codex_starts_path()
    store.parent.mkdir(parents=True)
    entries = {str(path): [stat_fingerprint(path.stat()), 0]}
    written = json.dumps({"v": REPLAY_VERSION, "entries": entries})
    store.write_text(written, encoding="utf-8")

    facts = extract(path)

    assert ([r.model for r in facts.requests], store.read_text(encoding="utf-8")) == (
        ["gpt-parent", "gpt-child"],
        written,
    )


def test_an_ingest_run_loads_the_start_store_once_for_every_forked_rollout(tmp_path, monkeypatch):
    roots = ingest.Roots(tmp_path / "claude", tmp_path / "codex", tmp_path / "archive")
    for d in (roots.claude, roots.codex, roots.archive):
        d.mkdir()
    forks = [write(roots.codex, own, forked_records(own)) for own in (FORK, GRANDCHILD)]
    store_path = paths.codex_starts_path()
    store_path.parent.mkdir(parents=True)
    entries = {str(f): [stat_fingerprint(f.stat()), 0] for f in forks}
    store_path.write_text(json.dumps({"v": REPLAY_VERSION, "entries": entries}), encoding="utf-8")
    loads = []
    real_load = usage_codex._load_starts
    monkeypatch.setattr(usage_codex, "_load_starts", lambda p: loads.append(p) or real_load(p))

    stats = ingest.prepare_index(
        tmp_path / "index", refresh=True, rebuild=False, roots=roots, now=NOW, notice=print
    )

    with closing(store.connect(Path(stats.path), read_only=True)) as conn:
        models = conn.execute("SELECT model FROM requests ORDER BY path, line").fetchall()
    assert (loads, [m for (m,) in models]) == (
        [store_path],
        ["gpt-parent", "gpt-child", "gpt-parent", "gpt-child"],
    )


def test_extraction_does_not_create_the_start_store(tmp_path):
    extract(write(tmp_path, FORK, forked_records()))

    assert not paths.codex_starts_path().exists()


def test_a_subagent_gets_the_root_session_and_ignores_parent_items(tmp_path):
    records = [
        meta(CHILD, root=ROOT, source=spawn(ROOT)),
        meta(ROOT),
        item(1, exec_item("p1", "make parent"), thread=ROOT),
        item(2, exec_item("c1", "make child"), thread=CHILD),
        user_item(3, "The following is the Codex agent history"),
    ]
    facts = extract(write(tmp_path, CHILD, records))
    assert (facts.session_id, facts.thread_id, facts.thread_key) == (ROOT, CHILD, CHILD)
    assert (facts.parent_thread_id, facts.subagent_description) == (ROOT, "/root/reviewer")
    assert [c.command for c in facts.tool_calls] == ["make child"]
    assert [m.origin for m in facts.user_messages] == ["agent"]


def test_a_subagent_without_a_recorded_root_uses_its_parent(tmp_path):
    records = [meta(GRANDCHILD, root=GRANDCHILD, source=spawn(CHILD, depth=2))]
    facts = extract(write(tmp_path, GRANDCHILD, records))
    assert (facts.session_id, facts.parent_thread_id) == (CHILD, CHILD)


def test_an_execution_in_both_item_and_function_call_counts_once(tmp_path):
    facts = session(
        tmp_path,
        function_call(1, "call_1", "exec_command", {"cmd": "ls"}),
        item(2, exec_item("call_1", "ls")),
        function_output(3, "call_1", "Process exited with code 0\nOutput:\na\n"),
    )
    call = only_call(facts)
    assert (call.call_id, call.line, call.status) == ("call_1", 2, "ok")


def test_the_same_command_in_another_directory_is_a_different_input(tmp_path):
    first, second = (exec_item(f"x{i}", "git status") for i in (1, 2))
    second["cwd"] = "file:///w/other"
    facts = session(
        tmp_path, item(1, first), item(2, second), item(3, exec_item("x3", "git status"))
    )
    hashes = [c.input_hash for c in facts.tool_calls]
    assert (hashes[0] == hashes[2], hashes[0] == hashes[1]) == (True, False)


def test_parsed_cmd_types_classify_the_call(tmp_path):
    reads = [{"type": "read", "cmd": "sed -n 1p a"}]
    lists = [{"type": "list_files", "cmd": "ls"}, {"type": "read", "cmd": "cat b"}]
    facts = session(
        tmp_path,
        item(1, exec_item("a", "sed -n 1p a", parsed=reads)),
        item(2, exec_item("b", "ls; cat b", parsed=lists)),
        item(3, exec_item("c", "uv run pytest -q")),
    )
    assert [c.kind for c in facts.tool_calls] == ["read", "search", "validate"]


def test_a_failing_exec_gets_the_exit_code_category(tmp_path):
    facts = session(tmp_path, item(1, exec_item("a", "make", exit_code="1", status="failed")))
    call = only_call(facts)
    assert (call.status, call.error_category, call.is_error) == ("error", "exit_code", True)
    assert call.error_text.startswith("Exit code 1")
    assert call.signature == "make|exit_code|"


def test_a_grep_exit_1_with_no_output_is_ok(tmp_path):
    facts = session(tmp_path, item(1, exec_item("a", "grep -r foo .", exit_code=1)))
    call = only_call(facts)
    assert (call.status, call.error_category) == ("ok", None)


def test_error_text_is_the_output_tail_from_a_line_boundary(tmp_path):
    output = "".join(f"line {i:04d} of noisy output\n" for i in range(200)) + "fatal: broke"
    facts = session(tmp_path, item(1, exec_item("a", "make", exit_code=2, output=output)))
    text = only_call(facts).error_text
    assert text.splitlines()[1].startswith("line ")
    assert text.endswith("fatal: broke")


def test_lc_unwraps_to_the_script(tmp_path):
    facts = session(tmp_path, item(1, exec_item("a", "cd /w && rg foo")))
    call = only_call(facts)
    assert (call.command, call.command_class, call.tool) == (
        "cd /w && rg foo",
        "rg",
        "exec_command",
    )


def test_a_two_path_file_change_gives_two_writes_anchored_on_seq(tmp_path):
    change = {
        "type": "FileChange",
        "id": "call_p",
        "changes": {"/w/a.py": {"type": "update"}, "/w/b.py": {"type": "add"}},
        "status": "completed",
    }
    facts = session(tmp_path, item(1, change))
    assert [(c.file_path, c.kind, c.call_id) for c in facts.tool_calls] == [
        ("/w/a.py", "write", "call_p"),
        ("/w/b.py", "write", "call_p"),
    ]
    assert episode_anchor(facts.tool_calls, facts.tool_calls[0]) == "seq:0"


def test_a_failed_mcp_call_is_parsed(tmp_path):
    body = {
        "type": "McpToolCall",
        "id": "m1",
        "server": "jina",
        "tool": "read_url",
        "arguments": {"url": "https://x"},
        "status": "failed",
        "result": {"content": [{"type": "text", "text": "MCP error: Input validation error"}]},
    }
    call = only_call(session(tmp_path, item(1, body)))
    assert (call.tool, call.kind, call.mcp_server, call.mcp_tool) == (
        "mcp__jina__read_url",
        "mcp",
        "jina",
        "read_url",
    )
    assert (call.status, call.error_category) == ("error", "input_validation")


def test_a_context_compaction_item_gives_a_compaction(tmp_path):
    facts = session(
        tmp_path,
        tokens(1, inp=180_000),
        item(2, exec_item("a", "ls")),
        item(3, {"type": "ContextCompaction", "id": "item-1"}),
    )
    [compaction] = facts.compactions
    assert (compaction.trigger, compaction.pre_tokens, compaction.before_call) == (
        "auto",
        180_000,
        1,
    )


def test_a_compacted_record_and_its_item_are_one_compaction(tmp_path):
    facts = session(
        tmp_path,
        {"timestamp": at(1), "type": "compacted", "payload": {"message": "summary"}},
        item(2, {"type": "ContextCompaction", "id": "item-1"}),
    )
    assert [c.line for c in facts.compactions] == [2]


def test_turn_aborted_gives_an_interrupt(tmp_path):
    facts = session(
        tmp_path, item(1, exec_item("a", "ls")), event(2, type="turn_aborted", reason="interrupted")
    )
    [message] = facts.user_messages
    assert (message.is_interrupt, message.before_call, message.rule_kind) == (True, 1, "other")
    assert facts.last_event == "turn_aborted"


def test_injected_and_preamble_user_text_is_excluded(tmp_path):
    facts = session(
        tmp_path,
        user_item(1, "# AGENTS.md instructions for /w\n<INSTRUCTIONS>x</INSTRUCTIONS>"),
        user_item(2, "<environment_context>\n<cwd>/w</cwd>\n</environment_context>"),
        user_item(3, "<skill>\nname: x\n</skill>"),
        user_item(4, "fix the build"),
        user_item(5, skill="commit"),
    )
    assert [(m.text, m.origin, m.slash_command) for m in facts.user_messages] == [
        ("fix the build", "human", None),
        ("/commit", "human", "/commit"),
    ]
    assert facts.user_messages[1].rule_kind == "new_task"


def test_a_code_mode_script_with_child_items_is_not_a_call(tmp_path):
    facts = session(
        tmp_path,
        script(1, "s1"),
        item(2, exec_item("exec-1", "ls")),
        script_output(3, "s1", "Script completed"),
    )
    assert [(c.tool, c.line) for c in facts.tool_calls] == [("exec_command", 3)]


def test_a_failed_script_without_child_items_is_an_error_call(tmp_path):
    facts = session(tmp_path, script(1, "s1"), script_output(2, "s1", "Script failed"))
    call = only_call(facts)
    assert (call.tool, call.status) == ("exec", "error")


def test_a_legacy_shell_call_reads_its_exit_code_from_metadata(tmp_path):
    failing = json.dumps({"output": "boom\n", "metadata": {"exit_code": 2}})
    facts = session(
        tmp_path,
        function_call(1, "s1", "shell", {"command": ["bash", "-lc", "make"]}),
        function_output(2, "s1", failing),
        function_call(3, "s2", "shell_command", {"command": "ls docs"}),
        function_output(4, "s2", "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nx\n"),
        function_call(5, "s3", "update_plan", {"plan": []}),
    )
    assert [(c.tool, c.command, c.status, c.error_category) for c in facts.tool_calls] == [
        ("exec_command", "make", "error", "exit_code"),
        ("exec_command", "ls docs", "ok", None),
        ("update_plan", None, "unknown", None),
    ]


def test_a_huge_response_output_is_clipped_on_arrival_and_keeps_its_tail(tmp_path):
    body = "start\n" + "noise\n" * 200_000 + "fatal: the real error\n"
    failing = json.dumps({"output": body, "metadata": {"exit_code": 2}})
    slim = codex._slim_output(failing)
    assert len(slim.body) <= 2 * codex.TAIL_CHARS + 1
    assert slim.code == 2
    facts = session(
        tmp_path,
        function_call(1, "s1", "shell", {"command": ["bash", "-lc", "make"]}),
        function_output(2, "s1", failing),
    )
    call = only_call(facts)
    assert (call.status, call.error_category) == ("error", "exit_code")
    assert call.error_text.endswith("fatal: the real error\n")


def test_a_huge_response_output_is_held_clipped_while_the_rollout_is_scanned(tmp_path):
    body = "start\n" + "noise\n" * 200_000 + "fatal: the real error\n"
    failing = json.dumps({"output": body, "metadata": {"exit_code": 2}})
    path = write(
        tmp_path,
        ROOT,
        [
            meta(ROOT),
            function_call(1, "s1", "shell", {"command": ["bash", "-lc", "make"]}),
            function_output(2, "s1", failing),
        ],
    )
    held = codex._scan(path, 0).calls["s1"].output
    assert len(held.body) <= 2 * codex.TAIL_CHARS + 1


def test_a_legacy_patch_call_writes_each_file(tmp_path):
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n*** Add File: b.py\n*** End Patch"
    output = json.dumps({"output": "Success.", "metadata": {"exit_code": 0}})
    facts = session(
        tmp_path,
        response(1, type="custom_tool_call", name="apply_patch", input=patch, call_id="p"),
        response(2, type="custom_tool_call_output", call_id="p", output=output),
    )
    assert [(c.file_path, c.kind, c.status) for c in facts.tool_calls] == [
        ("a.py", "write", "ok"),
        ("b.py", "write", "ok"),
    ]


@pytest.mark.parametrize(
    "record",
    [
        meta(ROOT),
        turn_context("m", 0),
        tokens(0),
        item(0, exec_item("a", "ls")),
        event(0, type="task_complete"),
        event(0, type="turn_aborted"),
        {"type": "compacted", "payload": {}},
        function_call(0, "c", "shell", {}),
        function_output(0, "c", "x"),
        script(0, "s"),
        script_output(0, "s", "Script failed"),
        response(0, type="message", role="user", content=[]),
    ],
)
def test_the_prefilter_passes_every_handled_record(record):
    for raw in (json.dumps(record), json.dumps(record, separators=(",", ":"))):
        assert any(token in raw for token in codex.PREFILTER)


def _usage_total(sessions_dir):
    return sum(r.total for r in CodexUsageReader(sessions_dir=sessions_dir).records())


def _facts_total(facts):
    return sum(
        total_of(input=r.input, cache_read=r.cache_read, cache_write=r.cache_write, output=r.output)
        for r in facts.requests
    )


def test_request_totals_match_the_usage_reader_including_a_fork(tmp_path):
    paths = [
        write(tmp_path, FORK, forked_records()),
        write(
            tmp_path,
            ROOT,
            [meta(ROOT), turn_context("gpt-6", 1), tokens(2), tokens(2), tokens(3, inp=4000)],
        ),
    ]
    assert sum(_facts_total(extract(p)) for p in paths) == _usage_total(tmp_path)
    assert _usage_total(tmp_path) == 4050 + 1050 + 2050


def test_a_depth_2_spawn_rolls_up_to_the_root_after_refresh(tmp_path):
    roots = ingest.Roots(tmp_path / "claude", tmp_path / "codex", tmp_path / "archive")
    for d in (roots.claude, roots.codex, roots.archive):
        d.mkdir()
    write(roots.codex, ROOT, [meta(ROOT), tokens(1)])
    write(roots.codex, CHILD, [meta(CHILD, root=ROOT, source=spawn(ROOT)), tokens(2)])
    write(roots.codex, GRANDCHILD, [meta(GRANDCHILD, source=spawn(CHILD, depth=2)), tokens(3)])
    stats = ingest.prepare_index(
        tmp_path / "index", refresh=True, rebuild=False, roots=roots, now=NOW, notice=print
    )
    with closing(store.connect(Path(stats.path), read_only=True)) as conn:
        rows = conn.execute("SELECT thread_key, session_id FROM transcripts").fetchall()
    sessions = dict(rows)
    assert sessions == {ROOT: ROOT, CHILD: ROOT, GRANDCHILD: ROOT}


def test_rooted_sessions_follows_parent_chains():
    rows = [("r", ROOT, MAIN_THREAD), ("c", ROOT, CHILD), ("g", CHILD, GRANDCHILD)]
    assert store.rooted_sessions(rows) == {"g": ROOT}


def legacy_exec(start, call_id, command, output):
    return [
        function_call(start, call_id, "shell_command", {"command": command}),
        function_output(start + 1, call_id, output),
    ]


def test_a_legacy_grep_with_no_matches_is_ok_in_both_header_formats(tmp_path):
    facts = session(
        tmp_path,
        *legacy_exec(1, "a", "grep -r foo .", "Exit code: 1\nWall time: 0.1 seconds\nOutput:\n"),
        *legacy_exec(
            3,
            "b",
            "grep -r foo .",
            "Chunk ID: ab12\nWall time: 0.0 seconds\nProcess exited with code 1\n"
            "Original token count: 0\nOutput:\n",
        ),
    )
    assert [(c.status, c.error_category) for c in facts.tool_calls] == [("ok", None)] * 2


def test_a_legacy_failure_signature_ignores_the_harness_header(tmp_path):
    def failing(chunk):
        return (
            f"Chunk ID: {chunk}\nWall time: 0.0 seconds\nProcess exited with code 2\n"
            "Original token count: 5\nOutput:\nmake: *** No rule to make target 'x'. Stop.\n"
        )

    facts = session(
        tmp_path,
        *legacy_exec(1, "a", "make x", failing("ab12")),
        *legacy_exec(3, "b", "make x", failing("cd34")),
    )
    first, second = facts.tool_calls
    assert first.signature == second.signature
    assert first.error_text.endswith("Stop.\n")


@pytest.mark.parametrize("status", ["declined"])
def test_a_declined_exec_is_a_user_rejection_with_text(tmp_path, status):
    facts = session(tmp_path, item(1, exec_item("a", "rm x", exit_code=None, status=status)))
    call = only_call(facts)
    assert (call.status, call.error_category, call.error_text) == ("error", "user_rejected", status)
    assert call.signature == "rm|user_rejected|declined"


def test_a_declined_file_change_is_a_user_rejection(tmp_path):
    change = {"type": "FileChange", "id": "f", "status": "declined", "changes": {"a.py": {}}}
    call = only_call(session(tmp_path, item(1, change)))
    assert (call.status, call.error_category) == ("error", "user_rejected")


def test_a_failed_exec_without_code_or_output_has_no_error_text(tmp_path):
    call = only_call(
        session(tmp_path, item(1, exec_item("a", "make", exit_code=None, status="failed")))
    )
    assert (call.status, call.error_text) == ("error", None)


def response_user(seconds, text):
    return response(
        seconds, type="message", role="user", content=[{"type": "input_text", "text": text}]
    )


def test_user_prompts_come_from_response_messages_when_there_are_no_items(tmp_path):
    facts = session(
        tmp_path,
        response_user(1, "# AGENTS.md instructions for /w\n<INSTRUCTIONS>x</INSTRUCTIONS>"),
        response_user(2, "fix the build"),
        item(3, exec_item("a", "ls")),
    )
    assert [(m.text, m.origin, m.before_call) for m in facts.user_messages] == [
        ("fix the build", "human", 0)
    ]
    assert facts.last_event == "tool_result"


def test_response_messages_are_not_counted_again_next_to_user_items(tmp_path):
    facts = session(tmp_path, response_user(1, "fix the build"), user_item(2, "fix the build"))
    assert [m.line for m in facts.user_messages] == [3]


def test_a_compaction_item_before_its_record_is_one_compaction(tmp_path):
    facts = session(
        tmp_path,
        item(1, {"type": "ContextCompaction", "id": "item-1"}),
        {"timestamp": at(2), "type": "compacted", "payload": {"message": "summary"}},
    )
    assert [c.line for c in facts.compactions] == [2]


def test_a_token_count_between_a_compaction_record_and_item_keeps_one_compaction(tmp_path):
    facts = session(
        tmp_path,
        {"timestamp": at(1), "type": "compacted", "payload": {"message": "summary"}},
        tokens(2),
        item(3, {"type": "ContextCompaction", "id": "item-1"}),
    )
    assert [c.line for c in facts.compactions] == [2]


def test_a_response_message_between_a_compaction_record_and_item_keeps_one_compaction(tmp_path):
    facts = session(
        tmp_path,
        user_item(1, "fix the build"),
        {"timestamp": at(2), "type": "compacted", "payload": {"message": "summary"}},
        response_user(3, "carry on"),
        item(4, {"type": "ContextCompaction", "id": "item-1"}),
    )
    assert [c.line for c in facts.compactions] == [3]


def test_a_response_message_after_user_items_leaves_the_last_event(tmp_path):
    facts = session(
        tmp_path,
        user_item(1, "fix the build"),
        item(2, exec_item("a", "ls")),
        response_user(3, "x"),
    )
    assert facts.last_event == "tool_result"


def test_agent_and_search_items_get_their_kinds(tmp_path):
    facts = session(
        tmp_path,
        item(
            1,
            {
                "type": "CollabAgentToolCall",
                "id": "a",
                "tool": "spawn_agent",
                "status": "completed",
            },
        ),
        item(2, {"type": "WebSearch", "id": "b", "query": "q"}),
        item(3, {"type": "Extension", "id": "c", "kind": "web.search"}),
    )
    assert [(c.kind, c.status) for c in facts.tool_calls] == [
        ("agent", "ok"),
        ("search", "ok"),
        ("search", "ok"),
    ]


def test_a_completed_sub_agent_activity_is_not_a_call(tmp_path):
    facts = session(
        tmp_path,
        item(1, {"type": "SubAgentActivity", "id": "s", "kind": "completed"}),
        item(2, exec_item("a", "ls")),
    )
    assert [c.tool for c in facts.tool_calls] == ["exec_command"]


def test_a_non_interrupt_abort_only_sets_the_last_event(tmp_path):
    facts = session(tmp_path, event(1, type="turn_aborted", reason="replaced"))
    assert (facts.last_event, facts.user_messages) == ("turn_aborted", ())


def test_polling_tools_do_not_form_a_retry_run(tmp_path):
    names = ["wait_agent", "sleep", "write_stdin", "list_agents"]
    records = [function_call(i + 1, f"p{i}-{n}", n, {}) for n in names for i in range(4)]
    facts = session(tmp_path, *records)
    hits = ingest.derive(facts)[0]
    assert [h.kind for h in hits if h.kind == "retry_run"] == []
    assert len(facts.tool_calls) == 16


def refresh(tmp_path, roots):
    stats = ingest.prepare_index(
        tmp_path / "index", refresh=True, rebuild=False, roots=roots, now=NOW, notice=print
    )
    with closing(store.connect(Path(stats.path), read_only=True)) as conn:
        return dict(conn.execute("SELECT thread_key, session_id FROM transcripts").fetchall())


def test_a_depth_2_roll_up_survives_a_second_refresh_and_the_parent_leaving(tmp_path):
    roots = ingest.Roots(tmp_path / "claude", tmp_path / "codex", tmp_path / "archive")
    for d in (roots.claude, roots.codex, roots.archive):
        d.mkdir()
    write(roots.codex, ROOT, [meta(ROOT), tokens(1)])
    child = write(roots.codex, CHILD, [meta(CHILD, root=ROOT, source=spawn(ROOT)), tokens(2)])
    grandchild = [meta(GRANDCHILD, source=spawn(CHILD, depth=2)), tokens(3)]
    path = write(roots.codex, GRANDCHILD, grandchild)
    assert refresh(tmp_path, roots) == {ROOT: ROOT, CHILD: ROOT, GRANDCHILD: ROOT}
    assert refresh(tmp_path, roots) == {ROOT: ROOT, CHILD: ROOT, GRANDCHILD: ROOT}
    child.unlink()
    path.write_text(path.read_text() + json.dumps(tokens(9)) + "\n", encoding="utf-8")
    assert refresh(tmp_path, roots) == {ROOT: ROOT, GRANDCHILD: ROOT}
