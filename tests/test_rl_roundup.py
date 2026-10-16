import json
import os
import signal
import threading
import time

import pytest
from ringleader import refresh, registry, stateio
from ringleader import roundup as roundup_api
from ringleader.commands import roundup as roundup_cmd
from ringleader.sources.base import stat_fingerprint

OLD = "2026-01-01T00:00:00+00:00"
GOOD = {"status": "unfinished", "whatItIs": "work in progress", "suggestedName": "wip"}


class FakeModel:
    name = "claude"
    model = "haiku"

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = 0

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        with self._lock:
            self.calls += 1
        return GOOD


def _thread(tmp_path, sid, *, mtime=OLD, msgs=50):
    transcript = tmp_path / "transcripts" / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n')
    return {
        "sessionId": sid,
        "source": "claude-code",
        "file": str(transcript),
        "project": "/w/x",
        "mtime": mtime,
        "messageCount": msgs,
        "status": "unclassified",
        "decision": None,
        "fileFingerprint": stat_fingerprint(transcript.stat()),
    }


def _write(tmp_path, threads, archived=()):
    reg = registry.empty_registry()
    reg["threads"] = list(threads)
    reg["archived"] = list(archived)
    path = tmp_path / "registry.json"
    registry.save_registry(path, reg)
    return path


def _recent_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 86_400))


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(roundup_cmd, "active_model", lambda cfg, task: fake)
    return fake


@pytest.fixture(autouse=True)
def _seams(monkeypatch):
    monkeypatch.setattr(roundup_cmd, "_live_sessions", frozenset)
    monkeypatch.setattr(roundup_cmd, "_interactive", lambda: False)


@pytest.fixture
def no_scan(monkeypatch):
    def unscanned(path, now, **kwargs):
        return refresh.Refreshed(registry.load_registry(path), None, frozenset())

    monkeypatch.setattr(roundup_cmd.refresh, "refresh_registry", unscanned)


def _saved(path):
    return json.loads(path.read_text())


@pytest.mark.usefixtures("no_scan")
def test_under_the_threshold_enriches_all_without_prompting(tmp_path, monkeypatch, model, capsys):
    path = _write(tmp_path, [_thread(tmp_path, "a"), _thread(tmp_path, "b")])
    monkeypatch.setattr(roundup_cmd, "_interactive", lambda: True)

    assert roundup_cmd.cmd_roundup(["--registry", str(path)]) == 0

    assert model.calls == 2
    assert capsys.readouterr().out.splitlines()[-1] == (
        "swept 0, pruned 0, purged 0, enriched 2 (0 failed, 0 stale)"
    )
    assert _saved(path)["lastRoundupAt"]


@pytest.mark.usefixtures("no_scan")
def test_over_the_threshold_without_a_tty_enriches_recent_and_hints(tmp_path, model, capsys):
    threads = [_thread(tmp_path, f"old{i}") for i in range(26)]
    path = _write(tmp_path, [*threads, _thread(tmp_path, "new", mtime=_recent_iso())])

    assert roundup_cmd.cmd_roundup(["--registry", str(path)]) == 0

    assert model.calls == 1
    out = capsys.readouterr().out
    assert (
        "enrich: 27 pending — enriching the 1 from the last 7 days; "
        "run `rl roundup --enrich all` for the rest (26, about 1 min)"
    ) in out


@pytest.mark.usefixtures("no_scan")
def test_over_the_threshold_on_a_tty_asks_for_the_scope(tmp_path, monkeypatch, model):
    threads = [_thread(tmp_path, f"old{i}") for i in range(26)]
    path = _write(tmp_path, threads)
    prompts = []
    monkeypatch.setattr(roundup_cmd, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "s")

    assert roundup_cmd.cmd_roundup(["--registry", str(path)]) == 0

    assert prompts == ["enrich: [a]ll 26 (~1 min) / [r]ecent 0 (~0 min) / [s]kip? [r] "]
    assert model.calls == 0
    assert _saved(path)["lastRoundupAt"]


@pytest.mark.usefixtures("no_scan")
def test_enrich_none_enriches_nothing_and_still_stamps(tmp_path, model):
    path = _write(tmp_path, [_thread(tmp_path, "a")])

    assert roundup_cmd.cmd_roundup(["--registry", str(path), "--enrich", "none"]) == 0

    assert model.calls == 0
    assert _saved(path)["lastRoundupAt"]


