import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ringleader import cli, gui_cli, registry
from ringleader import gui as gui_api
from scripts.tui_fixture import Fixture, build_fixture

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)


def gui_world(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    threads: int,
    transcripts: bool,
    presence: gui_api.PresenceView | None = None,
) -> Fixture:
    fixture = build_fixture(root, threads=threads, now=NOW_DT, transcripts=transcripts)
    for name, value in fixture.env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setattr(gui_cli, "_now_dt", lambda: NOW_DT)
    view = presence if presence is not None else gui_api.PresenceView(frozenset(), None)
    monkeypatch.setattr(gui_cli, "_presence", lambda: view)
    return fixture


def subprocess_env(fixture: Fixture) -> dict[str, str]:
    env = {**os.environ, **fixture.env}
    env.pop("OPENCODE_DB", None)
    return env


@pytest.fixture
def world(monkeypatch, tmp_path):
    return gui_world(monkeypatch, tmp_path, threads=40, transcripts=True)


def _reg_arg(fixture):
    return ["--registry", str(fixture.registry_path)]


def _list(fixture, capsys):
    assert gui_cli.cmd_gui_list(_reg_arg(fixture)) == 0
    return json.loads(capsys.readouterr().out)


def _tail(fixture, capsys, *args):
    assert gui_cli.cmd_gui_tail([*args, *_reg_arg(fixture)]) == 0
    return json.loads(capsys.readouterr().out)


def _edit_registry(fixture, edit):
    data = json.loads(fixture.registry_path.read_text(encoding="utf-8"))
    edit(data)
    fixture.registry_path.write_text(json.dumps(data), encoding="utf-8")


def _transcript(fixture, row_id):
    reg = registry.load_registry(fixture.registry_path)
    thread = registry.find_session(reg, row_id)
    assert thread is not None
    return Path(thread["file"])


def _plain(thread):
    return not any(thread.get(k) for k in ("snoozedAt", "decision", "filteredBy", "finishOnExit"))


def test_list_prints_one_list_document(world, capsys):
    doc = _list(world, capsys)
    assert (doc["version"], doc["kind"]) == (1, "list")
    assert doc["registry"] == str(world.registry_path.resolve())
    assert len(doc["sessions"]) >= 1


def test_list_marks_an_expired_snooze_as_woke(world, capsys):
    target = {}

    def stamp(data):
        thread = next(t for t in data["threads"] if _plain(t))
        thread["snoozedAt"] = (NOW_DT - timedelta(days=9)).isoformat()
        thread["snoozedUntil"] = (NOW_DT - timedelta(days=1)).isoformat()
        target["id"] = registry.qualified_id(thread)

    _edit_registry(world, stamp)
    rows = {row["id"]: row for row in _list(world, capsys)["sessions"]}
    assert rows[target["id"]]["woke"] is True


def test_tail_returns_items_in_request_order(world, capsys):
    first, second = _list(world, capsys)["sessions"][:2]
    doc = _tail(world, capsys, first["id"], second["id"], "claude-code:does-not-exist")
    assert doc["kind"] == "tails"
    found, other, missing = doc["items"]
    assert [found["id"], other["id"], missing["id"]] == [
        first["id"],
        second["id"],
        "claude-code:does-not-exist",
    ]
    for item, row in ((found, first), (other, second)):
        assert "## USER" in item["text"]
        assert item["unavailable"] is None
        assert (item["mtime"], item["file_fingerprint"]) == (row["mtime"], row["file_fingerprint"])
    assert missing["unavailable"] == "no longer in the registry"
    assert (missing["text"], missing["mtime"]) == ("", None)


def test_tail_truncates_to_max_chars(world, capsys):
    row = _list(world, capsys)["sessions"][0]
    record = {"type": "user", "message": {"role": "user", "content": "long " * 800}}
    with _transcript(world, row["id"]).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    item = _tail(world, capsys, row["id"], "--max-chars=1000")["items"][0]
    assert item["truncated"] is True
    assert len(item["text"]) == 1000


