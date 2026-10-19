import json
from itertools import count

from ringleader.analysis.claude import extract_claude
from ringleader.analysis.facts import MAIN_THREAD, TranscriptRef

SID = "11111111-2222-3333-4444-555555555555"
_ids = count(1)


def _uid():
    return f"u{next(_ids)}"


def _ref(path):
    return TranscriptRef(
        path=path, agent="claude-code", root="claude", archived=False, fingerprint="f"
    )


def _write(path, records, tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + tail, encoding="utf-8")
    return path


def _main(tmp_path, records, tail=""):
    return extract_claude(_ref(_write(tmp_path / "-proj" / f"{SID}.jsonl", records, tail)))


def chain(*records):
    out = []
    parent = None
    for record in records:
        rec = {"uuid": _uid(), "timestamp": "2026-10-01T10:00:00Z", **record}
        rec.setdefault("parentUuid", parent)
        parent = rec["uuid"]
        out.append(rec)
    return out


def user(text, **extra):
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def usage(inp=10, cr=0, cw=0, out=5, m5=0, h1=0):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw,
        "cache_creation": {"ephemeral_5m_input_tokens": m5, "ephemeral_1h_input_tokens": h1},
    }


def assistant(*blocks, mid="m1", rid="r1", use=None, **extra):
    return {
        "type": "assistant",
        "requestId": rid,
        "message": {
            "id": mid,
            "model": "claude-opus-5",
            "content": list(blocks),
            "usage": use or usage(),
        },
        **extra,
    }


def tool_use(call_id, name="Bash", **inp):
    return {"type": "tool_use", "id": call_id, "name": name, "input": inp}


def text(value):
    return {"type": "text", "text": value}


def result(call_id, content="ok", is_error=None, **extra):
    block = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return {"type": "user", "message": {"role": "user", "content": [block]}, **extra}


def _call(facts, call_id):
    return next(c for c in facts.tool_calls if c.call_id == call_id)


def test_main_transcript_identity_and_tracking(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("hi", cwd="/w/a", gitBranch="main"),
            assistant(text("done here"), cwd="/w/b", gitBranch="feat"),
            {"type": "system", "subtype": "turn_duration"},
        ),
    )
    assert (facts.session_id, facts.thread_id, facts.thread_key) == (SID, MAIN_THREAD, SID)
    assert (facts.cwd, facts.git_branch) == ("/w/b", "feat")
    assert facts.final_text == "done here"
    assert facts.last_event == "turn_complete"
    assert facts.first_ts == "2026-10-01T10:00:00Z"


def test_a_one_hour_write_above_the_total_is_clamped_like_rl_usage(tmp_path):
    facts = _main(tmp_path, chain(user("go"), assistant(text("hi"), use=usage(cw=50, h1=80))))
    [request] = facts.requests
    assert (request.cache_write, request.cache_write_1h) == (50, 50)


def test_split_assistant_blocks_give_one_request(tmp_path):
    use = usage(inp=3, cr=1000, cw=200, m5=50, h1=150)
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(text("thinking"), use=use),
            assistant(tool_use("t1", command="ls"), use=use),
        ),
    )
    [request] = facts.requests
    assert (request.cache_write_5m, request.cache_write_1h) == (50, 150)
    assert request.context_tokens == 1203
    assert request.dedupe_key == "m1|r1"
    assert request.gap_seconds is None
    assert request.rebuild is False


def test_gap_and_rebuild_after_cache_collapse(tmp_path):
    records = chain(
        user("go"),
        assistant(text("a"), mid="m1", rid="r1", use=usage(inp=5, cr=50_000, cw=1_000)),
        user("again"),
        assistant(text("b"), mid="m2", rid="r2", use=usage(inp=5, cr=2_000, cw=49_000)),
    )
    records[3]["timestamp"] = "2026-10-01T12:00:00Z"
    facts = _main(tmp_path, records)
    second = facts.requests[1]
    assert second.gap_seconds == 7200.0
    assert second.rebuild is True


