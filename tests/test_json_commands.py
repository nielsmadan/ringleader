import dataclasses
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ringleader import cli, contract, contract_io, guarded, presence, refresh, registry
from ringleader.models import ModelError, proc
from scripts.tui_fixture import Fixture, build_fixture

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)
REAL_PRESENCE = contract_io._presence


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


def test_inbox_json_on_a_directory_registry_prints_an_error_document(world, tmp_path, capsys):
    code, doc = _main(capsys, "inbox", "--json", "--registry", str(tmp_path))
    assert code == 1
    assert (doc["version"], doc["kind"]) == (1, "error")
    assert "Is a directory" in doc["message"]


@pytest.mark.parametrize(
    "error",
    [presence.PresenceUnavailable("hooklinesinker is not installed"), OSError("no socket")],
    ids=["unavailable", "oserror"],
)
def test_inbox_json_reports_unknown_presence_when_the_probe_fails(
    world, capsys, monkeypatch, error
):
    monkeypatch.setattr(contract_io, "_presence", REAL_PRESENCE)
    _live(monkeypatch, error)
    doc = _list(world, capsys)
    assert doc["presence"] == {"state": "unknown", "problem": str(error)}
    assert {row["running"] for row in doc["sessions"]} == {None}


@pytest.mark.parametrize("mtime", [12345, None])
def test_inbox_json_on_a_row_without_a_string_mtime_prints_an_error_document(world, capsys, mtime):
    target = {}

    def stamp(data):
        thread = next(t for t in data["threads"] if _plain(t))
        thread["mtime"] = mtime
        target["id"] = registry.qualified_id(thread)

    _edit_registry(world, stamp)
    code, doc = _main(capsys, "inbox", "--json", *_reg_arg(world))
    assert code == 1
    assert doc == {
        "version": 1,
        "kind": "error",
        "message": f"session {target['id']} has no string mtime",
    }


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
            "at most 16 IDs",
        ),
        (
            ["data", "export", "claude-code:a", "claude-code:a", "--json"],
            "each ID may appear only once",
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


def test_export_json_accepts_the_largest_batch_and_max_chars(world, capsys):
    ids = [f"claude-code:missing-{k}" for k in range(contract.TAIL_MAX_IDS)]
    doc = _tail(world, capsys, *ids, f"--max-chars={contract.TAIL_MAX_CHARS}")
    assert [item["id"] for item in doc["items"]] == ids
    assert {item["max_chars"] for item in doc["items"]} == {contract.TAIL_MAX_CHARS}


def test_plain_export_still_prints_the_conversation(world, capsys):
    row = _list(world, capsys)["sessions"][0]
    assert cli.main(["data", "export", row["id"], *_reg_arg(world)]) == 0
    assert "## USER" in capsys.readouterr().out


def test_plain_export_of_an_unknown_session_keeps_its_message(world, capsys):
    assert cli.main(["data", "export", "claude-code:does-not-exist", *_reg_arg(world)]) == 1
    assert capsys.readouterr().err == "rl-export: unknown session claude-code:does-not-exist\n"


class FakeModel:
    name = "fake"

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        self.calls += 1
        return self.answer


HARVEST_ANSWER = {"summary": "s", "gotchas": [], "decisions": [], "whatChanged": []}
EMPTY_ANSWER = {"summary": "", "gotchas": [], "decisions": [], "whatChanged": []}
VERBS = {
    "archive": ["session", "archive"],
    "delete": ["session", "delete"],
    "harvest": ["session", "harvest"],
    "snooze": ["session", "snooze"],
    "keep": ["session", "keep"],
    "keep_going": ["session", "mark", "reviewed"],
}
FUTURE_UNTIL = "--until=2026-10-20T09:00:00+02:00"


def _live(monkeypatch, ids):
    def require(_self):
        if isinstance(ids, Exception):
            raise ids
        return ids

    monkeypatch.setattr(presence.PresenceClient, "require_running_session_ids", require)


@pytest.fixture
def kills(monkeypatch):
    calls = []
    monkeypatch.setattr(proc, "kill_all_children", lambda: calls.append(1) or 0)
    return calls


@pytest.fixture
def decide_world(world, monkeypatch, kills):
    _live(monkeypatch, frozenset())
    return world


def _model(monkeypatch, model):
    monkeypatch.setattr(guarded, "_harvest_model", lambda: model)


def _target(fixture, capsys):
    rows = _list(fixture, capsys)["sessions"]
    return next(
        r
        for r in rows
        if r["source"] == "claude-code"
        and r["decision"] is None
        and r["snoozed_at"] is None
        and r["file_fingerprint"]
    )


def _verb(action, row_id, *flags):
    archive = ["--archive"] if action == "harvest" else []
    return [*VERBS[action], row_id, *archive, *flags]


def _guarded(fixture, capsys, action, row_id, *flags):
    code = cli.main(_verb(action, row_id, "--json", *flags, *_reg_arg(fixture)))
    out = capsys.readouterr().out
    assert code == 0
    doc = json.loads(out)
    assert (doc["version"], doc["kind"]) == (1, "outcome")
    return doc


def _outcome(action, row_id, status, message, **fields):
    return {
        "version": 1,
        "kind": "outcome",
        "action": action,
        "id": row_id,
        "status": status,
        "reason": None,
        "message": message,
        "wrote": False,
        "path": None,
        "warnings": [],
        **fields,
    }


def _refusal(action, row_id, reason, label=None):
    message = reason if label is None else f"{label}: {reason}"
    return _outcome(action, row_id, "refused", message, reason=reason)


def _stamped(row):
    return [f"--if-mtime={row['mtime']}", f"--if-fingerprint={row['file_fingerprint']}"]


def _registry_thread(fixture, row_id):
    return registry.find_session(registry.load_registry(fixture.registry_path), row_id)


def _archived_path(fixture, row_id):
    reg = registry.load_registry(fixture.registry_path)
    return registry.find_session(reg, row_id, bucket="archived")["archivePath"]


def test_guarded_archive_moves_the_transcript_and_drops_the_row(decide_world, capsys, kills):
    row = _target(decide_world, capsys)
    doc = _guarded(decide_world, capsys, "archive", row["id"], *_stamped(row))
    assert doc == _outcome(
        "archive",
        row["id"],
        "ok",
        f"archived {row['label']}",
        wrote=True,
        path=_archived_path(decide_world, row["id"]),
    )
    assert not Path(row["file"]).exists()
    assert row["id"] not in {r["id"] for r in _list(decide_world, capsys)["sessions"]}
    assert kills == [1]


DESTRUCTIVE = ("archive", "delete", "harvest")


@pytest.mark.parametrize("action", DESTRUCTIVE)
def test_a_guarded_destructive_verb_with_another_mtime_is_refused(
    decide_world, capsys, monkeypatch, action
):
    model = FakeModel(HARVEST_ANSWER)
    _model(monkeypatch, model)
    row = _target(decide_world, capsys)
    doc = _guarded(
        decide_world,
        capsys,
        action,
        row["id"],
        "--if-mtime=2020-01-01T00:00:00+00:00",
        f"--if-fingerprint={row['file_fingerprint']}",
    )
    label = None if action == "harvest" else row["label"]
    assert doc == _refusal(action, row["id"], "changed since you looked", label)
    assert Path(row["file"]).exists()
    assert model.calls == 0


@pytest.mark.parametrize(
    "fingerprint", [[], ["--if-fingerprint=opencode:abc"]], ids=["none", "bad"]
)
@pytest.mark.parametrize("action", ["archive", "delete", "harvest"])
def test_a_guarded_destructive_verb_without_a_fingerprint_is_refused(
    decide_world, capsys, monkeypatch, action, fingerprint
):
    monkeypatch.setattr(guarded, "_harvest_model", lambda: pytest.fail("model resolved"))
    row = _target(decide_world, capsys)
    doc = _guarded(
        decide_world, capsys, action, row["id"], f"--if-mtime={row['mtime']}", *fingerprint
    )
    assert doc == _refusal(action, row["id"], "changed since you looked: no fingerprint, rescan")
    assert Path(row["file"]).exists()


@pytest.mark.parametrize("action", DESTRUCTIVE)
def test_a_guarded_destructive_verb_on_a_running_session_is_refused(
    decide_world, capsys, monkeypatch, action
):
    model = FakeModel(HARVEST_ANSWER)
    _model(monkeypatch, model)
    row = _target(decide_world, capsys)
    _live(monkeypatch, frozenset({("claude", row["session_id"])}))
    doc = _guarded(decide_world, capsys, action, row["id"], *_stamped(row))
    reason = f"{row['label']} is running — finish it from inside with the ringleader-finish skill"
    assert doc == _refusal(action, row["id"], reason)
    assert Path(row["file"]).exists()
    assert model.calls == 0


@pytest.mark.parametrize("action", DESTRUCTIVE)
def test_a_guarded_destructive_verb_is_refused_when_presence_cannot_answer(
    decide_world, capsys, monkeypatch, action
):
    model = FakeModel(HARVEST_ANSWER)
    _model(monkeypatch, model)
    row = _target(decide_world, capsys)
    _live(monkeypatch, presence.PresenceUnavailable("hooklinesinker is not installed"))
    doc = _guarded(decide_world, capsys, action, row["id"], *_stamped(row))
    reason = (
        f"can't tell whether {row['label']} is running — "
        "install presence with rl data presence install"
    )
    assert doc == _refusal(action, row["id"], reason)
    assert Path(row["file"]).exists()
    assert model.calls == 0


@pytest.mark.parametrize("action", DESTRUCTIVE)
def test_a_guarded_destructive_verb_on_a_session_gone_from_the_registry_is_refused(
    decide_world, capsys, monkeypatch, action
):
    model = FakeModel(HARVEST_ANSWER)
    _model(monkeypatch, model)
    gone = "claude-code:does-not-exist"
    doc = _guarded(
        decide_world, capsys, action, gone, "--if-mtime=2026-10-01", "--if-fingerprint=1:2:3:4:5"
    )
    label = None if action == "harvest" else gone
    assert doc == _refusal(action, gone, "no longer in the registry", label)
    assert model.calls == 0


def test_guarded_delete_removes_the_transcript_and_the_thread(decide_world, capsys):
    row = _target(decide_world, capsys)
    doc = _guarded(decide_world, capsys, "delete", row["id"], *_stamped(row))
    assert doc == _outcome(
        "delete", row["id"], "ok", f"deleted {row['label']} — nothing harvested", wrote=True
    )
    assert not Path(row["file"]).exists()
    assert _registry_thread(decide_world, row["id"]) is None


@pytest.mark.parametrize("action", ["snooze", "keep", "keep_going"])
def test_a_stamp_only_guarded_verb_skips_revalidation_presence_and_fingerprint(
    decide_world, capsys, monkeypatch, action
):
    row = _target(decide_world, capsys)
    _live(monkeypatch, frozenset({("claude", row["session_id"])}))
    until = [FUTURE_UNTIL] if action == "snooze" else []
    stale = "--if-mtime=2020-01-01T00:00:00+00:00"
    doc = _guarded(
        decide_world, capsys, action, row["id"], stale, "--if-fingerprint=opencode:abc", *until
    )
    assert (doc["status"], doc["wrote"]) == ("ok", True)


def test_guarded_mark_reviewed_records_the_reviewed_mtime(decide_world, capsys):
    row = _target(decide_world, capsys)
    doc = _guarded(decide_world, capsys, "keep_going", row["id"], f"--if-mtime={row['mtime']}")
    assert doc == _outcome("keep_going", row["id"], "ok", f"kept going: {row['label']}", wrote=True)
    assert _registry_thread(decide_world, row["id"])["reviewedAtMtime"] == row["mtime"]


def test_guarded_snooze_stores_the_until_time_in_utc(decide_world, capsys):
    row = _target(decide_world, capsys)
    doc = _guarded(
        decide_world, capsys, "snooze", row["id"], f"--if-mtime={row['mtime']}", FUTURE_UNTIL
    )
    assert doc == _outcome(
        "snooze", row["id"], "ok", f"snoozed {row['label']} until 2026-10-20", wrote=True
    )
    thread = _registry_thread(decide_world, row["id"])
    assert thread["snoozedUntil"] == "2026-10-20T07:00:00+00:00"


def test_guarded_snooze_into_the_past_is_refused(decide_world, capsys):
    row = _target(decide_world, capsys)
    doc = _guarded(
        decide_world,
        capsys,
        "snooze",
        row["id"],
        f"--if-mtime={row['mtime']}",
        "--until=2026-10-01T09:00:00+00:00",
    )
    assert doc == _refusal("snooze", row["id"], "snooze time must be in the future")
    assert _registry_thread(decide_world, row["id"]).get("snoozedAt") is None


def test_guarded_keep_marks_the_thread_kept(decide_world, capsys):
    row = _target(decide_world, capsys)
    doc = _guarded(decide_world, capsys, "keep", row["id"], f"--if-mtime={row['mtime']}")
    assert doc == _outcome("keep", row["id"], "ok", f"kept long-term: {row['label']}", wrote=True)
    thread = _registry_thread(decide_world, row["id"])
    assert thread["decision"] == "keep"
    assert thread["keptAt"] == NOW_DT.isoformat()


def test_guarded_keep_accepts_and_ignores_yes(decide_world, capsys):
    row = _target(decide_world, capsys)
    doc = _guarded(decide_world, capsys, "keep", row["id"], "--yes", f"--if-mtime={row['mtime']}")
    assert doc["status"] == "ok"
    assert _registry_thread(decide_world, row["id"])["decision"] == "keep"


def test_guarded_keep_of_an_archived_session_fails_without_restoring_it(
    decide_world, capsys, monkeypatch
):
    row = _target(decide_world, capsys)
    _guarded(decide_world, capsys, "archive", row["id"], *_stamped(row))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("prompted"))
    doc = _guarded(decide_world, capsys, "keep", row["id"], f"--if-mtime={row['mtime']}")
    assert doc == _outcome("keep", row["id"], "failed", f"{row['id']}: no longer in the registry")
    assert _registry_thread(decide_world, row["id"]) is None


