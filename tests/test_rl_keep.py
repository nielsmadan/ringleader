import json
import subprocess
import sys
from pathlib import Path

from ringleader.commands import keep

REPO = Path(__file__).resolve().parent.parent


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "ringleader", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )


def _thread(sid, **kw):
    base = {
        "sessionId": sid,
        "source": "claude-code",
        "file": f"/nope/{sid}.jsonl",
        "project": "/p",
        "gitProjectKey": "repo-k",
        "gitProjectLabel": "repo",
        "title": sid,
        "whatItIs": f"does {sid}",
        "status": "unfinished",
        "mtime": "2026-08-01T00:00:00+00:00",
        "sizeBytes": 10,
        "messageCount": 5,
        "decision": None,
        "keptAt": None,
        "snoozedAt": None,
        "snoozedUntil": None,
        "reviewedAtMtime": None,
    }
    base.update(kw)
    return base


def _reg(tmp_path, *threads, archived=None):
    p = tmp_path / "registry.json"
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": list(threads),
                "archived": list(archived or []),
                "deleted": [],
            }
        )
    )
    return p


def _triage_ids(reg):
    r = _run("triage", "--no-scan", "--json", "--registry", str(reg))
    assert r.returncode == 0, r.stderr
    return [x["session_id"] for x in json.loads(r.stdout)]


def test_keep_removes_from_triage_and_unkeep_restores(tmp_path):
    reg = _reg(tmp_path, _thread("a"), _thread("b"))
    assert sorted(_triage_ids(reg)) == ["a", "b"]

    r = _run("session", "keep", "a", "--registry", str(reg))
    assert r.returncode == 0, r.stderr
    assert _triage_ids(reg) == ["b"]

    saved = json.loads(reg.read_text())["threads"]
    kept = next(t for t in saved if t["sessionId"] == "a")
    assert kept["decision"] == "keep"
    assert kept["keptAt"]

    r = _run("session", "unkeep", "a", "--registry", str(reg))
    assert r.returncode == 0, r.stderr
    assert sorted(_triage_ids(reg)) == ["a", "b"]


def test_kept_session_stays_out_of_triage_after_new_activity(tmp_path):
    """keep is permanent — unlike `mark reviewed`, later activity must not resurface it."""
    reg = _reg(tmp_path, _thread("a"), _thread("b"))
    assert _run("session", "keep", "a", "--registry", str(reg)).returncode == 0

    data = json.loads(reg.read_text())
    for t in data["threads"]:
        if t["sessionId"] == "a":
            t["mtime"] = "2026-08-17T00:00:00+00:00"
    reg.write_text(json.dumps(data))

    assert _triage_ids(reg) == ["b"]


def test_kept_view_text_and_json(tmp_path):
    reg = _reg(tmp_path, _thread("a"), _thread("b"))
    assert _run("session", "keep", "a", "--registry", str(reg)).returncode == 0

    r = _run("overview", "--kept", "--no-scan", "--registry", str(reg))
    assert r.returncode == 0, r.stderr
    assert "a" in r.stdout and "does a" in r.stdout

    r = _run("overview", "--kept", "--no-scan", "--json", "--registry", str(reg))
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert [x["session_id"] for x in rows] == ["a"]
    assert rows[0]["kept_at"]


def test_kept_empty_state(tmp_path):
    reg = _reg(tmp_path, _thread("a"))
    r = _run("overview", "--kept", "--no-scan", "--registry", str(reg))
    assert r.returncode == 0
    assert "no kept sessions" in r.stdout


def test_kept_session_still_listed_as_live(tmp_path):
    """keep silences triage only — the session stays visible in the session list."""
    reg = _reg(tmp_path, _thread("a"))
    assert _run("session", "keep", "a", "--registry", str(reg)).returncode == 0
    r = _run("overview", "-s", "--no-scan", "--json", "--registry", str(reg))
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert [x["session_id"] for x in rows] == ["a"]
    assert rows[0]["kept"] is True


def test_unkeep_on_a_session_that_is_not_kept_fails(tmp_path):
    reg = _reg(tmp_path, _thread("a"))
    r = _run("session", "unkeep", "a", "--registry", str(reg))
    assert r.returncode == 1
    assert "not kept" in r.stderr


def test_keep_unknown_session_fails(tmp_path):
    reg = _reg(tmp_path, _thread("a"))
    r = _run("session", "keep", "nope", "--registry", str(reg))
    assert r.returncode == 1
    assert "unknown session" in r.stderr


def test_keep_on_archived_session_offers_to_restore(tmp_path):
    """Keeping an archived session restores it first — but a tombstone written before
    ringleader recorded originalPath can't be auto-restored, and must say so."""
    reg = _reg(
        tmp_path,
        _thread("a"),
        archived=[{"sessionId": "gone", "archivePath": "/arc/gone.jsonl"}],
    )
    r = _run("session", "keep", "gone", "--yes", "--registry", str(reg))
    assert r.returncode == 1
    assert "archived" in r.stdout
    assert "original path" in r.stderr


def test_declining_keep_does_not_mutate_the_registry(tmp_path, monkeypatch, capsys):
    path = _reg(tmp_path, _thread("a", decision="harvested"))
    loaded = keep.registry.load_registry(path)
    monkeypatch.setattr(keep.registry, "load_registry", lambda _: loaded)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    assert keep.cmd_keep(["a", "--registry", str(path)]) == 0

    assert loaded["threads"][0]["decision"] == "harvested"
    assert json.loads(path.read_text())["threads"][0]["decision"] == "harvested"
    assert "aborted" in capsys.readouterr().out