def test_attribution_skill_and_plugin_land_on_requests(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("go"), assistant(text("x"), attributionSkill="commit", attributionPlugin="tools")
        ),
    )
    assert (facts.requests[0].skill, facts.requests[0].plugin) == ("commit", "tools")


def test_call_status_follows_is_error(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(
                tool_use("t1", command="false"),
                tool_use("t2", command="true"),
                tool_use("t3", name="Read", file_path="/a.py"),
                tool_use("t4", name="Edit", file_path="/a.py", old_string="a", new_string="b"),
                tool_use("t5", command="sleep 9"),
            ),
            result("t1", "Exit code 2\nboom", is_error=True),
            result("t2", "fine", is_error=False),
            result("t3", "contents"),
            result("t4", "<tool_use_error>File has not been read yet.</tool_use_error>"),
        ),
    )
    statuses = {c.call_id: (c.is_error, c.status, c.error_category) for c in facts.tool_calls}
    assert statuses == {
        "t1": (True, "error", "exit_code"),
        "t2": (False, "ok", None),
        "t3": (None, "ok", None),
        "t4": (None, "error", "file_not_read"),
        "t5": (None, "unknown", None),
    }
    assert _call(facts, "t1").signature == "false|exit_code|boom"
    assert _call(facts, "t4").edit_old_hash is not None
    assert [c.seq for c in facts.tool_calls] == [0, 1, 2, 3, 4]
    assert facts.last_event == "tool_result"


def test_grep_exit_one_without_output_is_ok(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(tool_use("t1", command="cd /x && grep needle file")),
            result("t1", "Exit code 1", is_error=True),
        ),
    )
    call = _call(facts, "t1")
    assert (call.status, call.error_category, call.kind) == ("ok", None, "search")


def test_string_tool_use_result_is_the_error_text(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(tool_use("t1", name="Edit", file_path="/a.py")),
            result(
                "t1",
                "<tool_use_error>String to replace not found in file.</tool_use_error>",
                is_error=True,
                toolUseResult="Error: String to replace not found in file.",
            ),
        ),
    )
    call = _call(facts, "t1")
    assert call.error_text == "Error: String to replace not found in file."
    assert call.error_category == "string_not_found"


def test_rejection_marker_is_user_rejected(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(tool_use("t1", command="rm -rf build")),
            result(
                "t1",
                "The user doesn't want to proceed with this tool use. The tool use was rejected.",
                is_error=False,
            ),
        ),
    )
    assert (_call(facts, "t1").status, _call(facts, "t1").error_category) == (
        "error",
        "user_rejected",
    )


def test_permission_decision_and_interrupted_result(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(tool_use("t1", command="curl x"), tool_use("t2", command="sleep 100")),
            result(
                "t1",
                "Permission for this action was denied",
                is_error=True,
                permissionDecision={"decision": "reject", "source": "config"},
            ),
            result("t2", "partial", is_error=False, toolUseResult={"interrupted": True}),
        ),
    )
    assert _call(facts, "t1").error_category == "permission_denied"
    assert (_call(facts, "t2").status, _call(facts, "t2").error_category) == (
        "error",
        "interruption",
    )


def test_caps_hold_at_extraction(tmp_path):
    long_error = "h" * 600 + "m" * 4000 + "t" * 400
    facts = _main(
        tmp_path,
        chain(
            user("x" * 5000),
            assistant(tool_use("t1", command="echo " + "a" * 5000), text("z" * 5000)),
            result("t1", long_error, is_error=True),
        ),
    )
    call = _call(facts, "t1")
    assert len(call.command) == 500
    assert call.error_text == "h" * 600 + "\n…\n" + "t" * 400
    assert len(facts.user_messages[0].text) == 2000
    assert facts.final_text == "z" * 2000


