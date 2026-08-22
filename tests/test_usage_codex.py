import json

from ringleader.usage.codex import CodexUsageReader

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
