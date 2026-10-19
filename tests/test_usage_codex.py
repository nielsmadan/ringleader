import json

from ringleader.usage.codex import CodexUsageReader, own_events_start, own_thread

SESSION = "01a0118b-f3a9-7453-af81-02d2d94a4e64"


def _token_count(inp, cached, cw, out, reasoning, *, ts, rate_limits=None):  # noqa: PLR0913 -- test data-builder helper, one arg per fixture field
    payload = {
        "type": "token_count",
        "info": {
            "last_token_usage": {
                "input_tokens": inp,
                "cached_input_tokens": cached,
                "cache_write_input_tokens": cw,
                "output_tokens": out,
                "reasoning_output_tokens": reasoning,
                "total_tokens": inp + out,
            },
            "total_token_usage": {"input_tokens": 999999, "output_tokens": 999999},
            "model_context_window": 272000,
        },
    }
    if rate_limits is not None:
        payload["rate_limits"] = rate_limits
    return {"type": "event_msg", "timestamp": ts, "payload": payload}


def _write(dir_, records, meta=None):
    d = dir_ / "2026" / "08" / "17"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-2026-08-17T23-06-19-{SESSION}.jsonl"
    lines = ([json.dumps(meta)] if meta else []) + [json.dumps(r) for r in records]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _session_meta(cwd):
    return {"type": "session_meta", "timestamp": "2026-08-17T21:00:00Z", "payload": {"cwd": cwd}}


def test_subtracts_cached_from_input_to_get_disjoint_buckets(tmp_path):
    _write(tmp_path, [_token_count(26481, 25344, 0, 182, 63, ts="2026-08-17T21:06:26.967Z")])

    recs = list(CodexUsageReader(sessions_dir=tmp_path).records())

    assert len(recs) == 1
    r = recs[0]
    assert r.input == 26481 - 25344  # uncached portion only
    assert r.cache_read == 25344
    assert r.cache_write == 0
    assert r.output == 182
    assert r.reasoning == 63  # subset of output
    assert r.total == 26481 + 182  # matches codex's own total_tokens
    assert r.session_id == SESSION
    assert r.source == "codex"


def test_uses_per_turn_delta_not_the_running_total(tmp_path):
    _write(
        tmp_path,
        [
            _token_count(100, 0, 0, 10, 0, ts="2026-08-17T21:06:26.967Z"),
            _token_count(200, 0, 0, 20, 0, ts="2026-08-17T21:07:26.967Z"),
        ],
    )

    recs = list(CodexUsageReader(sessions_dir=tmp_path).records())

    assert [r.total for r in recs] == [110, 220]


def test_parses_weekly_and_five_hour_windows(tmp_path):
    _write(
        tmp_path,
        [
            _token_count(
                100,
                0,
                0,
                10,
                0,
                ts="2026-08-17T21:06:26.967Z",
                rate_limits={
                    "primary": {
                        "used_percent": 47.0,
                        "window_minutes": 10080,
                        "resets_at": 1787213354,
                    },
                    "secondary": {
                        "used_percent": 12.5,
                        "window_minutes": 300,
                        "resets_at": 1787200000,
                    },
                    "plan_type": "pro",
                },
            )
        ],
    )

    wins = list(CodexUsageReader(sessions_dir=tmp_path).windows())

    by_kind = {w.window: w for w in wins}
    assert by_kind["weekly"].used_percent == 47.0
    assert by_kind["weekly"].window_minutes == 10080
    assert by_kind["weekly"].plan_type == "pro"
    assert by_kind["5h"].used_percent == 12.5
    assert by_kind["weekly"].resets_at.startswith("2026-")


def test_null_secondary_window_is_skipped(tmp_path):
    _write(
        tmp_path,
        [
            _token_count(
                100,
                0,
                0,
                10,
                0,
                ts="2026-08-17T21:06:26.967Z",
                rate_limits={
                    "primary": {
                        "used_percent": 47.0,
                        "window_minutes": 10080,
                        "resets_at": 1787213354,
                    },
                    "secondary": None,
                    "plan_type": "pro",
                },
            )
        ],
    )

    assert [w.window for w in CodexUsageReader(sessions_dir=tmp_path).windows()] == ["weekly"]


def test_available_is_false_without_the_directory(tmp_path):
    assert CodexUsageReader(sessions_dir=tmp_path / "nope").available() is False


def test_non_numeric_window_field_is_skipped_not_raised(tmp_path):
    _write(
        tmp_path,
        [
            _token_count(
                100,
                0,
                0,
                10,
                0,
                ts="2026-08-17T21:06:26.967Z",
                rate_limits={
                    "primary": {
                        "used_percent": "n/a",
                        "window_minutes": 10080,
                        "resets_at": 1787213354,
                    },
                    "secondary": None,
                    "plan_type": "pro",
                },
            )
        ],
    )

    assert list(CodexUsageReader(sessions_dir=tmp_path).windows()) == []