def test_skill_and_mcp_calls_are_parsed(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(
                tool_use("t1", name="Skill", skill="commit"),
                tool_use("t2", name="mcp__jina__read_url", url="u"),
            ),
        ),
    )
    skill, mcp = facts.tool_calls
    assert (skill.kind, skill.skill) == ("skill", "commit")
    assert (mcp.kind, mcp.mcp_server, mcp.mcp_tool) == ("mcp", "jina", "read_url")
    assert mcp.input_preview == '{"url": "u"}'


def test_rewound_branch_gives_one_rewind(tmp_path):
    first = chain(user("start"), assistant(text("ok"), mid="m1", rid="r1"))
    root = first[-1]["uuid"]
    abandoned = chain(
        {**user("try A"), "parentUuid": root},
        assistant(
            tool_use("a1", command="make"), tool_use("a2", command="make"), mid="m2", rid="r2"
        ),
        result("a1", "x"),
    )
    live = chain(
        {**user("try B"), "parentUuid": root},
        assistant(tool_use("b1", command="ls"), mid="m3", rid="r3"),
        result("b1", "ok"),
    )
    facts = _main(tmp_path, [*first, *abandoned, *live])
    [rewind] = facts.rewinds
    assert rewind.abandoned_calls == 2
    assert rewind.branch_uuid == root
    assert rewind.line == 6
    assert rewind.before_call == 2
    branches = {c.call_id: c.on_main_branch for c in facts.tool_calls}
    assert branches == {"a1": False, "a2": False, "b1": True}
    assert [m.on_main_branch for m in facts.user_messages] == [True, False, True]
    assert [r.on_main_branch for r in facts.requests] == [True, False, True]


def test_sibling_tool_result_for_a_live_call_is_not_abandoned(tmp_path):
    head = chain(user("go"), assistant(tool_use("t1", command="ls"), mid="m1", rid="r1"))
    first_call = head[-1]["uuid"]
    rest = chain(
        {**assistant(tool_use("t2", command="pwd"), mid="m1", rid="r1"), "parentUuid": first_call},
        result("t2", "/w"),
        assistant(text("both done"), mid="m2", rid="r2"),
    )
    sibling = {**result("t1", "files"), "uuid": _uid(), "parentUuid": first_call}
    facts = _main(tmp_path, [*head, rest[0], sibling, *rest[1:]])
    assert facts.rewinds == ()
    assert [(c.call_id, c.on_main_branch, c.status) for c in facts.tool_calls] == [
        ("t1", True, "ok"),
        ("t2", True, "ok"),
    ]


def test_malformed_chain_falls_back_to_all_live(tmp_path):
    records = chain(user("a"), assistant(tool_use("t1", command="ls")), user("b"))
    records[2]["parentUuid"] = "missing-parent"
    facts = _main(tmp_path, records)
    assert facts.rewinds == ()
    assert [m.on_main_branch for m in facts.user_messages] == [True, True]
    assert facts.tool_calls[0].on_main_branch is True


def test_compaction_reads_pre_and_post_tokens(tmp_path):
    records = chain(user("a"), assistant(tool_use("t1", command="ls")), result("t1"))
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": _uid(),
        "parentUuid": None,
        "logicalParentUuid": records[-1]["uuid"],
        "timestamp": "2026-10-01T11:00:00Z",
        "compactMetadata": {"trigger": "auto", "preTokens": 180_000, "postTokens": 9_000},
    }
    summary = {
        **user("This session is being continued..."),
        "uuid": _uid(),
        "parentUuid": boundary["uuid"],
        "isCompactSummary": True,
    }
    facts = _main(tmp_path, [*records, boundary, summary])
    [compaction] = facts.compactions
    assert (compaction.trigger, compaction.pre_tokens, compaction.post_tokens) == (
        "auto",
        180_000,
        9_000,
    )
    assert compaction.before_call == 1
    assert [m.text for m in facts.user_messages] == ["a"]