def test_guarded_mark_reviewed_on_a_missing_session_fails_without_writing(decide_world, capsys):
    doc = _guarded(
        decide_world, capsys, "keep_going", "claude-code:does-not-exist", "--if-mtime=2026-10-01"
    )
    assert doc == _outcome(
        "keep_going",
        "claude-code:does-not-exist",
        "failed",
        "claude-code:does-not-exist: no longer in the registry",
    )


def test_an_empty_if_mtime_still_takes_the_guarded_path(decide_world, capsys):
    row = _target(decide_world, capsys)
    doc = _guarded(decide_world, capsys, "keep_going", row["id"], "--if-mtime=")
    assert doc["kind"] == "outcome"
    assert _registry_thread(decide_world, row["id"])["reviewedAtMtime"] == ""


def test_guarded_harvest_saves_the_notes_and_archives(decide_world, capsys, monkeypatch, kills):
    model = FakeModel(HARVEST_ANSWER)
    _model(monkeypatch, model)
    row = _target(decide_world, capsys)
    doc = _guarded(decide_world, capsys, "harvest", row["id"], *_stamped(row))
    saved = Path(doc["path"])
    assert doc == _outcome(
        "harvest",
        row["id"],
        "ok",
        f"harvested {row['label']} → {saved}",
        wrote=True,
        path=str(saved),
    )
    harvests = decide_world.root / "xdg-data" / "ringleader" / "harvests"
    assert saved.suffix == ".md"
    assert saved.is_relative_to(harvests)
    assert saved.exists()
    assert not Path(row["file"]).exists()
    assert model.calls == 1
    assert kills == [1]


