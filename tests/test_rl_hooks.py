import ast
import json
from pathlib import Path

from ringleader import paths
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
    ours = next(h for group in entries for h in group["hooks"] if "session_end.sh" in h["command"])
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


def _droid_dir(tmp_path: Path) -> Path:
    """Droid's config dir under the suite's `FACTORY_HOME_OVERRIDE` (see conftest)."""
    return tmp_path / "_factory" / ".factory"


def test_install_registers_a_droid_hook_keyed_by_event_name(tmp_path, monkeypatch):
    """Droid's standalone hooks.json is keyed by event name directly, unlike the
    `hooks` wrapper Claude's settings.json and Codex's hooks.json use."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _droid_dir(tmp_path).mkdir(parents=True)
    hooks_json = _droid_dir(tmp_path) / "hooks.json"

    assert hooks.cmd_install(["--yes"]) == 0

    data = json.loads(hooks_json.read_text())
    assert list(data) == [hooks.HOOK_EVENT]
    commands = [h["command"] for g in data[hooks.HOOK_EVENT] for h in g["hooks"]]
    assert any("session_end.sh" in c for c in commands)


def test_install_is_idempotent_for_droid(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _droid_dir(tmp_path).mkdir(parents=True)
    hooks_json = _droid_dir(tmp_path) / "hooks.json"
    hooks.cmd_install(["--yes"])
    once = hooks_json.read_text()

    assert hooks.cmd_install(["--yes"]) == 0

    assert hooks_json.read_text() == once


def test_install_uses_the_droid_session_end_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _droid_dir(tmp_path).mkdir(parents=True)
    hooks_json = _droid_dir(tmp_path) / "hooks.json"

    assert hooks.cmd_install(["--yes"]) == 0

    groups = json.loads(hooks_json.read_text())[hooks.HOOK_EVENT]
    ours = next(h for g in groups for h in g["hooks"] if "session_end.sh" in h["command"])
    assert ours["timeout"] == 5


def test_update_repairs_a_droid_timeout_and_keeps_a_foreign_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _droid_dir(tmp_path).mkdir(parents=True)
    hooks_json = _droid_dir(tmp_path) / "hooks.json"
    hooks_json.write_text(
        json.dumps(
            {
                hooks.HOOK_EVENT: [
                    {"hooks": [{"type": "command", "command": "/other/thing.sh"}]},
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": str(hooks._session_end_script()),
                                "timeout": 60,
                            }
                        ]
                    },
                ]
            }
        )
    )

    assert hooks.cmd_update(["--yes"]) == 0

    groups = json.loads(hooks_json.read_text())[hooks.HOOK_EVENT]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert "/other/thing.sh" in commands
    ours = next(h for g in groups for h in g["hooks"] if "session_end.sh" in h["command"])
    assert ours["timeout"] == 5


def test_uninstall_removes_only_our_droid_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _droid_dir(tmp_path).mkdir(parents=True)
    hooks_json = _droid_dir(tmp_path) / "hooks.json"
    hooks_json.write_text(
        json.dumps(
            {hooks.HOOK_EVENT: [{"hooks": [{"type": "command", "command": "/other/thing.sh"}]}]}
        )
    )
    hooks.cmd_install(["--yes"])

    assert hooks.cmd_uninstall(["--yes"]) == 0

    data = json.loads(hooks_json.read_text())
    assert list(data) == [hooks.HOOK_EVENT]
    commands = [h["command"] for g in data[hooks.HOOK_EVENT] for h in g["hooks"]]
    assert commands == ["/other/thing.sh"]


def test_status_reports_an_installed_droid_hook(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    _droid_dir(tmp_path).mkdir(parents=True)
    hooks.cmd_install(["--yes"])
    capsys.readouterr()

    hooks.cmd_status([])

    out = capsys.readouterr().out
    assert f"droid: {_droid_dir(tmp_path) / 'hooks.json'}" in out
    assert "status: installed" in out


def test_droid_is_offered_only_once_its_home_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()

    assert [a.label for a in hooks._json_agents(Path.home())] == ["claude"]

    _droid_dir(tmp_path).mkdir(parents=True)
    assert [a.label for a in hooks._json_agents(Path.home())] == ["claude", "droid"]


def test_install_writes_into_a_legacy_droid_hooks_file_rather_than_shadowing_it(
    tmp_path, monkeypatch
):
    """Droid reads `hooks.json` or the legacy `hooks/hooks.json`, never both, so
    creating the former would disable every hook in the latter."""
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = _droid_dir(tmp_path) / "hooks" / "hooks.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            {hooks.HOOK_EVENT: [{"hooks": [{"type": "command", "command": "/other/thing.sh"}]}]}
        )
    )

    assert hooks.cmd_install(["--yes"]) == 0

    assert [p.name for p in _droid_dir(tmp_path).iterdir()] == ["hooks"]
    commands = [
        h["command"] for g in json.loads(legacy.read_text())[hooks.HOOK_EVENT] for h in g["hooks"]
    ]
    assert "/other/thing.sh" in commands
    assert any("session_end.sh" in c for c in commands)


def test_uninstall_deletes_a_droid_hooks_file_that_held_only_our_entry(tmp_path, monkeypatch):
    """An empty `hooks.json` still wins over the legacy `hooks/hooks.json`, so leaving
    one behind would keep shadowing it forever."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _droid_dir(tmp_path).mkdir(parents=True)
    hooks.cmd_install(["--yes"])

    assert hooks.cmd_uninstall(["--yes"]) == 0

    assert [p.name for p in _droid_dir(tmp_path).iterdir()] == []