def test_user_message_origins_and_rules(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user(
                "<task-notification>done</task-notification>", origin={"kind": "task-notification"}
            ),
            user("<command-name>/commit</command-name>"),
            user("[Request interrupted by user]"),
            user("ok"),
            user("<local-command-stdout>output</local-command-stdout>"),
            user("caveat", isMeta=True),
        ),
    )
    found = [(m.origin, m.rule_kind, m.slash_command, m.is_interrupt) for m in facts.user_messages]
    assert found == [
        ("task-notification", "other", None, False),
        ("human", "new_task", "/commit", False),
        ("human", "other", None, True),
        ("human", "approval", None, False),
    ]
    first = facts.user_messages[0]
    assert first.message_id == first.dedupe_key
    assert len(first.content_hash) == 16
    assert facts.last_event == "user"


def test_api_error_record_is_excluded_and_counted(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            {
                **assistant(text("API Error: overloaded"), mid="e1", rid="e1"),
                "isApiErrorMessage": True,
            },
        ),
    )
    assert facts.api_errors == 1
    assert facts.requests == ()
    assert facts.last_event == "api_error"
    assert facts.final_text is None


def test_truncated_last_line_is_skipped(tmp_path):
    facts = _main(
        tmp_path, chain(user("go"), assistant(text("x"))), tail='{"type": "user", "uuid": "u-tr'
    )
    assert len(facts.requests) == 1
    assert [m.text for m in facts.user_messages] == ["go"]


def test_prefilter_passes_spaced_uuid_keys(tmp_path):
    path = tmp_path / "-proj" / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    record = {"type": "user", "uuid": "spaced", "parentUuid": None, "message": {"content": "hello"}}
    path.write_text(json.dumps(record, indent=1).replace("\n", " ") + "\n", encoding="utf-8")
    facts = extract_claude(_ref(path))
    assert [m.message_id for m in facts.user_messages] == ["spaced"]


def _subagent(tmp_path, relative, meta):
    path = tmp_path / "-proj" / SID / relative
    _write(
        path,
        chain(
            user("parent prompt", isSidechain=True), assistant(text("sub done"), isSidechain=True)
        ),
    )
    path.with_suffix(".meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return extract_claude(_ref(path))


def test_subagent_is_credited_to_the_parent_session(tmp_path):
    facts = _subagent(
        tmp_path,
        "subagents/agent-abc.jsonl",
        {
            "agentType": "Explore",
            "description": "find things",
            "toolUseId": "toolu_9",
            "spawnDepth": 1,
        },
    )
    assert (facts.session_id, facts.thread_id, facts.thread_key) == (
        SID,
        "agent-abc",
        f"{SID}/agent-abc",
    )
    assert (facts.subagent_type, facts.subagent_description) == ("Explore", "find things")
    assert (facts.parent_tool_use_id, facts.parent_thread_id) == ("toolu_9", MAIN_THREAD)
    assert facts.user_messages[0].origin == "agent"
    assert facts.user_messages[0].rule_kind == "other"
    assert facts.final_text == "sub done"


def test_workflow_subagent_is_credited_to_the_parent_session(tmp_path):
    facts = _subagent(
        tmp_path,
        "subagents/workflows/wf_1/agent-def.jsonl",
        {
            "agentType": "general-purpose",
            "description": "phase",
            "workflowPhase": "x",
            "parentAgentId": "abc",
        },
    )
    assert (facts.session_id, facts.thread_id, facts.thread_key) == (
        SID,
        "agent-def",
        f"{SID}/agent-def",
    )
    assert facts.subagent_type == "general-purpose"
    assert facts.parent_thread_id == "agent-abc"
    assert facts.parent_tool_use_id is None


def test_parallel_calls_whose_last_result_answers_the_first_stay_live(tmp_path):
    head = chain(user("go"))
    a1 = {**assistant(tool_use("t1", command="ls"), mid="m1", rid="r1"), "uuid": _uid()}
    a2 = {**assistant(tool_use("t2", command="pwd"), mid="m1", rid="r1"), "uuid": _uid()}
    a3 = {**assistant(tool_use("t3", command="id"), mid="m1", rid="r1"), "uuid": _uid()}
    for parent, record in zip([head[-1], a1, a2], [a1, a2, a3], strict=True):
        record["parentUuid"] = parent["uuid"]
        record["timestamp"] = "2026-10-01T10:00:00Z"
    results = [
        {**result(call, "ok"), "uuid": _uid(), "parentUuid": owner["uuid"]}
        for call, owner in (("t3", a3), ("t2", a2), ("t1", a1))
    ]
    done = {
        **assistant(text("all done"), mid="m2", rid="r2"),
        "uuid": _uid(),
        "parentUuid": results[-1]["uuid"],
    }
    facts = _main(tmp_path, [*head, a1, a2, a3, *results, done])
    assert facts.rewinds == ()
    assert [(c.call_id, c.on_main_branch) for c in facts.tool_calls] == [
        ("t1", True),
        ("t2", True),
        ("t3", True),
    ]
    assert [r.on_main_branch for r in facts.requests] == [True, True]


def test_markers_quoted_inside_output_do_not_make_errors(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(
                tool_use("t1", command="grep x"),
                tool_use("t2", command="cat y"),
                tool_use("t3", command="cat z"),
                mid="m1",
                rid="r1",
            ),
            result("t1", "log: [Request interrupted by user]", is_error=False),
            result("t2", "docs: User rejected tool use", is_error=None),
            result("t3", "see <tool_use_error> in docs", is_error=None),
            user("what does [Request interrupted by user] mean?"),
        ),
    )
    assert [c.status for c in facts.tool_calls] == ["ok", "ok", "ok"]
    assert [m.is_interrupt for m in facts.user_messages] == [False, False]