def test_guarded_harvest_with_nothing_worth_keeping_is_empty(decide_world, capsys, monkeypatch):
    _model(monkeypatch, FakeModel(EMPTY_ANSWER))
    row = _target(decide_world, capsys)
    doc = _guarded(decide_world, capsys, "harvest", row["id"], *_stamped(row))
    assert doc == _outcome("harvest", row["id"], "empty", "nothing worth harvesting")
    assert Path(row["file"]).exists()


def test_guarded_harvest_of_an_already_harvested_session_only_archives(
    decide_world, capsys, monkeypatch
):
    model = FakeModel(HARVEST_ANSWER)
    _model(monkeypatch, model)
    row = _target(decide_world, capsys)

    def harvested(data):
        thread = next(t for t in data["threads"] if registry.qualified_id(t) == row["id"])
        thread["decision"] = "harvested"

    _edit_registry(decide_world, harvested)
    doc = _guarded(decide_world, capsys, "harvest", row["id"], *_stamped(row))
    assert (doc["status"], doc["wrote"]) == ("ok", True)
    assert model.calls == 0
    assert not Path(row["file"]).exists()


def test_a_failing_harvest_model_is_a_failed_outcome(decide_world, capsys, monkeypatch):
    class Broken(FakeModel):
        def complete_json(self, prompt, schema):
            raise ModelError("model crashed")

    _model(monkeypatch, Broken(HARVEST_ANSWER))
    row = _target(decide_world, capsys)
    doc = _guarded(decide_world, capsys, "harvest", row["id"], *_stamped(row))
    assert doc == _outcome(
        "harvest", row["id"], "failed", f"harvest {row['label']} failed: model crashed"
    )
    assert Path(row["file"]).exists()


