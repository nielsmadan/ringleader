import json
import os
import time

from ringleader.commands import roundup as roundup_mod


def test_roundup_refreshes_prunes_enriches_and_stamps(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 2, "threads": [], "archived": []}))
    calls = []
    monkeypatch.setattr(
        roundup_mod.refresh,
        "auto_refresh",
        lambda p, now, **k: (
            calls.append("refresh") or (roundup_mod.registry.load_registry(p), None)
        ),
    )
    monkeypatch.setattr(roundup_mod.prune, "prune_candidates", lambda r, now, **k: [])
    monkeypatch.setattr(roundup_mod.enrich, "cmd_enrich", lambda argv: calls.append("enrich") or 0)
    rc = roundup_mod.cmd_roundup(["--registry", str(reg), "--yes"])
    assert rc == 0 and calls == ["refresh", "enrich"]
    assert json.loads(reg.read_text()).get("lastRoundupAt")


def test_roundup_purges_expired_archives(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    (xdg / "ringleader").mkdir(parents=True)
    (xdg / "ringleader" / "config.json").write_text(json.dumps({"archive_retention_days": 30}))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))

    arch = xdg / "ringleader" / "archive" / "repo"  # matches archive_root() under XDG
    arch.mkdir(parents=True)
    jsonl = arch / "a.jsonl"
    jsonl.write_text("t")
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [],
                "archived": [
                    {
                        "sessionId": "a",
                        "archivedAt": "2026-01-01T00:00:00+00:00",
                        "reason": "archived",
                        "archivePath": str(jsonl),
                        "harvestPath": None,
                        "summary": "s",
                        "project": "repo",
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(roundup_mod.refresh, "auto_refresh", lambda p, now, **k: None)
    monkeypatch.setattr(roundup_mod.enrich, "cmd_enrich", lambda argv: 0)

    rc = roundup_mod.cmd_roundup(["--registry", str(reg), "--yes"])

    assert rc == 0
    assert not jsonl.exists()
    assert json.loads(reg.read_text())["deleted"][0]["reason"] == "purged"


def test_roundup_archives_a_session_marked_finish_on_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    projects = tmp_path / "projects" / "-w-x"
    projects.mkdir(parents=True)
    transcript = projects / "s1.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n')
    when = time.time() - 600
    os.utime(transcript, (when, when))
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "projects"))

    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "archived": [],
                "deleted": [],
                "threads": [
                    {
                        "sessionId": "s1",
                        "file": str(transcript),
                        "project": "/w/x",
                        "source": "claude-code",
                        "decision": "harvested",
                        "finishOnExit": True,
                    }
                ],
            }
        )
    )

    roundup_mod.cmd_roundup(["--registry", str(reg), "--yes"])

    out = json.loads(reg.read_text())
    assert [a["sessionId"] for a in out["archived"]] == ["s1"]
    assert not transcript.exists()
