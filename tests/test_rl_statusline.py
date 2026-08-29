import json
import stat

from ringleader.commands import statusline
from ringleader.paths import state_dir

BASE_CMD = "input=$(cat); echo \"$input\" | jq -r '.model.display_name'"


def _settings(tmp_path, cmd=BASE_CMD, extra=None):
    p = tmp_path / "settings.json"
    body = {"statusLine": {"type": "command", "command": cmd}, "model": "opus"}
    if extra:
        body.update(extra)
    p.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return p


def test_install_inserts_snippet_and_preserves_everything_else(tmp_path):
    p = _settings(tmp_path)

    rc = statusline.cmd_install(["--settings", str(p), "--yes"])

    body = json.loads(p.read_text())
    assert rc == 0
    assert "ringleader-capture" in body["statusLine"]["command"]
    assert BASE_CMD in body["statusLine"]["command"]  # original command intact
    assert body["model"] == "opus"  # unrelated keys untouched
    assert body["statusLine"]["type"] == "command"


def test_install_ensures_state_dir_exists(tmp_path):
    p = _settings(tmp_path)

    statusline.cmd_install(["--settings", str(p), "--yes"])

    assert state_dir().exists()


def test_install_writes_a_backup(tmp_path):
    p = _settings(tmp_path)
    original = p.read_text()

    statusline.cmd_install(["--settings", str(p), "--yes"])

    backups = [f for f in tmp_path.iterdir() if f.name != "settings.json"]
    assert backups and backups[0].read_text() == original


def test_install_is_idempotent(tmp_path):
    p = _settings(tmp_path)
    statusline.cmd_install(["--settings", str(p), "--yes"])
    once = p.read_text()

    statusline.cmd_install(["--settings", str(p), "--yes"])

    assert p.read_text() == once


def test_uninstall_restores_the_original_exactly(tmp_path):
    p = tmp_path / "settings.json"
    original = json.dumps(
        {
            "statusLine": {"type": "command", "command": BASE_CMD},
            "model": "opus",
            "note": "café raw unicode ☕",
        },
        indent=4,
        ensure_ascii=False,
    )
    p.write_text(original, encoding="utf-8")
    statusline.cmd_install(["--settings", str(p), "--yes"])

    rc = statusline.cmd_uninstall(["--settings", str(p), "--yes"])

    assert rc == 0
    # raw bytes, not just parsed equality — a round-trip that re-dumps the whole
    # document (e.g. ensure_ascii escaping, a different indent) can pass a parsed
    # comparison while still mangling content this task promises to leave untouched.
    assert p.read_text(encoding="utf-8") == original


def test_refuses_when_no_statusline_configured(tmp_path, capsys):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "opus"}), encoding="utf-8")

    rc = statusline.cmd_install(["--settings", str(p), "--yes"])

    assert rc != 0
    assert "jq -c" in capsys.readouterr().out  # prints the snippet to paste
    assert json.loads(p.read_text()) == {"model": "opus"}  # unchanged


def test_refuses_when_the_command_does_not_capture_stdin(tmp_path, capsys):
    p = _settings(tmp_path, cmd="echo hello")

    rc = statusline.cmd_install(["--settings", str(p), "--yes"])

    assert rc != 0
    assert json.loads(p.read_text())["statusLine"]["command"] == "echo hello"


def test_status_reports_not_installed(tmp_path, capsys):
    p = _settings(tmp_path)

    statusline.cmd_status(["--settings", str(p)])

    assert "not installed" in capsys.readouterr().out.lower()


DECOY_COMMAND = "DECOY-SHOULD-NOT-BE-TOUCHED"


def _settings_with_nested_decoy(tmp_path, *, decoy_before: bool):
    real_status_line = {"statusLine": {"type": "command", "command": BASE_CMD}}
    decoy = {"someExtension": {"statusLine": {"command": DECOY_COMMAND}}}
    body = {**decoy, **real_status_line} if decoy_before else {**real_status_line, **decoy}
    body["model"] = "opus"
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return p


def test_install_targets_top_level_statusline_not_a_decoy_before_it(tmp_path):
    p = _settings_with_nested_decoy(tmp_path, decoy_before=True)

    rc = statusline.cmd_install(["--settings", str(p), "--yes"])

    raw = p.read_text()
    body = json.loads(raw)
    assert rc == 0
    assert statusline.MARKER in body["statusLine"]["command"]
    assert BASE_CMD in body["statusLine"]["command"]
    assert body["someExtension"]["statusLine"]["command"] == DECOY_COMMAND
    assert raw.count(DECOY_COMMAND) == 1


def test_install_targets_top_level_statusline_not_a_decoy_after_it(tmp_path):
    p = _settings_with_nested_decoy(tmp_path, decoy_before=False)

    rc = statusline.cmd_install(["--settings", str(p), "--yes"])

    raw = p.read_text()
    body = json.loads(raw)
    assert rc == 0
    assert statusline.MARKER in body["statusLine"]["command"]
    assert BASE_CMD in body["statusLine"]["command"]
    assert body["someExtension"]["statusLine"]["command"] == DECOY_COMMAND
    assert raw.count(DECOY_COMMAND) == 1


BASE_SCRIPT = "#!/bin/bash\ninput=$(cat)\n\necho \"$input\" | jq -r '.model.display_name'\n"


def _script(tmp_path, content=BASE_SCRIPT, name="statusline.sh"):
    s = tmp_path / name
    s.write_text(content, encoding="utf-8")
    return s


def _settings_for_script(tmp_path, script, extra=None):
    return _settings(tmp_path, cmd=str(script), extra=extra)


