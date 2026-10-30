import json
import signal
from pathlib import Path

import pytest
from ringleader import cli, contract_io, guarded, presence, registry
from ringleader.models import proc
from tests.test_json_commands import NOW_DT, gui_world


@pytest.fixture
def world(monkeypatch, tmp_path):
    return gui_world(monkeypatch, tmp_path, threads=40, transcripts=True)


def _reg_arg(fixture):
    return ["--registry", str(fixture.registry_path)]


def _list(fixture, capsys):
    assert cli.main(["inbox", "--json", *_reg_arg(fixture)]) == 0
    return json.loads(capsys.readouterr().out)


def _edit_registry(fixture, edit):
    data = json.loads(fixture.registry_path.read_text(encoding="utf-8"))
    edit(data)
    fixture.registry_path.write_text(json.dumps(data), encoding="utf-8")


def test_unknown_gui_subcommand_returns_2(capsys):
    assert cli.main(["gui", "nope"]) == 2
    assert "unknown command 'nope' in 'rl gui'" in capsys.readouterr().err


def test_bare_gui_lists_its_subcommands(capsys):
    assert cli.main(["gui"]) == 0
    assert "rl gui — subcommands: decide" in capsys.readouterr().out


def test_the_command_table_lists_the_gui_dispatch_table():
    assert {name: entry.resolve() for name, entry in cli.TABLE["gui"].items()} == (
        contract_io.GUI_COMMANDS
    )


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


def _decide(fixture, capsys, *args):
    code = contract_io.cmd_gui_decide([*args, *_reg_arg(fixture)])
    out = capsys.readouterr().out
    assert code == 0
    doc = json.loads(out)
    assert (doc["version"], doc["kind"]) == (1, "outcome")
    return doc


def _stamped(row, action):
    return [
        action,
        row["id"],
        f"--mtime={row['mtime']}",
        f"--fingerprint={row['file_fingerprint']}",
    ]


def _registry_thread(fixture, row_id):
    return registry.find_session(registry.load_registry(fixture.registry_path), row_id)


def test_archive_moves_the_transcript_and_drops_the_row(decide_world, capsys, kills):
    row = _target(decide_world, capsys)
    doc = _decide(decide_world, capsys, *_stamped(row, "archive"))
    assert (doc["action"], doc["id"], doc["status"], doc["wrote"]) == (
        "archive",
        row["id"],
        "ok",
        True,
    )
    assert not Path(row["file"]).exists()
    assert row["id"] not in {r["id"] for r in _list(decide_world, capsys)["sessions"]}
    assert kills == [1]


def test_archive_with_another_mtime_is_refused(decide_world, capsys):
    row = _target(decide_world, capsys)
    args = _stamped(row, "archive")
    args[2] = "--mtime=2020-01-01T00:00:00+00:00"
    doc = _decide(decide_world, capsys, *args)
    assert (doc["status"], doc["reason"], doc["wrote"]) == (
        "refused",
        "changed since you looked",
        False,
    )
    assert Path(row["file"]).exists()


@pytest.mark.parametrize("action", ["archive", "delete", "harvest"])
def test_a_destructive_decision_without_a_fingerprint_is_refused(
    decide_world, capsys, monkeypatch, action
):
    monkeypatch.setattr(guarded, "_harvest_model", lambda: pytest.fail("model resolved"))
    row = _target(decide_world, capsys)
    doc = _decide(decide_world, capsys, action, row["id"], f"--mtime={row['mtime']}")
    assert (doc["status"], doc["reason"]) == (
        "refused",
        "changed since you looked: no fingerprint, rescan",
    )
    assert Path(row["file"]).exists()


def test_archive_of_a_running_session_is_refused(decide_world, capsys, monkeypatch):
    row = _target(decide_world, capsys)
    _live(monkeypatch, frozenset({("claude", row["session_id"])}))
    doc = _decide(decide_world, capsys, *_stamped(row, "archive"))
    assert doc["status"] == "refused"
    assert "is running" in doc["reason"]
    assert Path(row["file"]).exists()


