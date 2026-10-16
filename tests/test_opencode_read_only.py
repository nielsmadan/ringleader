import json
import sqlite3
from unittest.mock import Mock

import pytest
from ringleader import export, prune, refresh, registry, scanner
from ringleader.commands import (
    archive,
    complete,
    delete,
    finish,
    harvest,
    roundup,
    sweep,
    unarchive,
)


def _db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL);
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            parent_id TEXT,
            slug TEXT NOT NULL,
            directory TEXT NOT NULL,
            title TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            time_archived INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    connection.execute("INSERT INTO project VALUES (?, ?)", ("opaque", "/repo"))
    connection.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ses_1",
            "opaque",
            None,
            "shiny-star",
            "/repo/worktree",
            "Read-only work",
            1770998000000,
            1770998200000,
            None,
        ),
    )
    connection.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        ("msg_1", "ses_1", 1770998100000, 1770998100000, json.dumps({"role": "user"})),
    )
    connection.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
        (
            "part_1",
            "msg_1",
            "ses_1",
            1770998100001,
            1770998100001,
            json.dumps({"type": "text", "text": "hello from sqlite"}),
        ),
    )
    connection.commit()
    connection.close()
    return path


def _thread(**updates):
    thread = {
        "source": "opencode",
        "sourceRef": "ses_1",
        "sessionId": "ses_1",
        "file": None,
        "project": "/repo/worktree",
        "mtime": "2026-01-01T00:00:00+00:00",
        "messageCount": 2,
        "decision": "harvested",
    }
    thread.update(updates)
    return thread


def _registry_file(path, thread=None):
    reg = registry.empty_registry()
    reg["threads"] = [thread or _thread()]
    registry.save_registry(path, reg)
    return path


def test_export_and_harvest_route_db_backed_conversation(tmp_path, monkeypatch, capsys):
    xdg = tmp_path / "xdg"
    _db(xdg / "opencode" / "opencode.db")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    reg_path = _registry_file(tmp_path / "registry.json")

    assert export.render_conversation(_thread()).endswith("hello from sqlite\n")

    class Model:
        def complete_json(self, prompt, schema):
            assert "hello from sqlite" in prompt
            return {"summary": "kept", "gotchas": [], "decisions": [], "whatChanged": []}

    monkeypatch.setattr(harvest, "active_model", lambda config: Model())
    rc, markdown = harvest._prepare_harvest("ses_1", str(reg_path))

    assert rc == 0
    assert "kept" in markdown
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("command", "operation"),
    [
        ("archive", "archive"),
        ("delete", "delete"),
        ("finish", "finish"),
        ("complete", "complete"),
        ("sweep", "archive"),
    ],
)
def test_destructive_commands_refuse_opencode_for_the_expected_reason(
    tmp_path, monkeypatch, capsys, command, operation
):
    reg_path = _registry_file(tmp_path / "registry.json", _thread(finishOnExit=True))
    prepare_harvest = Mock(side_effect=AssertionError("harvest must not run"))
    monkeypatch.setattr(complete.harvest_mod, "_prepare_harvest", prepare_harvest)

    if command == "archive":
        result = archive.cmd_archive(
            ["ses_1", "--registry", str(reg_path), "--archive-root", str(tmp_path / "archive")]
        )
    elif command == "delete":
        result = delete.cmd_delete(["ses_1", "--registry", str(reg_path)])
    elif command == "finish":
        result = finish.cmd_finish(["ses_1", "--registry", str(reg_path), "--force"])
    elif command == "complete":
        result = complete.cmd_complete(["ses_1", "--registry", str(reg_path)])
    else:
        result = sweep.cmd_sweep(["--session", "ses_1", "--registry", str(reg_path)])

    assert result == 1
    if command == "complete":
        prepare_harvest.assert_not_called()

    saved = registry.load_registry(reg_path)
    assert [thread["sessionId"] for thread in saved["threads"]] == ["ses_1"]
    assert saved["archived"] == []
    assert saved["deleted"] == []
    error = capsys.readouterr().err
    assert f"cannot {operation} opencode session ses_1" in error
    assert "no filesystem transcript lifecycle" in error


def test_prune_and_roundup_skip_opencode(tmp_path, monkeypatch, capsys):
    reg_path = _registry_file(tmp_path / "registry.json")
    reg = registry.load_registry(reg_path)

    assert prune.delete_sessions(reg, list(reg["threads"]), "2026-08-30T00:00:00+00:00") == 0
    assert [thread["sessionId"] for thread in reg["threads"]] == ["ses_1"]

    monkeypatch.setattr(
        roundup.refresh,
        "refresh_registry",
        lambda path, now, **kwargs: refresh.Refreshed(reg, None, frozenset()),
    )
    monkeypatch.setattr(roundup, "active_model", lambda cfg, task: None)
    monkeypatch.setattr(roundup, "_live_sessions", frozenset)
    assert roundup.cmd_roundup(["--registry", str(reg_path), "--yes"]) == 0
    assert [thread["sessionId"] for thread in registry.load_registry(reg_path)["threads"]] == [
        "ses_1"
    ]
    assert "cannot prune opencode session" in capsys.readouterr().err


def test_unarchive_and_unknown_sources_fail_safe_before_path_conversion():
    reg = registry.empty_registry()
    archived = {
        "sessionId": "ses_1",
        "archivePath": None,
        "originalPath": "/original/ses_1.jsonl",
        "thread": _thread(),
    }
    reg["archived"] = [archived]

    with pytest.raises(RuntimeError, match="no filesystem transcript lifecycle"):
        unarchive.restore(reg, archived)
    with pytest.raises(RuntimeError, match="no filesystem transcript lifecycle"):
        scanner.filesystem_path(
            {"source": "unknown", "sessionId": "x", "file": "/tmp/x.jsonl"}, "delete"
        )