def test_one_turn_per_rate_limit_family_counts_once(tmp_path):
    turn = _token_count(
        26481,
        25344,
        0,
        182,
        63,
        ts="2026-07-25T17:11:25.192Z",
        rate_limits={"primary": {"used_percent": 5.0, "window_minutes": 300}, "plan_type": "plus"},
    )
    other_family = json.loads(json.dumps(turn))
    other_family["payload"]["rate_limits"] = {
        "primary": {"used_percent": 9.0, "window_minutes": 300},
        "limit_id": "codex_bengalfox",
        "plan_type": "plus",
    }
    _write(tmp_path, [turn, other_family])

    recs = list(CodexUsageReader(sessions_dir=tmp_path).records())

    assert len(recs) == 1
    assert recs[0].total == 26481 + 182


def test_a_genuinely_repeated_turn_later_in_the_file_is_kept(tmp_path):
    turn = _token_count(100, 0, 0, 10, 0, ts="2026-07-25T17:11:25.192Z")
    other = _token_count(200, 0, 0, 20, 0, ts="2026-07-25T17:11:25.192Z")
    _write(tmp_path, [turn, other, turn])

    assert len(list(CodexUsageReader(sessions_dir=tmp_path).records())) == 3


def test_record_id_is_the_file_and_line_ordinal(tmp_path):
    path = _write(
        tmp_path,
        [
            _token_count(100, 0, 0, 10, 0, ts="2026-07-25T17:11:25.192Z"),
            _token_count(200, 0, 0, 20, 0, ts="2026-07-25T17:11:25.192Z"),
        ],
    )

    recs = list(CodexUsageReader(sessions_dir=tmp_path).records())

    assert [r.record_id for r in recs] == [f"{path}|0", f"{path}|1"]


def test_project_comes_from_the_rollout_session_meta(tmp_path):
    _write(
        tmp_path,
        [_token_count(100, 0, 0, 10, 0, ts="2026-08-17T21:06:26.967Z")],
        meta=_session_meta("/Users/x/wrksp/foo"),
    )

    recs = list(CodexUsageReader(sessions_dir=tmp_path).records())

    assert recs[0].project == "/Users/x/wrksp/foo"


def test_project_is_none_when_the_rollout_has_no_session_meta(tmp_path):
    _write(tmp_path, [_token_count(100, 0, 0, 10, 0, ts="2026-08-17T21:06:26.967Z")])

    assert next(iter(CodexUsageReader(sessions_dir=tmp_path).records())).project is None


CHILD = "01a12cfc-2eaa-7492-8e3d-76c57fc66ffb"
PARENT = "01a12cfb-c620-7ad3-aa4e-1c540d1bf686"


def _turn_context(model):
    return {
        "type": "turn_context",
        "timestamp": "2026-08-17T21:00:00Z",
        "payload": {"model": model},
    }


def _write_named(dir_, session, records):
    d = dir_ / "2026" / "08" / "17"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-2026-08-17T23-06-19-{session}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _fork_meta(*, ordinal):
    payload = {"id": CHILD, "forked_from_id": PARENT, "cwd": "/x"}
    if ordinal is not None:
        payload["subagent_history_start_ordinal"] = ordinal
    return {"type": "session_meta", "timestamp": "2026-08-17T21:00:00Z", "payload": payload}


def _parent_meta():
    return {
        "type": "session_meta",
        "timestamp": "2026-08-17T21:00:00Z",
        "payload": {"id": PARENT, "cwd": "/x"},
    }


def _settings_applied(thread):
    return {
        "type": "event_msg",
        "timestamp": "2026-08-17T21:00:00Z",
        "payload": {"type": "thread_settings_applied", "thread_id": thread},
    }


def test_token_counts_take_the_preceding_turn_context_model(tmp_path):
    _write(
        tmp_path,
        [
            _token_count(10, 0, 0, 1, 0, ts="2026-08-17T21:00:01Z"),
            _turn_context("gpt-a"),
            _token_count(20, 0, 0, 2, 0, ts="2026-08-17T21:00:02Z"),
            _turn_context("gpt-b"),
            _token_count(30, 0, 0, 3, 0, ts="2026-08-17T21:00:03Z"),
        ],
    )

    recs = list(CodexUsageReader(sessions_dir=tmp_path).records())

    assert [r.model for r in recs] == [None, "gpt-a", "gpt-b"]