def test_archive_is_refused_when_presence_cannot_answer(decide_world, capsys, monkeypatch):
    row = _target(decide_world, capsys)
    _live(monkeypatch, presence.PresenceUnavailable("hooklinesinker is not installed"))
    doc = _decide(decide_world, capsys, *_stamped(row, "archive"))
    assert doc["status"] == "refused"
    assert doc["reason"].startswith("can't tell whether")
    assert Path(row["file"]).exists()


def test_delete_removes_the_transcript_and_the_thread(decide_world, capsys):
    row = _target(decide_world, capsys)
    doc = _decide(decide_world, capsys, *_stamped(row, "delete"))
    assert (doc["status"], doc["wrote"]) == ("ok", True)
    assert not Path(row["file"]).exists()
    assert _registry_thread(decide_world, row["id"]) is None


def test_keep_going_records_the_reviewed_mtime(decide_world, capsys):
    row = _target(decide_world, capsys)
    doc = _decide(decide_world, capsys, "keep_going", row["id"], f"--mtime={row['mtime']}")
    assert doc["status"] == "ok"
    assert _registry_thread(decide_world, row["id"])["reviewedAtMtime"] == row["mtime"]


def test_snooze_stores_the_until_time_in_utc(decide_world, capsys):
    row = _target(decide_world, capsys)
    doc = _decide(
        decide_world,
        capsys,
        "snooze",
        row["id"],
        f"--mtime={row['mtime']}",
        "--until=2026-10-20T09:00:00+02:00",
    )
    assert doc["status"] == "ok"
    thread = _registry_thread(decide_world, row["id"])
    assert thread["snoozedUntil"] == "2026-10-20T07:00:00+00:00"


def test_snooze_into_the_past_is_refused(decide_world, capsys):
    row = _target(decide_world, capsys)
    doc = _decide(
        decide_world,
        capsys,
        "snooze",
        row["id"],
        f"--mtime={row['mtime']}",
        "--until=2026-10-01T09:00:00+00:00",
    )
    assert (doc["status"], doc["reason"], doc["wrote"]) == (
        "refused",
        "snooze time must be in the future",
        False,
    )
    assert _registry_thread(decide_world, row["id"]).get("snoozedAt") is None


@pytest.mark.parametrize(
    "args",
    [
        ["snooze", "claude-code:a", "--mtime=m"],
        ["keep", "claude-code:a", "--mtime=m", "--until=2026-10-20T09:00:00Z"],
        ["keep", "a", "--mtime=m"],
        ["keep", "claude-code:a"],
        ["nap", "claude-code:a", "--mtime=m"],
    ],
    ids=["snooze-without-until", "until-on-keep", "unqualified-id", "no-mtime", "bad-action"],
)
def test_decide_usage_errors_exit_2_without_stdout(decide_world, capsys, args):
    with pytest.raises(SystemExit) as raised:
        contract_io.cmd_gui_decide([*args, *_reg_arg(decide_world)])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "rl gui decide" in captured.err


def test_keep_marks_the_thread_kept(decide_world, capsys):
    row = _target(decide_world, capsys)
    doc = _decide(decide_world, capsys, "keep", row["id"], f"--mtime={row['mtime']}")
    assert doc["status"] == "ok"
    thread = _registry_thread(decide_world, row["id"])
    assert thread["decision"] == "keep"
    assert thread["keptAt"] == NOW_DT.isoformat()


def test_keep_going_on_a_missing_session_fails_without_writing(decide_world, capsys):
    doc = _decide(
        decide_world, capsys, "keep_going", "claude-code:does-not-exist", "--mtime=2026-10-01"
    )
    assert (doc["status"], doc["wrote"]) == ("failed", False)