def test_uninstall_keeps_an_emptied_droid_hooks_file_that_shadows_a_legacy_one(
    tmp_path, monkeypatch
):
    """Deleting the standalone file would hand the legacy one back to droid, re-arming
    hooks the user shadowed on purpose. It is kept empty, whoever created it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = _droid_dir(tmp_path) / "hooks" / "hooks.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            {hooks.HOOK_EVENT: [{"hooks": [{"type": "command", "command": "/usr/bin/old.sh"}]}]}
        )
    )
    standalone = _droid_dir(tmp_path) / "hooks.json"
    standalone.write_text("{}")
    hooks.cmd_install(["--yes"])

    assert hooks.cmd_uninstall(["--yes"]) == 0

    assert json.loads(standalone.read_text()) == {}
    assert json.loads(legacy.read_text())[hooks.HOOK_EVENT][0]["hooks"][0]["command"] == (
        "/usr/bin/old.sh"
    )


def test_uninstall_empties_but_keeps_a_legacy_droid_hooks_file(tmp_path, monkeypatch):
    """The legacy file is the user's, not ours — emptying it shadows nothing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = _droid_dir(tmp_path) / "hooks" / "hooks.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}")
    hooks.cmd_install(["--yes"])

    assert hooks.cmd_uninstall(["--yes"]) == 0

    assert json.loads(legacy.read_text()) == {}


