import json
from pathlib import Path

from ringleader.commands import hooks


def test_install_registers_a_claude_session_end_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text("{}")

    assert hooks.cmd_install(["--yes"]) == 0

    data = json.loads(settings.read_text())
    entries = data["hooks"]["SessionEnd"]
    assert any("session_end.sh" in h["command"] for group in entries for h in group["hooks"])


def test_install_uses_codex_session_end_timeout_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".codex").mkdir()
    settings = tmp_path / ".codex" / "hooks.json"
    settings.write_text("{}")

    assert hooks.cmd_install(["--yes"]) == 0

    entries = json.loads(settings.read_text())["hooks"]["SessionEnd"]
    ours = next(
        h
        for group in entries
        for h in group["hooks"]
        if "session_end.sh" in h["command"]
    )
    assert ours["timeout"] == 3


def test_update_repairs_an_existing_codex_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".codex").mkdir()
    settings = tmp_path / ".codex" / "hooks.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": str(hooks._session_end_script()),
                                    "timeout": 5,
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )

    assert hooks.cmd_update(["--yes"]) == 0

    entry = json.loads(settings.read_text())["hooks"]["SessionEnd"][0]["hooks"][0]
    assert entry["timeout"] == 3


def test_install_previews_a_diff_and_keeps_a_backup(tmp_path, monkeypatch, capsys):
    """The same file `rl install statusline` edits, so the same convention: show the
    change, keep the old bytes."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    original = '{\n    "cleanupPeriodDays": 30\n}\n'
    settings.write_text(original)

    assert hooks.cmd_install(["--yes"]) == 0

    out = capsys.readouterr().out
    assert f"--- {settings}" in out and f"+++ {settings}" in out
    assert '+  "cleanupPeriodDays": 30,' in out  # the re-indent is shown, not silent
    assert '+            "type": "command",' in out
    backup = settings.with_name("settings.json.bak")
    assert backup.read_text() == original
    assert "backup" in out


def test_install_prompts_before_showing_nothing_to_change(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text("{}")
    hooks.cmd_install(["--yes"])
    capsys.readouterr()

    assert hooks.cmd_install(["--yes"]) == 0
    out = capsys.readouterr().out
    assert "already installed" in out
    assert "synced 0 target(s)" in out
    assert not settings.with_name("settings.json.bak.bak").exists()


def test_a_failed_write_leaves_the_original_config_intact(tmp_path, monkeypatch, capsys):
    """The write is temp-file + os.replace, so an interrupted or failing write can
    never leave the user's settings truncated."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    original = '{\n    "cleanupPeriodDays": 30\n}\n'
    settings.write_text(original)

    def boom(*_a, **_kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("ringleader.safe_write.os.replace", boom)

    assert hooks.cmd_install(["--yes"]) == 1
    assert settings.read_text() == original
    assert "could not write" in capsys.readouterr().err
    leftovers = [p.name for p in (tmp_path / ".claude").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_install_preserves_a_foreign_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [{"hooks": [{"type": "command", "command": "/other/thing.sh"}]}]
                }
            }
        )
    )

    hooks.cmd_install(["--yes"])

    commands = [
        h["command"]
        for g in json.loads(settings.read_text())["hooks"]["SessionEnd"]
        for h in g["hooks"]
    ]
    assert "/other/thing.sh" in commands
    assert any("session_end.sh" in c for c in commands)


def test_install_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text("{}")
    hooks.cmd_install(["--yes"])
    once = settings.read_text()
    hooks.cmd_install(["--yes"])
    assert settings.read_text() == once


def test_uninstall_removes_only_our_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [{"hooks": [{"type": "command", "command": "/other/thing.sh"}]}]
                }
            }
        )
    )
    hooks.cmd_install(["--yes"])

    assert hooks.cmd_uninstall(["--yes"]) == 0

    commands = [
        h["command"]
        for g in json.loads(settings.read_text())["hooks"]["SessionEnd"]
        for h in g["hooks"]
    ]
    assert commands == ["/other/thing.sh"]


def test_install_links_the_pi_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".pi" / "agent").mkdir(parents=True)
    hooks.cmd_install(["--yes"])
    assert (tmp_path / ".pi" / "agent" / "extensions" / "pi-ringleader-finish.ts").exists()


def test_status_names_opencode_as_unsupported(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    hooks.cmd_status([])
    out = capsys.readouterr().out.lower()
    assert "opencode" in out and "no session-end" in out


def test_install_reports_an_unreadable_config_without_overwriting(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    original = json.dumps({"cleanupPeriodDays": 30})
    settings.write_text(original)
    settings.chmod(0o000)
    try:
        rc = hooks.cmd_install(["--yes"])
    finally:
        settings.chmod(0o644)

    assert rc == 0
    assert settings.read_text() == original
    err = capsys.readouterr().err.lower()
    assert "unreadable" in err


def test_stat_denial_does_not_mask_an_unreadable_config(tmp_path, monkeypatch, capsys):
    """Pins the mechanism behind the fixed Critical, not just its symptom. A plain
    `chmod` on the file (see the test above) reaches "read denied" but not "stat
    denied, write permitted" — POSIX ties a directory's write bit to the same execute
    bit that blocks stat, so no portable `chmod` alone isolates that split. Simulate
    the environment instead: make `Path.exists()` raise for this one path (as a
    sandbox that grants read/write separately could), while the file's own
    permissions still make `read_text()` raise its own `PermissionError` for real.
    If anything in the code path still used `.exists()`/`_exists()` as a "does this
    file exist" precheck the way the pre-fix `_load_json_or_none` did, that raise
    would be caught and read as "absent," and the config would get silently replaced.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    original = json.dumps({"cleanupPeriodDays": 30})
    settings.write_text(original)
    settings.chmod(0o000)

    real_exists = Path.exists

    def fake_exists(self, *args, **kwargs):
        if self == settings:
            raise PermissionError(13, "Permission denied")
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", fake_exists)

    try:
        rc = hooks.cmd_install(["--yes"])
    finally:
        settings.chmod(0o644)

    assert rc == 0
    assert settings.read_text() == original
    err = capsys.readouterr().err.lower()
    assert "unreadable" in err


def test_update_never_opts_in_an_agent_never_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    original = "{}"
    settings.write_text(original)

    assert hooks.cmd_update(["--yes"]) == 0

    assert settings.read_text() == original
    assert json.loads(settings.read_text()) == {}


def test_malformed_json_is_preserved_across_every_verb(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    original = "{not valid json"
    settings.write_text(original)

    hooks.cmd_install(["--yes"])
    assert settings.read_text() == original

    hooks.cmd_status([])
    assert settings.read_text() == original

    hooks.cmd_uninstall(["--yes"])
    assert settings.read_text() == original


def test_declining_still_reports_a_malformed_config(tmp_path, monkeypatch):
    """The planning-time failure is found before the prompt; declining the *other*
    target must not swallow its exit code."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"hooks": "nope"}')
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "hooks.json").write_text("{}")
    monkeypatch.setattr("builtins.input", lambda *_: "n")

    assert hooks.cmd_install([]) == 1

    assert json.loads((tmp_path / ".codex" / "hooks.json").read_text()) == {}
