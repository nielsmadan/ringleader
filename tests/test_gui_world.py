import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ringleader import contract, harvesting, presence, registry
from scripts import gui_world

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)


def _run(script: Path, *args: str, stdin: str = "", env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        [str(script), *args], input=stdin, capture_output=True, text=True, check=True, env=env
    )
    return done.stdout


def test_world_registry_config_and_marker(tmp_path):
    root = tmp_path / "w"
    world = gui_world.build_world(root, now=NOW_DT, threads=40)
    reg = registry.load_registry(world.registry_path)
    config = json.loads((root / "xdg-data" / "ringleader" / "config.json").read_text())
    assert (root / ".gui-world").is_file()
    assert len(reg["threads"]) >= 1
    assert config["ai_command_prefix"] == [str(root / "bin" / "fake-harvest")]
    assert config["session_filters"] == ["zz-hidden*"]
    assert world.env["RINGLEADER_CLAUDE_SETTINGS"] == str(root / "claude-settings.json")


def test_fake_hooklinesinker_reports_one_running_visible_session(tmp_path):
    root = tmp_path / "w"
    world = gui_world.build_world(root, now=NOW_DT, threads=40)
    binary = root / "xdg-data" / "hooklinesinker" / "bin" / "hooklinesinker"
    doc = json.loads(_run(binary, "sessions", "--json"))
    running = doc["sessions"][0]
    reg = registry.load_registry(world.registry_path)
    thread = next(t for t in reg["threads"] if t["sessionId"] == running["session"]["id"])
    assert running["running"] is True
    assert thread.get("filteredBy") is None
    assert registry.qualified_id(thread) in {r["id"] for r in _list_rows(reg)}
    assert _run(binary, "doctor", "--json").strip() == "{}"
    live = presence.PresenceClient(executable=binary).require_running_session_ids()
    assert live == frozenset({("claude", running["session"]["id"])})


def _list_rows(reg):
    return contract.list_payload(
        reg,
        NOW_DT.isoformat(),
        registry_path="x",
        presence=contract.PresenceView(None, None),
        stale_hours=24,
        scan=None,
    )["sessions"]


def test_fake_harvest_is_empty_only_for_proj_00(tmp_path):
    root = tmp_path / "w"
    gui_world.build_world(root, now=NOW_DT, threads=40)
    script = root / "bin" / "fake-harvest"
    fast = {"GUI_WORLD_HARVEST_DELAY": "0", "PATH": "/usr/bin:/bin"}
    empty = json.loads(
        _run(script, stdin=harvesting.build_prompt({"project": "/x/proj-00"}, "t"), env=fast)
    )
    full = json.loads(
        _run(script, stdin=harvesting.build_prompt({"project": "/x/proj-01"}, "t"), env=fast)
    )
    assert empty["structured_output"]["summary"] == ""
    assert full["structured_output"]["summary"] != ""
    assert len(full["structured_output"]["gotchas"]) == 1


def test_claude_stub_exits_97(tmp_path):
    root = tmp_path / "w"
    gui_world.build_world(root, now=NOW_DT, threads=40)
    done = subprocess.run(
        [str(root / "bin" / "claude")], capture_output=True, text=True, check=False
    )
    assert (done.returncode, "must not run here" in done.stderr) == (97, True)


def test_exports_are_rooted_and_end_with_path(tmp_path, capsys):
    root = tmp_path / "w"
    assert gui_world.main(["--root", str(root), "--threads", "40", "--exports"]) == 0
    lines = capsys.readouterr().out.splitlines()
    data_home = next(line for line in lines if line.startswith("export XDG_DATA_HOME="))
    assert str(root.resolve()) in data_home
    assert lines[-1].startswith("export PATH=")
    assert lines[-1].endswith(':"$PATH"')


def test_refuses_to_build_in_agent_state_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    target = tmp_path / "home" / ".claude" / "x"
    assert gui_world.main(["--root", str(target)]) == 1
    assert not target.exists()


def test_refuses_an_unmarked_directory_and_keeps_its_files(tmp_path):
    root = tmp_path / "precious"
    root.mkdir()
    (root / "keep.txt").write_text("mine")
    assert gui_world.main(["--root", str(root)]) == 1
    assert (root / "keep.txt").read_text() == "mine"


def test_rebuilds_a_root_left_by_a_crashed_build(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    (root / ".gui-world").write_text("")
    assert gui_world.main(["--root", str(root), "--threads", "40"]) == 0
    assert (root / "xdg-data" / "ringleader" / "registry.json").is_file()


def test_refuses_an_empty_unmarked_directory(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    assert gui_world.main(["--root", str(root)]) == 1
    assert root.is_dir()
    assert not (root / ".gui-world").exists()


def test_refuses_a_root_that_is_a_file(tmp_path):
    root = tmp_path / "file"
    root.write_text("x")
    assert gui_world.main(["--root", str(root)]) == 1
    assert root.read_text() == "x"


def test_too_few_threads_fails_before_touching_the_root(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    (root / ".gui-world").write_text("")
    (root / "old.txt").write_text("old")
    assert gui_world.main(["--root", str(root), "--threads", "1"]) == 1
    assert (root / "old.txt").read_text() == "old"