def test_leading_markers_still_make_errors(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(tool_use("t1", command="a"), tool_use("t2", command="b"), mid="m1", rid="r1"),
            result("t1", "[Request interrupted by user for tool use]", is_error=False),
            result("t2", "<tool_use_error>ValueError: nope</tool_use_error>", is_error=None),
        ),
    )
    assert [(c.status, c.error_category) for c in facts.tool_calls] == [
        ("error", "interruption"),
        ("error", None),
    ]


def test_manual_compact_command_is_not_a_rewind(tmp_path):
    base = chain(user("a"), assistant(text("b")), {"type": "system", "subtype": "turn_duration"})
    command = {
        **user("<command-name>/compact</command-name>"),
        "uuid": _uid(),
        "parentUuid": base[-1]["uuid"],
        "timestamp": "2026-10-01T10:00:00Z",
    }
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": _uid(),
        "parentUuid": None,
        "logicalParentUuid": base[-1]["uuid"],
        "timestamp": "2026-10-01T11:00:00Z",
        "compactMetadata": {"trigger": "manual"},
    }
    summary = {
        **user("This session is being continued..."),
        "uuid": _uid(),
        "parentUuid": boundary["uuid"],
        "isCompactSummary": True,
    }
    facts = _main(tmp_path, [*base, command, boundary, summary])
    assert facts.rewinds == ()
    assert [m.on_main_branch for m in facts.user_messages] == [True, True]


def test_history_before_a_forward_pointing_boundary_stays_live(tmp_path):
    records = chain(user("a"), assistant(tool_use("t1", command="ls")), result("t1"))
    later = {**user("after"), "uuid": _uid(), "timestamp": "2026-10-01T11:00:00Z"}
    boundary = {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": _uid(),
        "parentUuid": None,
        "logicalParentUuid": later["uuid"],
        "timestamp": "2026-10-01T11:00:00Z",
        "compactMetadata": {"trigger": "auto"},
    }
    later["parentUuid"] = boundary["uuid"]
    facts = _main(tmp_path, [*records, boundary, later])
    assert facts.rewinds == ()
    assert [c.on_main_branch for c in facts.tool_calls] == [True]
    assert [m.on_main_branch for m in facts.user_messages] == [True, True]