def test_guarded_harvest_without_a_model_is_refused(decide_world, capsys, monkeypatch):
    _model(monkeypatch, None)
    row = _target(decide_world, capsys)
    doc = _guarded(decide_world, capsys, "harvest", row["id"], *_stamped(row))
    assert doc == _refusal("harvest", row["id"], "no harvest model configured — run rl config")
    assert Path(row["file"]).exists()


def _add_opencode(mtime):
    def edit(data):
        data["threads"].append(
            {
                **data["threads"][0],
                "sessionId": "oc-1",
                "source": "opencode",
                "mtime": mtime,
                "fileFingerprint": "1:2:3:4:5",
                "decision": None,
            }
        )

    return edit


@pytest.mark.parametrize("action", ["archive", "delete", "harvest"])
def test_a_guarded_verb_on_a_source_without_a_file_lifecycle_is_refused(
    decide_world, capsys, monkeypatch, action
):
    _model(monkeypatch, FakeModel(HARVEST_ANSWER))
    mtime = "2026-10-01T00:00:00+00:00"
    _edit_registry(decide_world, _add_opencode(mtime))
    doc = _guarded(
        decide_world,
        capsys,
        action,
        "opencode:oc-1",
        f"--if-mtime={mtime}",
        "--if-fingerprint=1:2:3:4:5",
    )
    reason = doc["reason"]
    assert reason.startswith(f"cannot {action}")
    assert "no filesystem transcript lifecycle" in reason
    assert doc == _refusal(action, "opencode:oc-1", reason)