def test_install_warns_that_droid_settings_lose_their_own_session_end(
    tmp_path, monkeypatch, capsys
):
    """Droid merges its hooks file over settings.json per event, hooks file winning."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _droid_dir(tmp_path).mkdir(parents=True)
    settings = _droid_dir(tmp_path) / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    hooks.HOOK_EVENT: [
                        {"hooks": [{"type": "command", "command": "/theirs/thing.sh"}]}
                    ]
                }
            }
        )
    )

    assert hooks.cmd_install(["--yes"]) == 0

    err = capsys.readouterr().err
    assert f"warning: {settings} declares a {hooks.HOOK_EVENT} hook" in err
    assert "droid will ignore" in err


def test_install_keeps_foreign_keys_in_a_droid_hooks_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _droid_dir(tmp_path).mkdir(parents=True)
    hooks_json = _droid_dir(tmp_path) / "hooks.json"
    foreign = {
        "PreToolUse": [{"hooks": [{"type": "command", "command": "/other/pre.sh"}]}],
        "someUnknownKey": {"a": 1},
    }
    hooks_json.write_text(json.dumps(foreign))

    assert hooks.cmd_install(["--yes"]) == 0
    assert list(json.loads(hooks_json.read_text())) == [
        "PreToolUse",
        "someUnknownKey",
        hooks.HOOK_EVENT,
    ]

    assert hooks.cmd_uninstall(["--yes"]) == 0
    assert json.loads(hooks_json.read_text()) == foreign


def test_a_droid_hooks_file_that_is_not_an_object_is_left_untouched(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    _droid_dir(tmp_path).mkdir(parents=True)
    hooks_json = _droid_dir(tmp_path) / "hooks.json"
    hooks_json.write_text("[1, 2, 3]")

    assert hooks.cmd_install(["--yes"]) == 0

    assert hooks_json.read_text() == "[1, 2, 3]"
    assert "top-level JSON is not an object" in capsys.readouterr().err


def test_a_droid_session_end_that_is_not_an_array_is_left_untouched(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    _droid_dir(tmp_path).mkdir(parents=True)
    hooks_json = _droid_dir(tmp_path) / "hooks.json"
    original = json.dumps({hooks.HOOK_EVENT: "nope"})
    hooks_json.write_text(original)

    assert hooks.cmd_install(["--yes"]) == 1

    assert hooks_json.read_text() == original
    assert f"'{hooks.HOOK_EVENT}' is not an array" in capsys.readouterr().err


def test_droid_hooks_land_under_the_real_home_without_an_override(tmp_path, monkeypatch):
    """Every user runs the un-overridden branch of `paths.factory_home()`; the suite
    sets FACTORY_HOME_OVERRIDE for every other test (see conftest)."""
    monkeypatch.delenv("FACTORY_HOME_OVERRIDE")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".factory").mkdir()

    assert [(a.label, a.path) for a in hooks._json_agents(Path.home())] == [
        ("droid", tmp_path / ".factory" / "hooks.json")
    ]


def test_the_droid_config_root_is_read_from_the_environment_not_a_session_source():
    """`paths` is a leaf utility: reading FACTORY_HOME_OVERRIDE itself keeps
    `factory_home()` off droid's transcript store, which is free to move."""
    tree = ast.parse(Path(paths.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert sorted(imported) == ["__future__", "os", "pathlib", "sys"]


def _cursor_cli(tmp_path: Path) -> Path:
    """Mark the Cursor CLI installed where conftest points CURSOR_CONFIG_DIR; return the
    hooks.json Cursor reads from $HOME."""
    config = tmp_path / "_cursor"
    config.mkdir(exist_ok=True)
    (config / "cli-config.json").write_text("{}")
    return tmp_path / ".cursor" / "hooks.json"


def test_install_registers_a_bare_cursor_session_end_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = _cursor_cli(tmp_path)

    assert hooks.cmd_install(["--yes"]) == 0

    assert json.loads(target.read_text()) == {
        "hooks": {
            "sessionEnd": [
                {"type": "command", "command": str(hooks._session_end_script()), "timeout": 5}
            ]
        },
        "version": 1,
    }


def test_install_keeps_cursors_other_hooks_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = _cursor_cli(tmp_path)
    target.parent.mkdir()
    foreign = {"type": "command", "command": "/opt/other ingest", "timeout": 5}
    target.write_text(
        json.dumps({"version": 1, "hooks": {"sessionEnd": [foreign], "stop": [foreign]}})
    )

    assert hooks.cmd_install(["--yes"]) == 0
    assert hooks.cmd_install(["--yes"]) == 0

    data = json.loads(target.read_text())
    assert [h["command"] for h in data["hooks"]["sessionEnd"]] == [
        "/opt/other ingest",
        str(hooks._session_end_script()),
    ]
    assert data["hooks"]["stop"] == [foreign]


def test_uninstall_removes_only_our_cursor_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = _cursor_cli(tmp_path)
    target.parent.mkdir()
    foreign = {"type": "command", "command": "/opt/other ingest", "timeout": 5}
    target.write_text(json.dumps({"version": 1, "hooks": {"sessionEnd": [foreign]}}))
    assert hooks.cmd_install(["--yes"]) == 0

    assert hooks.cmd_uninstall(["--yes"]) == 0

    assert json.loads(target.read_text()) == {"version": 1, "hooks": {"sessionEnd": [foreign]}}


def test_a_cursor_dir_without_the_cli_gets_no_hook(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".cursor").mkdir()

    assert hooks.cmd_install(["--yes"]) == 0

    assert "no supported agents detected" in capsys.readouterr().out