def test_forked_rollout_counts_only_its_own_token_counts(tmp_path):
    replay = [
        _parent_meta(),
        _turn_context("gpt-old"),
        _token_count(900, 0, 0, 90, 0, ts="2026-08-17T20:00:00Z"),
        _settings_applied(CHILD),
    ]
    own = [
        _turn_context("gpt-new"),
        _token_count(10, 0, 0, 1, 0, ts="2026-08-17T21:00:01Z"),
        _token_count(20, 0, 0, 2, 0, ts="2026-08-17T21:00:02Z"),
    ]
    _write_named(tmp_path, CHILD, [_fork_meta(ordinal=len(replay) + 1), *replay, *own])

    recs = list(CodexUsageReader(sessions_dir=tmp_path).records())

    assert [(r.input, r.output, r.model) for r in recs] == [(10, 1, "gpt-new"), (20, 2, "gpt-new")]


def test_a_fork_whose_ordinal_is_the_line_count_still_counts_its_own_turn(tmp_path):
    stamp = "2026-08-17T21:00:00"
    replay = [
        _parent_meta(),
        _token_count(900, 0, 0, 90, 0, ts=f"{stamp}.100Z"),
        _settings_applied(PARENT),
    ]
    own = [
        _token_count(10, 0, 0, 1, 0, ts="2026-08-17T21:00:05Z"),
        _token_count(20, 0, 0, 2, 0, ts="2026-08-17T21:00:06Z"),
    ]
    lines = [_fork_meta(ordinal=len(replay) + len(own) + 1), *replay, *own]
    _write_named(tmp_path, CHILD, lines)

    recs = list(CodexUsageReader(sessions_dir=tmp_path).records())

    assert [r.input for r in recs] == [10, 20]


def test_a_fork_whose_ordinal_is_the_line_count_and_replays_only_counts_nothing(tmp_path):
    replay = [_parent_meta(), _token_count(900, 0, 0, 90, 0, ts="2026-08-17T21:00:00Z")]
    _write_named(tmp_path, CHILD, [_fork_meta(ordinal=len(replay) + 1), *replay])

    assert list(CodexUsageReader(sessions_dir=tmp_path).records()) == []


def test_a_fork_without_an_ordinal_starts_at_its_thread_settings_event(tmp_path):
    lines = [
        _fork_meta(ordinal=None),
        _token_count(900, 0, 0, 90, 0, ts="2026-08-17T20:00:00Z"),
        _settings_applied(CHILD),
        _token_count(10, 0, 0, 1, 0, ts="2026-08-17T21:00:01Z"),
    ]
    _write_named(tmp_path, CHILD, lines)

    recs = list(CodexUsageReader(sessions_dir=tmp_path).records())

    assert [r.input for r in recs] == [10]


def test_own_events_start_is_zero_for_a_rollout_that_replays_nothing():
    records = [(0, _parent_meta()), (1, _settings_applied(PARENT))]

    assert own_events_start(records) == 0
    assert own_events_start([]) == 0


def test_own_events_start_reads_the_recorded_ordinal():
    records = [(0, _fork_meta(ordinal=12)), *((i, _settings_applied(PARENT)) for i in range(1, 20))]

    assert own_events_start(records) == 12


def test_own_thread_accepts_unstamped_and_own_payloads_only():
    assert own_thread({}, CHILD) is True
    assert own_thread({"thread_id": CHILD}, CHILD) is True
    assert own_thread({"thread_id": PARENT}, CHILD) is False


def test_a_token_count_stamped_with_another_thread_is_excluded(tmp_path):
    foreign = _token_count(900, 0, 0, 90, 0, ts="2026-08-17T21:00:01Z")
    foreign["payload"]["thread_id"] = PARENT
    mine = _token_count(10, 0, 0, 1, 0, ts="2026-08-17T21:00:02Z")
    mine["payload"]["thread_id"] = SESSION
    _write(tmp_path, [foreign, mine])

    recs = list(CodexUsageReader(sessions_dir=tmp_path).records())

    assert [r.input for r in recs] == [10]


def test_windows_are_unchanged_by_turn_context_and_replay_lines(tmp_path):
    limits = {
        "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1787000000},
        "secondary": None,
    }
    tc = _token_count(10, 0, 0, 1, 0, ts="2026-08-17T21:00:01Z", rate_limits=limits)
    plain = tmp_path / "plain"
    noisy = tmp_path / "noisy"
    _write(plain, [tc])
    _write(noisy, [_turn_context("gpt-a"), tc])

    assert list(CodexUsageReader(sessions_dir=noisy).windows()) == list(
        CodexUsageReader(sessions_dir=plain).windows()
    )