@pytest.mark.parametrize("roots", [{"store": "relative/store"}, {"root": "/elsewhere"}])
@pytest.mark.parametrize("action", ["archive", "delete", "harvest"])
def test_a_row_with_corrupt_native_roots_is_refused_with_its_lifecycle_block(
    decide_world, capsys, monkeypatch, action, roots
):
    _model(monkeypatch, FakeModel(HARVEST_ANSWER))
    row_id = _target(decide_world, capsys)["id"]

    def corrupt(data):
        thread = next(t for t in data["threads"] if registry.qualified_id(t) == row_id)
        thread["nativeRoots"] = roots

    _edit_registry(decide_world, corrupt)
    row = next(r for r in _list(decide_world, capsys)["sessions"] if r["id"] == row_id)
    doc = _guarded(decide_world, capsys, action, row_id, *_stamped(row))
    assert doc == _refusal(action, row_id, row["lifecycle_block"])
    assert Path(row["file"]).exists()


@pytest.mark.parametrize("action", DESTRUCTIVE)
def test_a_transcript_that_grew_after_the_caller_looked_is_refused(
    decide_world, capsys, monkeypatch, action
):
    _model(monkeypatch, FakeModel(HARVEST_ANSWER))
    row = _target(decide_world, capsys)
    with open(row["file"], "a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant"}\n')
    doc = _guarded(decide_world, capsys, action, row["id"], *_stamped(row))
    label = None if action == "harvest" else row["label"]
    assert doc == _refusal(action, row["id"], "changed since you looked", label)
    assert Path(row["file"]).exists()


def _terminating_harvest(monkeypatch):
    class Terminated(FakeModel):
        def complete_json(self, prompt, schema):
            signal.raise_signal(signal.SIGTERM)

    _model(monkeypatch, Terminated(HARVEST_ANSWER))


def test_sigterm_during_a_guarded_harvest_exits_143_and_reaps_the_model(
    decide_world, capsys, monkeypatch, kills
):
    _terminating_harvest(monkeypatch)
    row = _target(decide_world, capsys)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as exited:
        cli.main(_verb("harvest", row["id"], "--json", *_stamped(row), *_reg_arg(decide_world)))
    assert exited.value.code == 143
    assert capsys.readouterr().out == ""
    assert kills == [1]
    assert signal.getsignal(signal.SIGTERM) == before
    assert Path(row["file"]).exists()


