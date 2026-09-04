import json

from ringleader import presence
from ringleader.commands import doctor


def test_reports_low_retention(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"cleanupPeriodDays": 7}))
    r = doctor.check_claude_retention(p)
    assert r["value"] == 7 and r["ok"] is False and "cleanupPeriodDays" in r["advice"]


def test_reports_ok_retention(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"cleanupPeriodDays": 90}))
    assert doctor.check_claude_retention(p)["ok"] is True


def test_missing_file_is_not_ok(tmp_path):
    r = doctor.check_claude_retention(tmp_path / "nope.json")
    assert r["value"] is None and r["ok"] is False


def test_settings_path_falls_back_to_the_env_override(tmp_path, monkeypatch, capsys):
    """A sandboxed run redirects three of four inputs via env; settings must follow too,
    or `rl doctor` silently reads the real ~/.claude/settings.json."""
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / "sandbox-settings.json"
    p.write_text(json.dumps({"cleanupPeriodDays": 7}))
    monkeypatch.setenv("RINGLEADER_CLAUDE_SETTINGS", str(p))
    assert doctor.cmd_doctor([]) == 0
    assert "cleanupPeriodDays=7" in capsys.readouterr().out


def test_settings_flag_beats_the_env_override(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps({"cleanupPeriodDays": 7}))
    flag_file = tmp_path / "flag.json"
    flag_file.write_text(json.dumps({"cleanupPeriodDays": 90}))
    monkeypatch.setenv("RINGLEADER_CLAUDE_SETTINGS", str(env_file))
    assert doctor.cmd_doctor(["--settings", str(flag_file)]) == 0
    assert "cleanupPeriodDays=90" in capsys.readouterr().out


def test_doctor_reports_every_setup_surface(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text('{"cleanupPeriodDays": 30}')

    assert doctor.cmd_doctor(["--settings", str(settings)]) == 0

    out = capsys.readouterr().out
    assert "cleanupPeriodDays" in out
    assert "skills" in out.lower()
    assert "statusline" in out.lower()


def test_doctor_warns_when_hooklinesinker_is_absent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"cleanupPeriodDays": 90}))

    assert doctor.cmd_doctor(["--settings", str(settings)]) == 0

    out = capsys.readouterr().out
    assert "--- presence ---" in out
    assert presence.SETUP_COMMAND in out
    assert "⚠ hooklinesinker" in out


def test_doctor_reports_malformed_presence_state_as_an_error_and_still_exits_zero(
    tmp_path, monkeypatch, capsys
):
    """Reporting commands in this CLI always exit 0; a broken install is an error glyph."""
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"cleanupPeriodDays": 90}))
    monkeypatch.setattr(
        doctor,
        "presence_rows",
        lambda _client: [
            {"label": "status ledger", "state": "error", "detail": "failed to parse a record"}
        ],
    )

    assert doctor.cmd_doctor(["--settings", str(settings)]) == 0

    assert "✖ status ledger: failed to parse a record" in capsys.readouterr().out


def test_doctor_reports_absent_claude_settings_as_not_installed(tmp_path, monkeypatch, capsys):
    """`rl doctor` is step 3 of `ringleader-init`, and a Codex-only machine has no
    ~/.claude/settings.json at all — that reads as "not installed", not an error."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".codex").mkdir()
    settings = tmp_path / ".claude" / "settings.json"

    assert doctor.cmd_doctor(["--settings", str(settings)]) == 0

    captured = capsys.readouterr()
    assert f"target: none ({settings} does not exist)" in captured.out
    assert "snippet: not installed" in captured.out
    assert "could not read" not in captured.err