def test_install_splices_into_a_script_file(tmp_path):
    script = _script(tmp_path)
    original = script.read_text()
    p = _settings_for_script(tmp_path, script)

    rc = statusline.cmd_install(["--settings", str(p), "--yes"])

    assert rc == 0
    new_content = script.read_text()
    lines = new_content.splitlines()
    idx = next(i for i, line in enumerate(lines) if "input=$(cat)" in line)
    assert lines[idx + 1] == statusline.MARKER
    assert lines[idx + 3] == statusline.MARKER
    assert "jq -c" in lines[idx + 2]
    # inserted immediately after the capture line, nothing else touched
    assert new_content.startswith("".join(original.splitlines(keepends=True)[: idx + 1]))
    assert new_content.endswith("".join(original.splitlines(keepends=True)[idx + 1 :]))
    # the settings file itself is untouched — it still just names the script
    assert json.loads(p.read_text())["statusLine"]["command"] == str(script)


def test_uninstall_from_script_restores_the_original_exactly(tmp_path):
    script = _script(tmp_path)
    original = script.read_text()
    p = _settings_for_script(tmp_path, script)
    statusline.cmd_install(["--settings", str(p), "--yes"])

    rc = statusline.cmd_uninstall(["--settings", str(p), "--yes"])

    assert rc == 0
    assert script.read_text() == original


def test_script_install_writes_a_backup(tmp_path):
    script = _script(tmp_path)
    original = script.read_text()
    p = _settings_for_script(tmp_path, script)

    statusline.cmd_install(["--settings", str(p), "--yes"])

    backup = script.with_name(script.name + ".bak")
    assert backup.exists()
    assert backup.read_text() == original


def test_script_install_is_idempotent(tmp_path):
    script = _script(tmp_path)
    p = _settings_for_script(tmp_path, script)
    statusline.cmd_install(["--settings", str(p), "--yes"])
    once = script.read_text()

    statusline.cmd_install(["--settings", str(p), "--yes"])

    assert script.read_text() == once


def test_script_install_refuses_when_the_referenced_file_is_missing(tmp_path, capsys):
    missing = tmp_path / "no-such-script.sh"
    p = _settings_for_script(tmp_path, missing)

    rc = statusline.cmd_install(["--settings", str(p), "--yes"])

    assert rc != 0
    assert not missing.exists()
    assert "jq -c" in capsys.readouterr().out


def test_script_install_refuses_when_the_script_has_no_stdin_capture(tmp_path, capsys):
    script = _script(tmp_path, content="#!/bin/bash\necho hello\n")
    original = script.read_text()
    p = _settings_for_script(tmp_path, script)

    rc = statusline.cmd_install(["--settings", str(p), "--yes"])

    assert rc != 0
    assert script.read_text() == original
    assert "jq -c" in capsys.readouterr().out


def test_status_reports_script_target_and_snippet_state(tmp_path, capsys):
    script = _script(tmp_path)
    p = _settings_for_script(tmp_path, script)

    statusline.cmd_status(["--settings", str(p)])
    out = capsys.readouterr().out.lower()
    assert "script" in out
    assert str(script).lower() in out
    assert "not installed" in out

    statusline.cmd_install(["--settings", str(p), "--yes"])

    statusline.cmd_status(["--settings", str(p)])
    out = capsys.readouterr().out.lower()
    assert "installed" in out


def test_status_reports_inline_target(tmp_path, capsys):
    p = _settings(tmp_path)

    statusline.cmd_status(["--settings", str(p)])

    out = capsys.readouterr().out.lower()
    assert "inline" in out


def test_install_refuses_on_duplicate_top_level_statusline_key(tmp_path, capsys):
    p = tmp_path / "settings.json"
    raw = (
        '{"statusLine": {"type": "command", "command": "DECOY"}, '
        f'"statusLine": {{"type": "command", "command": {json.dumps(BASE_CMD)}}}}}'
    )
    p.write_text(raw, encoding="utf-8")

    rc = statusline.cmd_install(["--settings", str(p), "--yes"])

    assert rc != 0
    assert p.read_text() == raw
    assert "duplicate" in capsys.readouterr().err.lower()


def test_install_refuses_on_non_string_command(tmp_path, capsys):
    p = _settings(tmp_path, extra=None)
    body = json.loads(p.read_text())
    body["statusLine"]["command"] = 123
    original = json.dumps(body, indent=2)
    p.write_text(original, encoding="utf-8")

    rc = statusline.cmd_install(["--settings", str(p), "--yes"])

    assert rc != 0
    assert p.read_text() == original
    assert "not a string" in capsys.readouterr().err.lower()


def test_status_refuses_on_non_string_command(tmp_path, capsys):
    p = _settings(tmp_path)
    body = json.loads(p.read_text())
    body["statusLine"]["command"] = 123
    p.write_text(json.dumps(body, indent=2), encoding="utf-8")

    rc = statusline.cmd_status(["--settings", str(p)])

    assert rc != 0
    assert "not a string" in capsys.readouterr().err.lower()


def test_script_install_preserves_the_scripts_mode(tmp_path):
    script = _script(tmp_path)
    script.chmod(0o755)
    p = _settings_for_script(tmp_path, script)

    rc = statusline.cmd_install(["--settings", str(p), "--yes"])

    assert rc == 0
    assert stat.S_IMODE(script.stat().st_mode) == 0o755


def test_install_writes_through_a_symlinked_settings_file(tmp_path):
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = _settings(dotfiles)
    link = tmp_path / "settings.json"
    link.symlink_to(real)

    rc = statusline.cmd_install(["--settings", str(link), "--yes"])

    assert rc == 0
    assert link.is_symlink()
    assert "ringleader-capture" in json.loads(real.read_text())["statusLine"]["command"]
