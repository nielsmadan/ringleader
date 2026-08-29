import json
from datetime import UTC, datetime

from ringleader.usage.claude_limits import read_windows

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)
CAPTURED = int(NOW.timestamp()) - 60  # one minute old: fresh
RESETS_5H = int(NOW.timestamp()) + 3600  # an hour out: current
RESETS_7D = int(NOW.timestamp()) + 86400


def _write(tmp_path, body):
    p = tmp_path / "claude-limits.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def _capture(five=23.5, seven=41.2, captured_at=CAPTURED):
    return {
        "captured_at": captured_at,
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": RESETS_5H},
            "seven_day": {"used_percentage": seven, "resets_at": RESETS_7D},
        },
    }


def test_reads_both_windows(tmp_path):
    got = read_windows(_write(tmp_path, _capture()), NOW)

    by = {w.window: w for w in got}
    assert sorted(by) == ["5h", "weekly"]
    assert by["5h"].used_percent == 23.5
    assert by["5h"].window_minutes == 300
    assert by["5h"].source == "claude"
    assert by["weekly"].used_percent == 41.2
    assert by["weekly"].window_minutes == 10080
    assert by["5h"].resets_at.startswith("2026-")


def test_epoch_shaped_percentage_is_dropped(tmp_path):
    # claude-code#52326: used_percentage can carry the resets_at epoch
    got = read_windows(_write(tmp_path, _capture(five=RESETS_5H)), NOW)

    assert [w.window for w in got] == ["weekly"]


def test_rounding_overshoot_is_clamped(tmp_path):
    got = read_windows(_write(tmp_path, _capture(five=100.4)), NOW)

    assert next(w for w in got if w.window == "5h").used_percent == 100.0


def test_non_finite_and_bool_are_rejected(tmp_path):
    for bad in ("NaN", "Infinity", "true"):
        p = tmp_path / "claude-limits.json"
        p.write_text(
            f'{{"captured_at": {CAPTURED}, "rate_limits": {{"five_hour": '
            f'{{"used_percentage": {bad}, "resets_at": {RESETS_5H}}}}}}}',
            encoding="utf-8",
        )
        assert read_windows(p, NOW) == [], bad


def test_stale_capture_yields_nothing(tmp_path):
    old = int(NOW.timestamp()) - 601
    assert read_windows(_write(tmp_path, _capture(captured_at=old)), NOW) == []


def test_expired_window_is_still_returned_for_the_caller_to_judge(tmp_path):
    body = _capture()
    body["rate_limits"]["five_hour"]["resets_at"] = int(NOW.timestamp()) - 10
    got = read_windows(_write(tmp_path, body), NOW)

    # read_windows reports what was captured; window_is_current decides currency
    assert {w.window for w in got} == {"5h", "weekly"}


def test_missing_file_and_null_rate_limits_and_garbage(tmp_path):
    assert read_windows(tmp_path / "nope.json", NOW) == []
    assert read_windows(_write(tmp_path, {"captured_at": CAPTURED, "rate_limits": None}), NOW) == []
    p = tmp_path / "claude-limits.json"
    p.write_text("{not json", encoding="utf-8")
    assert read_windows(p, NOW) == []


def test_one_window_absent(tmp_path):
    body = {
        "captured_at": CAPTURED,
        "rate_limits": {"seven_day": {"used_percentage": 5.0, "resets_at": RESETS_7D}},
    }
    assert [w.window for w in read_windows(_write(tmp_path, body), NOW)] == ["weekly"]


def test_millisecond_scale_resets_at_drops_window(tmp_path):
    body = _capture()
    body["rate_limits"]["five_hour"]["resets_at"] = 999999999999999
    got = read_windows(_write(tmp_path, body), NOW)

    assert [w.window for w in got] == ["weekly"]


def test_captured_at_out_of_range_yields_nothing(tmp_path):
    assert read_windows(_write(tmp_path, _capture(captured_at=999999999999999)), NOW) == []


def test_huge_resets_at_drops_window(tmp_path):
    body = _capture()
    body["rate_limits"]["five_hour"]["resets_at"] = 10**400
    got = read_windows(_write(tmp_path, body), NOW)

    assert [w.window for w in got] == ["weekly"]


def test_huge_captured_at_yields_nothing(tmp_path):
    assert read_windows(_write(tmp_path, _capture(captured_at=10**400)), NOW) == []


def test_huge_used_percentage_drops_window(tmp_path):
    body = _capture()
    body["rate_limits"]["five_hour"]["used_percentage"] = 10**400
    got = read_windows(_write(tmp_path, body), NOW)

    assert [w.window for w in got] == ["weekly"]