def test_tail_replaces_lone_surrogates(world, capsysbinary):
    row = _list(world, capsysbinary)["sessions"][0]
    path = _transcript(world, row["id"])
    lines = path.read_text(encoding="utf-8").splitlines()
    last_user = json.loads(next(line for line in reversed(lines) if '"type": "user"' in line))
    last_user["message"]["content"] = "SURROGATE"
    broken = json.dumps(last_user).replace('"SURROGATE"', '"broken \\ud83d emoji"')
    with path.open("a", encoding="utf-8") as fh:
        fh.write(broken + "\n")
    assert gui_cli.cmd_gui_tail([row["id"], *_reg_arg(world)]) == 0
    raw = capsysbinary.readouterr().out
    doc = json.loads(raw.decode("utf-8", errors="strict"))
    assert "broken ? emoji" in doc["items"][0]["text"]


@pytest.mark.parametrize(
    "args",
    [
        ["claude-code:a", "--max-chars=10"],
        [f"claude-code:{k}" for k in range(17)],
        ["claude-code:a", "claude-code:a"],
    ],
    ids=["max-chars-too-small", "seventeen-ids", "repeated-id"],
)
def test_tail_usage_errors_exit_2_without_stdout(world, capsys, args):
    with pytest.raises(SystemExit) as raised:
        gui_cli.cmd_gui_tail([*args, *_reg_arg(world)])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "rl gui tail" in captured.err


def test_scan_reports_added_sessions_as_one_document(world, capsys):
    _edit_registry(world, lambda data: data.update(threads=[]))
    assert gui_cli.cmd_gui_scan(_reg_arg(world)) == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["kind"] == "scan"
    assert doc["added"] >= 1
    assert doc["scanned_at"] == NOW_DT.isoformat()


def test_list_on_a_corrupt_registry_prints_an_error_document(world, tmp_path, capsys):
    bad = tmp_path / "corrupt.json"
    bad.write_text("{not json", encoding="utf-8")
    assert gui_cli.cmd_gui_list(["--registry", str(bad)]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert (doc["version"], doc["kind"]) == (1, "error")
    assert doc["message"]


def test_rl_gui_list_runs_as_a_process(world):
    result = subprocess.run(
        [sys.executable, "-m", "ringleader", "gui", "list", *_reg_arg(world)],
        env=subprocess_env(world),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["kind"] == "list"


def test_unknown_gui_subcommand_returns_2(capsys):
    assert cli.main(["gui", "nope"]) == 2
    assert "unknown command 'nope' in 'rl gui'" in capsys.readouterr().err


def test_bare_gui_lists_its_subcommands(capsys):
    assert cli.main(["gui"]) == 0
    assert "rl gui — subcommands: list  scan  tail" in capsys.readouterr().out


def test_the_command_table_lists_the_gui_dispatch_table():
    assert {name: entry.resolve() for name, entry in cli.TABLE["gui"].items()} == (
        gui_cli.GUI_COMMANDS
    )


def test_tail_isolates_a_failing_item_from_its_neighbours(world, capsys):
    good, gone, odd = _list(world, capsys)["sessions"][:3]
    _transcript(world, gone["id"]).unlink()
    with _transcript(world, odd["id"]).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": "plain string"}) + "\n")
    items = _tail(world, capsys, good["id"], gone["id"], odd["id"])["items"]
    assert "## USER" in items[0]["text"]
    assert items[1]["unavailable"] == "transcript not found"
    assert items[2]["unavailable"] == "could not read the transcript (AttributeError)"


def test_list_survives_a_malformed_lastprompt_and_roundup_stamp(world, capsys):
    def corrupt(data):
        data["threads"][0]["lastPrompt"] = 123
        data["lastRoundupAt"] = "garbage"

    _edit_registry(world, corrupt)
    doc = _list(world, capsys)
    assert doc["roundup"]["stale"] is True
    assert doc["roundup"]["age_human"] is None