def test_fallback_keys_carry_the_thread_key(tmp_path):
    record = assistant(text("x"))
    record["message"]["id"] = None
    del record["requestId"]
    facts = _main(tmp_path, chain(user("go"), record))
    assert facts.requests[0].dedupe_key == f"{SID}:line:2"


def test_nested_subagent_without_a_recorded_parent_has_no_parent_thread(tmp_path):
    facts = _subagent(
        tmp_path,
        "subagents/agent-abc.jsonl",
        {"agentType": "Explore", "description": "d", "spawnDepth": 2},
    )
    assert facts.parent_thread_id is None


def _branching():
    first = chain(user("start"), assistant(text("ok"), mid="m1", rid="r1"))
    root = first[-1]["uuid"]
    abandoned = chain(
        {**user("try A"), "parentUuid": root},
        assistant(tool_use("a1", command="make"), mid="m2", rid="r2"),
        result("a1", "x"),
    )
    live = chain(
        {**user("try B"), "parentUuid": root},
        assistant(tool_use("b1", command="ls"), mid="m3", rid="r3"),
        result("b1", "ok"),
    )
    return [*first, *abandoned, *live]


def test_a_branch_inside_a_subagent_transcript_is_not_a_rewind(tmp_path):
    path = _write(tmp_path / "-proj" / SID / "subagents" / "agent-b.jsonl", _branching())
    path.with_suffix(".meta.json").write_text(json.dumps({"agentType": "x"}), encoding="utf-8")
    sub = extract_claude(_ref(path))
    assert (sub.thread_id, sub.rewinds) == ("agent-b", ())
    assert {c.call_id: c.on_main_branch for c in sub.tool_calls} == {"a1": False, "b1": True}
    [main_rewind] = _main(tmp_path, _branching()).rewinds
    assert main_rewind.abandoned_calls == 1


def test_execute_and_apply_patch_calls_get_shell_and_write_kinds(tmp_path):
    patch = "*** Begin Patch\n*** Update File: /w/src/a.rs\n@@\n-x\n+y\n*** End Patch"
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(
                tool_use("e1", name="Execute", command="uv run pytest -q", riskLevel="low"),
                tool_use("p1", name="ApplyPatch", input=patch),
            ),
            result("e1", "1 passed"),
            result("p1", "applied"),
        ),
    )
    execute, apply = _call(facts, "e1"), _call(facts, "p1")
    assert (execute.kind, execute.command_class, execute.command) == (
        "validate",
        "pytest",
        "uv run pytest -q",
    )
    assert (apply.kind, apply.file_path) == ("write", "/w/src/a.rs")


def test_read_offset_and_limit_are_recorded(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(
                tool_use("r1", name="Read", file_path="/w/a.py", offset=120, limit=40),
                tool_use("r2", name="Read", file_path="/w/a.py"),
            ),
            result("r1", "lines"),
            result("r2", "lines"),
        ),
    )
    assert (_call(facts, "r1").read_offset, _call(facts, "r1").read_limit) == (120, 40)
    assert (_call(facts, "r2").read_offset, _call(facts, "r2").read_limit) == (None, None)


def test_a_probe_ending_in_grep_that_matches_nothing_is_ok(tmp_path):
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(tool_use("t1", command="ls src; echo ---; grep -n needle src/a.py")),
            result("t1", "Exit code 1\na.py\nb.py\n---", is_error=True),
        ),
    )
    call = _call(facts, "t1")
    assert (call.status, call.error_category, call.error_text) == ("ok", None, None)


def test_benign_exit_reads_the_whole_command_past_the_cap(tmp_path):
    command = f"echo {'x' * 600}; grep -n needle src/a.py"
    facts = _main(
        tmp_path,
        chain(
            user("go"),
            assistant(tool_use("t1", command=command)),
            result("t1", "Exit code 1\nxxxx", is_error=True),
        ),
    )
    assert _call(facts, "t1").status == "ok"
