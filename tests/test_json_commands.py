import dataclasses
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ringleader import cli, contract, contract_io, refresh, registry
from scripts.tui_fixture import Fixture, build_fixture

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)


def gui_world(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    threads: int,
    transcripts: bool,
    presence: contract.PresenceView | None = None,
) -> Fixture:
    fixture = build_fixture(root, threads=threads, now=NOW_DT, transcripts=transcripts)
    for name, value in fixture.env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setattr(contract_io, "_now_dt", lambda: NOW_DT)
    view = presence if presence is not None else contract.PresenceView(frozenset(), None)
    monkeypatch.setattr(contract_io, "_presence", lambda: view)
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


def _main(capsys, *argv):
    code = cli.main([*argv])
    return code, json.loads(capsys.readouterr().out)


def _list(fixture, capsys, *flags):
    code, doc = _main(capsys, "inbox", "--json", *flags, *_reg_arg(fixture))
    assert code == 0
    return doc


def _tail(fixture, capsys, *args):
    code, doc = _main(capsys, "data", "export", *args, "--json", *_reg_arg(fixture))
    assert code == 0
    return doc


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


def test_inbox_json_prints_one_list_document_without_a_scan(world, capsys):
    doc = _list(world, capsys)
    assert (doc["version"], doc["kind"]) == (1, "list")
    assert doc["registry"] == str(world.registry_path.resolve())
    assert doc["scan"] is None
    assert len(doc["sessions"]) >= 1


def test_inbox_json_marks_an_expired_snooze_as_woke(world, capsys):
    target = {}

    def stamp(data):
        thread = next(t for t in data["threads"] if _plain(t))
        thread["snoozedAt"] = (NOW_DT - timedelta(days=9)).isoformat()
        thread["snoozedUntil"] = (NOW_DT - timedelta(days=1)).isoformat()
        target["id"] = registry.qualified_id(thread)

    _edit_registry(world, stamp)
    rows = {row["id"]: row for row in _list(world, capsys)["sessions"]}
    assert rows[target["id"]]["woke"] is True


def test_inbox_json_scan_lists_sessions_found_on_disk_in_the_same_call(world, capsys):
    _edit_registry(world, lambda data: data.update(threads=[]))
    doc = _list(world, capsys, "--scan")
    assert doc["kind"] == "list"
    assert doc["scan"]["scanned_at"] == NOW_DT.isoformat()
    assert doc["scan"]["added"] == len(registry.load_registry(world.registry_path)["threads"])
    assert doc["scan"]["added"] >= 1
    assert doc["scan"]["removed"] == 0
    assert len(doc["sessions"]) >= 1


def test_inbox_json_scan_reports_unavailable_sources_sorted(world, capsys, monkeypatch):
    real = refresh.refresh_registry

    def with_unavailable(*args, **kwargs):
        return dataclasses.replace(
            real(*args, **kwargs), unavailable_sources=frozenset({"pi", "codex"})
        )

    monkeypatch.setattr(refresh, "refresh_registry", with_unavailable)
    doc = _list(world, capsys, "--scan")
    assert doc["scan"]["unavailable_sources"] == ["codex", "pi"]


def test_a_registry_conflict_during_scan_is_an_error_document(world, capsys, monkeypatch):
    def conflict(*_args, **_kwargs):
        raise registry.RegistryConflict("registry changed underneath the scan")

    monkeypatch.setattr(refresh, "refresh_registry", conflict)
    code, doc = _main(capsys, "inbox", "--json", "--scan", *_reg_arg(world))
    assert code == 1
    assert doc == {"version": 1, "kind": "error", "message": "registry changed underneath the scan"}


def test_inbox_json_on_a_corrupt_registry_prints_an_error_document(world, tmp_path, capsys):
    bad = tmp_path / "corrupt.json"
    bad.write_text("{not json", encoding="utf-8")
    code, doc = _main(capsys, "inbox", "--json", "--registry", str(bad))
    assert code == 1
    assert (doc["version"], doc["kind"]) == (1, "error")
    assert doc["message"]


def test_inbox_json_works_without_textual(world, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "textual", None)
    assert cli.main(["inbox", *_reg_arg(world)]) == 2
    assert "needs Textual" in capsys.readouterr().out
    assert _list(world, capsys)["kind"] == "list"


def test_inbox_json_survives_a_malformed_lastprompt_and_roundup_stamp(world, capsys):
    def corrupt(data):
        data["threads"][0]["lastPrompt"] = 123
        data["lastRoundupAt"] = "garbage"

    _edit_registry(world, corrupt)
    doc = _list(world, capsys)
    assert doc["roundup"]["stale"] is True
    assert doc["roundup"]["age_human"] is None