def test_the_sigterm_handler_is_restored_even_if_reaping_the_model_fails(
    decide_world, capsys, monkeypatch
):
    def broken_kill():
        raise OSError("kill failed")

    monkeypatch.setattr(proc, "kill_all_children", broken_kill)
    row = _target(decide_world, capsys)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(OSError, match="kill failed"):
        cli.main(
            _verb(
                "keep_going",
                row["id"],
                "--json",
                f"--if-mtime={row['mtime']}",
                *_reg_arg(decide_world),
            )
        )
    assert signal.getsignal(signal.SIGTERM) == before


def test_an_ambiguous_id_on_a_guarded_json_call_is_an_error_document(decide_world, capsys):
    row = _target(decide_world, capsys)

    def duplicate(data):
        thread = next(t for t in data["threads"] if registry.qualified_id(t) == row["id"])
        data["threads"].append({**thread})

    _edit_registry(decide_world, duplicate)
    code, doc = _main(
        capsys, *_verb("keep", row["id"], "--json", "--if-mtime=m", *_reg_arg(decide_world))
    )
    assert code == 1
    assert (doc["version"], doc["kind"]) == (1, "error")
    assert "ambiguous" in doc["message"]


def test_guarded_human_ok_prints_the_message_and_path(decide_world, capsys):
    row = _target(decide_world, capsys)
    code = cli.main(_verb("archive", row["id"], *_stamped(row), *_reg_arg(decide_world)))
    captured = capsys.readouterr()
    assert code == 0
    archived = _archived_path(decide_world, row["id"])
    assert captured.out == f"archived {row['label']}\n{archived}\n"
    assert not Path(row["file"]).exists()


def test_guarded_human_refusal_goes_to_stderr_with_exit_1(decide_world, capsys):
    row = _target(decide_world, capsys)
    argv = _verb("archive", row["id"], "--if-mtime=2020-01-01T00:00:00+00:00")
    code = cli.main([*argv, f"--if-fingerprint={row['file_fingerprint']}", *_reg_arg(decide_world)])
    captured = capsys.readouterr()
    assert code == 1
    assert (captured.out, captured.err) == (
        "",
        f"rl-archive: {row['label']}: changed since you looked\n",
    )


def test_guarded_human_harvest_prints_the_saved_path_once(decide_world, capsys, monkeypatch):
    _model(monkeypatch, FakeModel(HARVEST_ANSWER))
    row = _target(decide_world, capsys)
    code = cli.main(_verb("harvest", row["id"], *_stamped(row), *_reg_arg(decide_world)))
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.startswith(f"harvested {row['label']} → ")
    saved = captured.out.removeprefix(f"harvested {row['label']} → ").strip()
    assert captured.out == f"harvested {row['label']} → {saved}\n"
    assert Path(saved).exists()


def test_guarded_human_empty_harvest_prints_to_stdout_with_exit_0(
    decide_world, capsys, monkeypatch
):
    _model(monkeypatch, FakeModel(EMPTY_ANSWER))
    row = _target(decide_world, capsys)
    code = cli.main(_verb("harvest", row["id"], *_stamped(row), *_reg_arg(decide_world)))
    captured = capsys.readouterr()
    assert code == 0
    assert (captured.out, captured.err) == ("nothing worth harvesting\n", "")
    assert Path(row["file"]).exists()


def test_guarded_human_failure_reports_where_the_work_was_saved(capsys, monkeypatch):
    def failed(*_args, **_kwargs):
        return contract.GuiOutcome(
            "failed", None, "harvested, not archived: boom", True, "/n.md", ()
        )

    monkeypatch.setattr(guarded, "decide", failed)
    code = cli.main(["session", "keep", "claude-code:a", "--if-mtime=m"])
    captured = capsys.readouterr()
    assert code == 1
    assert (captured.out, captured.err) == (
        "",
        "rl-keep: harvested, not archived: boom\n/n.md\n",
    )


def test_guarded_human_warnings_go_to_stderr(decide_world, capsys, monkeypatch):
    def warned(*_args, **_kwargs):
        return contract.GuiOutcome("ok", None, "kept going: x", True, None, ("index stale",))

    monkeypatch.setattr(guarded, "decide", warned)
    code = cli.main(["session", "mark", "reviewed", "claude-code:a", "--if-mtime=m"])
    captured = capsys.readouterr()
    assert code == 0
    assert (captured.out, captured.err) == ("kept going: x\n", "rl-mark-reviewed: index stale\n")