def test_harvest_saves_the_notes_and_archives(decide_world, capsys, monkeypatch, kills):
    model = FakeModel(HARVEST_ANSWER)
    _model(monkeypatch, model)
    row = _target(decide_world, capsys)
    doc = _decide(decide_world, capsys, *_stamped(row, "harvest"))
    assert (doc["status"], doc["wrote"]) == ("ok", True)
    saved = Path(doc["path"])
    harvests = decide_world.root / "xdg-data" / "ringleader" / "harvests"
    assert saved.suffix == ".md"
    assert saved.is_relative_to(harvests)
    assert saved.exists()
    assert not Path(row["file"]).exists()
    assert model.calls == 1
    assert kills == [1]


def test_harvest_with_nothing_worth_keeping_is_empty(decide_world, capsys, monkeypatch):
    _model(monkeypatch, FakeModel(EMPTY_ANSWER))
    row = _target(decide_world, capsys)
    doc = _decide(decide_world, capsys, *_stamped(row, "harvest"))
    assert (doc["status"], doc["message"], doc["wrote"]) == (
        "empty",
        "nothing worth harvesting",
        False,
    )
    assert Path(row["file"]).exists()


def test_harvest_without_a_model_is_refused(decide_world, capsys, monkeypatch):
    _model(monkeypatch, None)
    row = _target(decide_world, capsys)
    doc = _decide(decide_world, capsys, *_stamped(row, "harvest"))
    assert doc["status"] == "refused"
    assert "no harvest model configured" in doc["reason"]
    assert Path(row["file"]).exists()


def test_harvest_of_a_source_without_a_file_lifecycle_is_refused(decide_world, capsys, monkeypatch):
    _model(monkeypatch, FakeModel(HARVEST_ANSWER))
    mtime = "2026-10-01T00:00:00+00:00"

    def add_opencode(data):
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

    _edit_registry(decide_world, add_opencode)
    doc = _decide(
        decide_world,
        capsys,
        "harvest",
        "opencode:oc-1",
        f"--mtime={mtime}",
        "--fingerprint=1:2:3:4:5",
    )
    assert doc["status"] == "refused"
    assert doc["reason"].startswith("cannot harvest")
    assert "no filesystem transcript lifecycle" in doc["reason"]


@pytest.mark.parametrize("action", ["archive", "delete"])
def test_removing_a_source_without_a_file_lifecycle_is_refused(decide_world, capsys, action):
    mtime = "2026-10-01T00:00:00+00:00"

    def add_opencode(data):
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

    _edit_registry(decide_world, add_opencode)
    doc = _decide(
        decide_world, capsys, action, "opencode:oc-1", f"--mtime={mtime}", "--fingerprint=1:2:3:4:5"
    )
    assert doc["status"] == "refused"
    assert doc["reason"].startswith(f"cannot {action}")


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
    doc = _decide(decide_world, capsys, *_stamped(row, action))
    assert (doc["status"], doc["reason"]) == ("refused", row["lifecycle_block"])
    assert Path(row["file"]).exists()


@pytest.mark.parametrize("action", ["archive", "harvest"])
def test_a_transcript_that_grew_after_the_gui_looked_is_refused(
    decide_world, capsys, monkeypatch, action
):
    _model(monkeypatch, FakeModel(HARVEST_ANSWER))
    row = _target(decide_world, capsys)
    with open(row["file"], "a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant"}\n')
    doc = _decide(decide_world, capsys, *_stamped(row, action))
    assert (doc["status"], doc["reason"], doc["wrote"]) == (
        "refused",
        "changed since you looked",
        False,
    )
    assert Path(row["file"]).exists()


def test_sigterm_during_a_harvest_exits_143_and_reaps_the_model(
    decide_world, capsys, monkeypatch, kills
):
    class Terminated(FakeModel):
        def complete_json(self, prompt, schema):
            signal.raise_signal(signal.SIGTERM)

    _model(monkeypatch, Terminated(HARVEST_ANSWER))
    row = _target(decide_world, capsys)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as exited:
        contract_io.cmd_gui_decide([*_stamped(row, "harvest"), *_reg_arg(decide_world)])
    assert exited.value.code == 143
    assert capsys.readouterr().out == ""
    assert kills == [1]
    assert signal.getsignal(signal.SIGTERM) == before
    assert Path(row["file"]).exists()