def test_rl_inbox_json_runs_as_a_process(world):
    result = subprocess.run(
        [sys.executable, "-m", "ringleader", "inbox", "--json", *_reg_arg(world)],
        env=subprocess_env(world),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["kind"] == "list"


def test_export_json_returns_items_in_request_order(world, capsys):
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
        assert item["max_chars"] == contract.TAIL_DEFAULT_CHARS
        assert (item["mtime"], item["file_fingerprint"]) == (row["mtime"], row["file_fingerprint"])
    assert missing["unavailable"] == "no longer in the registry"
    assert (missing["text"], missing["mtime"]) == ("", None)


def test_export_json_truncates_to_max_chars(world, capsys):
    row = _list(world, capsys)["sessions"][0]
    record = {"type": "user", "message": {"role": "user", "content": "long " * 800}}
    with _transcript(world, row["id"]).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    item = _tail(world, capsys, row["id"], "--max-chars=1000")["items"][0]
    assert item["truncated"] is True
    assert len(item["text"]) == 1000


def test_export_json_replaces_lone_surrogates(world, capsysbinary):
    row = _list(world, capsysbinary)["sessions"][0]
    path = _transcript(world, row["id"])
    lines = path.read_text(encoding="utf-8").splitlines()
    last_user = json.loads(next(line for line in reversed(lines) if '"type": "user"' in line))
    last_user["message"]["content"] = "SURROGATE"
    broken = json.dumps(last_user).replace('"SURROGATE"', '"broken \\ud83d emoji"')
    with path.open("a", encoding="utf-8") as fh:
        fh.write(broken + "\n")
    assert cli.main(["data", "export", row["id"], "--json", *_reg_arg(world)]) == 0
    raw = capsysbinary.readouterr().out
    doc = json.loads(raw.decode("utf-8", errors="strict"))
    assert "broken ? emoji" in doc["items"][0]["text"]


def test_export_json_isolates_a_failing_item_from_its_neighbours(world, capsys):
    good, gone, odd = _list(world, capsys)["sessions"][:3]
    _transcript(world, gone["id"]).unlink()
    with _transcript(world, odd["id"]).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": "plain string"}) + "\n")
    items = _tail(world, capsys, good["id"], gone["id"], odd["id"])["items"]
    assert "## USER" in items[0]["text"]
    assert items[1]["unavailable"] == "transcript not found"
    assert items[2]["unavailable"] == "could not read the transcript (AttributeError)"


def test_export_json_reports_an_ambiguous_id_as_an_unavailable_item(world, capsys):
    shared = {}

    def duplicate(data):
        thread = next(t for t in data["threads"] if t.get("source") == "claude-code")
        data["threads"].append({**thread, "source": "codex"})
        shared["id"] = thread["sessionId"]

    _edit_registry(world, duplicate)
    (item,) = _tail(world, capsys, shared["id"])["items"]
    assert (item["id"], item["unavailable"]) == (shared["id"], "ambiguous session id")


def test_export_json_on_a_corrupt_registry_prints_an_error_document(world, tmp_path, capsys):
    bad = tmp_path / "corrupt.json"
    bad.write_text("{not json", encoding="utf-8")
    code, doc = _main(capsys, "data", "export", "claude-code:a", "--json", "--registry", str(bad))
    assert code == 1
    assert (doc["version"], doc["kind"]) == (1, "error")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["inbox", "--scan"], "--scan needs --json"),
        (["inbox", "--json", "--read-only"], "--json cannot be combined with --read-only"),
        (["data", "export", "claude-code:a", "claude-code:b"], "more than one ID needs --json"),
        (["data", "export", "claude-code:a", "--max-chars=5000"], "--max-chars needs --json"),
        (
            ["data", "export", "claude-code:a", "--json", "--out=x.md"],
            "--out cannot be combined with --json",
        ),
        (["data", "export", "claude-code:a", "--json", "--max-chars=999"], "between 1000"),
        (["data", "export", "claude-code:a", "--json", "--max-chars=2000001"], "between 1000"),
        (
            ["data", "export", *[f"claude-code:{k}" for k in range(17)], "--json"],
            "at most 16 ids",
        ),
        (
            ["data", "export", "claude-code:a", "claude-code:a", "--json"],
            "each id may appear only once",
        ),
    ],
    ids=[
        "scan-without-json",
        "json-with-read-only",
        "two-ids-without-json",
        "max-chars-without-json",
        "out-with-json",
        "max-chars-too-small",
        "max-chars-too-large",
        "seventeen-ids",
        "repeated-id",
    ],
)
def test_json_argument_rules_exit_2_without_stdout(world, capsys, argv, message):
    with pytest.raises(SystemExit) as raised:
        cli.main([*argv, *_reg_arg(world)])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert "usage: rl " in captured.err


def test_plain_export_still_prints_the_conversation(world, capsys):
    row = _list(world, capsys)["sessions"][0]
    assert cli.main(["data", "export", row["id"], *_reg_arg(world)]) == 0
    assert "## USER" in capsys.readouterr().out


def test_plain_export_of_an_unknown_session_keeps_its_message(world, capsys):
    assert cli.main(["data", "export", "claude-code:does-not-exist", *_reg_arg(world)]) == 1
    assert capsys.readouterr().err == "rl-export: unknown session claude-code:does-not-exist\n"
