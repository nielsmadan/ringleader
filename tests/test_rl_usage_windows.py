import json
from datetime import UTC, datetime

import pytest
from ringleader.commands import usage as usage_cmd
from ringleader.usage.base import WindowSnapshot

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _win(
    window="weekly",
    used=47.0,
    ts="2026-08-17T21:06:26Z",
    minutes=10080,
    resets="2026-08-24T12:09:14+00:00",
):
    return WindowSnapshot(
        source="codex",
        timestamp=ts,
        plan_type="pro",
        window=window,
        window_minutes=minutes,
        used_percent=used,
        resets_at=resets,
    )


@pytest.fixture
def fake_windows(monkeypatch):
    wins = []
    monkeypatch.setattr(usage_cmd, "collect_windows", lambda: list(wins))
    monkeypatch.setattr(usage_cmd, "collect_claude_windows", lambda: [])
    monkeypatch.setattr(usage_cmd, "_now", lambda: NOW)
    return wins


def test_reports_latest_snapshot_per_window(fake_windows, capsys):
    fake_windows += [
        _win(used=10.0, ts="2026-08-17T20:00:00Z"),
        _win(used=47.0, ts="2026-08-17T21:00:00Z"),
        _win(window="5h", used=12.5, ts="2026-08-17T21:00:00Z", minutes=300),
    ]

    rc = usage_cmd.cmd_usage(["windows", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["codex"]["weekly"]["used_percent"] == 47.0  # latest, not the earlier 10.0
    assert out["codex"]["5h"]["used_percent"] == 12.5
    assert out["claude"] == {}


def test_expired_window_is_reported_separately_not_as_current(fake_windows, capsys):
    fake_windows += [
        _win(),
        _win(
            window="5h",
            used=1.0,
            ts="2026-06-05T20:59:00Z",
            minutes=300,
            resets="2026-06-06T01:57:00+00:00",
        ),
    ]

    usage_cmd.cmd_usage(["windows", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert set(out["codex"]) == {"weekly"}
    assert out["codex_expired"]["5h"]["as_of"].startswith("2026-06-05")


def test_expired_window_text_says_there_is_no_current_snapshot(fake_windows, capsys):
    fake_windows += [
        _win(
            window="5h",
            used=1.0,
            ts="2026-06-05T20:59:00Z",
            minutes=300,
            resets="2026-06-06T01:57:00+00:00",
        )
    ]

    usage_cmd.cmd_usage(["windows"])

    text = capsys.readouterr().out
    assert "no current snapshot" in text
    assert "1.0%" not in text


def test_window_without_resets_at_expires_after_its_own_length(fake_windows, capsys):
    fake_windows += [
        _win(window="5h", ts="2026-08-18T11:50:00Z", minutes=300, resets=""),
        _win(window="weekly", ts="2026-08-10T11:50:00Z", minutes=10080, resets=""),
    ]

    usage_cmd.cmd_usage(["windows", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert set(out["codex"]) == {"5h"}  # 10 minutes old, 5h window
    assert set(out["codex_expired"]) == {"weekly"}  # 8 days old, 7-day window


def test_text_output_points_claude_at_the_installer_when_absent(fake_windows, capsys):
    fake_windows += [_win()]

    usage_cmd.cmd_usage(["windows"])

    text = capsys.readouterr().out
    assert "47" in text and "weekly" in text
    assert "rl install statusline" in text


def test_empty_state_when_no_codex_data(fake_windows, capsys):
    usage_cmd.cmd_usage(["windows"])
    assert "no codex window data" in capsys.readouterr().out.lower()


def test_claude_windows_render_beside_codex(monkeypatch, capsys):
    monkeypatch.setattr(usage_cmd, "collect_windows", lambda: [])
    monkeypatch.setattr(usage_cmd, "_now", lambda: NOW)
    monkeypatch.setattr(
        usage_cmd,
        "collect_claude_windows",
        lambda: [
            WindowSnapshot(
                source="claude",
                timestamp="2026-08-22T11:59:00+00:00",
                plan_type=None,
                window="5h",
                window_minutes=300,
                used_percent=23.5,
                resets_at="2026-08-22T13:00:00+00:00",
            )
        ],
    )

    rc = usage_cmd.cmd_usage(["windows", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["claude"]["5h"]["used_percent"] == 23.5
    assert "available" not in out["claude"]


def test_claude_absent_points_at_the_installer(monkeypatch, capsys):
    monkeypatch.setattr(usage_cmd, "collect_windows", lambda: [])
    monkeypatch.setattr(usage_cmd, "collect_claude_windows", lambda: [])

    usage_cmd.cmd_usage(["windows"])

    assert "rl install statusline" in capsys.readouterr().out


def test_expired_claude_window_is_not_shown_as_current(monkeypatch, capsys):
    monkeypatch.setattr(usage_cmd, "collect_windows", lambda: [])
    monkeypatch.setattr(
        usage_cmd,
        "collect_claude_windows",
        lambda: [
            WindowSnapshot(
                source="claude",
                timestamp="2026-06-05T20:59:00+00:00",
                plan_type=None,
                window="5h",
                window_minutes=300,
                used_percent=1.0,
                resets_at="2026-06-06T01:57:00+00:00",
            )
        ],
    )

    usage_cmd.cmd_usage(["windows"])

    assert "no current snapshot" in capsys.readouterr().out