@pytest.mark.usefixtures("no_scan")
def test_preview_prints_the_plan_and_deletes_nothing(tmp_path, model, capsys):
    tiny = _thread(tmp_path, "tiny", msgs=3)
    path = _write(tmp_path, [tiny])

    assert roundup_cmd.cmd_roundup(["--registry", str(path), "--preview"]) == 0

    assert os.path.exists(tiny["file"])
    assert "delete 1 tiny session older than 7 days (prune)" in capsys.readouterr().out
    assert _saved(path)["lastRoundupAt"] is None


@pytest.mark.usefixtures("no_scan")
def test_sigint_during_prune_stops_at_the_next_batch(tmp_path, monkeypatch, model):
    monkeypatch.setattr(roundup_api, "PRUNE_BATCH", 1)
    path = _write(tmp_path, [_thread(tmp_path, f"t{i}", msgs=3) for i in range(3)])

    def interrupt(event):
        if event.phase == "prune" and event.done == 1:
            signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(roundup_cmd, "_print_progress", interrupt)
    before = signal.getsignal(signal.SIGINT)

    assert roundup_cmd.cmd_roundup(["--registry", str(path), "--yes"]) == 130

    saved = _saved(path)
    assert [d["sessionId"] for d in saved["deleted"]] == ["t0"]
    assert saved["lastRoundupAt"] is None
    assert model.calls == 0
    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.usefixtures("no_scan")
def test_a_held_roundup_lock_exits_1(tmp_path, model, capsys):
    path = _write(tmp_path, [_thread(tmp_path, "a")])

    with stateio.roundup_lock():
        assert roundup_cmd.cmd_roundup(["--registry", str(path), "--yes"]) == 1

    assert "rl roundup: another roundup is running" in capsys.readouterr().err


@pytest.mark.usefixtures("no_scan")
def test_roundup_purges_expired_archives(tmp_path, monkeypatch):
    data = tmp_path / "xdg"
    (data / "ringleader").mkdir(parents=True)
    (data / "ringleader" / "config.json").write_text(json.dumps({"archive_retention_days": 30}))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setattr(roundup_cmd, "active_model", lambda cfg, task: None)
    archive = data / "ringleader" / "archive" / "repo"
    archive.mkdir(parents=True)
    jsonl = archive / "a.jsonl"
    jsonl.write_text("t")
    path = _write(
        tmp_path,
        [],
        archived=[
            {
                "sessionId": "a",
                "archivedAt": OLD,
                "reason": "archived",
                "archivePath": str(jsonl),
                "harvestPath": None,
                "summary": "s",
                "project": "repo",
            }
        ],
    )

    assert roundup_cmd.cmd_roundup(["--registry", str(path), "--yes"]) == 0

    assert not jsonl.exists()
    assert _saved(path)["deleted"][0]["reason"] == "purged"


def test_roundup_archives_a_quiet_session_marked_finish_on_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(roundup_cmd, "active_model", lambda cfg, task: None)
    projects = tmp_path / "projects" / "-w-x"
    projects.mkdir(parents=True)
    transcript = projects / "s1.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hi"}}\n')
    when = time.time() - 600
    os.utime(transcript, (when, when))
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(tmp_path / "projects"))
    path = tmp_path / "registry.json"
    path.write_text(
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

    assert roundup_cmd.cmd_roundup(["--registry", str(path), "--yes"]) == 0

    assert [a["sessionId"] for a in _saved(path)["archived"]] == ["s1"]
    assert not transcript.exists()


@pytest.mark.usefixtures("no_scan")
def test_interrupt_after_the_stamp_exits_zero(tmp_path, monkeypatch):
    path = _write(tmp_path, [_thread(tmp_path, "a")])
    monkeypatch.setattr(roundup_cmd, "active_model", lambda cfg, task: None)
    real = roundup_api.stamp

    def stamp_then_cancel(registry_path, now_iso):
        real(registry_path, now_iso)
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(roundup_api, "stamp", stamp_then_cancel)

    assert roundup_cmd.cmd_roundup(["--registry", str(path), "--yes"]) == 0
    assert _saved(path)["lastRoundupAt"]