def test_guarded_human_error_is_reported_with_the_verb_prefix(world, tmp_path, capsys):
    bad = tmp_path / "corrupt.json"
    bad.write_text("{not json", encoding="utf-8")
    code = cli.main(
        ["session", "snooze", "claude-code:a", "--if-mtime=m", FUTURE_UNTIL, "--registry", str(bad)]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("rl-snooze: ")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["session", "archive", "claude-code:a", "--json"], "--json needs --if-mtime"),
        (
            ["session", "keep", "claude-code:a", "--if-fingerprint=1:2:3:4:5"],
            "--if-fingerprint needs --if-mtime",
        ),
        (["session", "keep", "a", "--if-mtime=m"], "ID must be qualified"),
        (["session", "harvest", "--archive", "--if-mtime=m"], "needs a session ID"),
        (["session", "harvest", "claude-code:a", "--if-mtime=m"], "needs --archive"),
        (["session", "snooze", "claude-code:a", "--if-mtime=m"], "needs --until"),
        (
            ["session", "archive", "claude-code:a", "--if-mtime=m", "--archive-root=/x"],
            "--archive-root cannot be combined",
        ),
        (
            ["session", "archive", "claude-code:a", "--if-mtime=m", "--summary-file=s.md"],
            "--summary-file cannot be combined",
        ),
        (
            ["session", "harvest", "claude-code:a", "--archive", "--if-mtime=m", "--current"],
            "--current cannot be combined",
        ),
        (
            ["session", "harvest", "claude-code:a", "--archive", "--if-mtime=m", "--from-file=-"],
            "--from-file cannot be combined",
        ),
        (
            ["session", "harvest", "claude-code:a", "--archive", "--if-mtime=m", "--dry-run"],
            "--dry-run cannot be combined",
        ),
        (
            ["session", "snooze", "claude-code:a", "--if-mtime=", "--for=3"],
            "--for cannot be combined",
        ),
        (
            ["session", "snooze", "claude-code:a", "--if-mtime=m", "--until=U", "--for=0"],
            "--for cannot be combined",
        ),
        (["session", "harvest", "claude-code:a", "--archive"], "--archive needs --if-mtime"),
    ],
    ids=[
        "json-without-if-mtime",
        "if-fingerprint-without-if-mtime",
        "unqualified-id",
        "harvest-without-id",
        "harvest-without-archive",
        "snooze-without-until",
        "archive-root",
        "summary-file",
        "harvest-current",
        "harvest-from-file",
        "harvest-dry-run",
        "empty-if-mtime-with-for",
        "zero-for-with-until",
        "archive-without-guards",
    ],
)
def test_guarded_argument_rules_exit_2_without_stdout(world, capsys, argv, message):
    with pytest.raises(SystemExit) as raised:
        cli.main([*argv, *_reg_arg(world)])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert "usage: rl session " in captured.err


def test_plain_snooze_hides_the_session_until_the_given_time(world, capsys):
    row = _target(world, capsys)
    until = "2026-11-01T00:00:00+00:00"
    assert cli.main(["session", "snooze", row["id"], f"--until={until}", *_reg_arg(world)]) == 0
    assert capsys.readouterr().out == (
        f"snoozed {row['id']} (auto-wakes {until}, or `rl session wake {row['id'][:8]}…` sooner)\n"
    )
    assert _registry_thread(world, row["id"])["snoozedUntil"] == until


def test_plain_archive_without_archive_root_uses_the_state_archive(world, capsys):
    row = _target(world, capsys)
    assert cli.main(["session", "archive", row["id"], *_reg_arg(world)]) == 0
    dest = _archived_path(world, row["id"])
    assert capsys.readouterr().out == f"archived {row['id']} -> {dest}\n"
    assert Path(dest).is_relative_to(world.root / "xdg-data" / "ringleader" / "archive")
    assert Path(dest).exists()
    assert not Path(row["file"]).exists()


def test_rl_gui_is_no_longer_a_command(capsys):
    assert cli.main(["gui", "list"]) == 2
    assert "rl: unknown command 'gui'" in capsys.readouterr().err
