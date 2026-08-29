import json
import threading
from contextlib import contextmanager

from ringleader.commands import complete as complete_mod


class FakeModel:
    name = "fake"

    def __init__(self, boom=False):
        self._boom = boom

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        from ringleader.models.base import ModelError

        if self._boom:
            raise ModelError("boom")
        return {"summary": "did it"}


class ControlledPublishLock:
    def __init__(self, writer_name):
        self.writer_name = writer_name
        self.writer_attempted = threading.Event()
        self.lock = threading.Lock()
        self.owner = None

    @contextmanager
    def __call__(self):
        if threading.current_thread().name == self.writer_name:
            self.writer_attempted.set()
        self.lock.acquire()
        self.owner = threading.get_ident()
        try:
            yield
        finally:
            self.owner = None
            self.lock.release()

    def owned_by_current_thread(self):
        return self.owner == threading.get_ident()


def _seed(reg, tmp_path, session_ids=("s1",)):
    threads = []
    for session_id in session_ids:
        f = tmp_path / f"{session_id}.jsonl"
        f.write_text('{"type":"user","message":{"content":"hi"}}\n')
        threads.append(
            {
                "sessionId": session_id,
                "file": str(f),
                "project": "/repo",
                "gitProjectLabel": "repo",
                "title": f"title-{session_id}",
            }
        )
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": threads,
                "tombstones": [],
            }
        )
    )


def test_complete_harvests_then_archives(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"
    _seed(reg, tmp_path)
    monkeypatch.setattr(complete_mod.harvest_mod, "active_model", lambda cfg: FakeModel())
    monkeypatch.setattr(complete_mod.harvest_mod.export, "render_conversation", lambda p: "hi")
    rc = complete_mod.cmd_complete(["s1", "--registry", str(reg)])
    assert rc == 0
    r = json.loads(reg.read_text())
    assert r["threads"] == [] or all(t["sessionId"] != "s1" for t in r["threads"])
    assert any(ts["sessionId"] == "s1" for ts in r["archived"])  # archived
    from ringleader import harvests

    assert len(harvests.load_harvests()) == 1


def test_complete_no_model_does_not_archive(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"
    _seed(reg, tmp_path)
    monkeypatch.setattr(complete_mod.harvest_mod, "active_model", lambda cfg: None)
    rc = complete_mod.cmd_complete(["s1", "--registry", str(reg)])
    assert rc != 0 or "no model" in capsys.readouterr().out.lower()
    r = json.loads(reg.read_text())
    assert any(t["sessionId"] == "s1" for t in r["threads"])  # still live, not archived


def test_complete_and_concurrent_harvest_preserve_both_registry_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"
    _seed(reg, tmp_path, ("s1", "s2"))
    monkeypatch.setattr(complete_mod.harvest_mod, "active_model", lambda cfg: FakeModel())
    monkeypatch.setattr(complete_mod.harvest_mod.export, "render_conversation", lambda path: "hi")
    complete_loaded = threading.Event()
    writer_done = threading.Event()
    controlled_lock = ControlledPublishLock("harvest-writer")
    monkeypatch.setattr(complete_mod, "publish_lock", controlled_lock, raising=False)
    monkeypatch.setattr(complete_mod.harvest_mod, "publish_lock", controlled_lock)
    real_load = complete_mod.registry.load_registry
    complete_loads = 0

    def controlled_load(path):
        nonlocal complete_loads
        loaded = real_load(path)
        if threading.current_thread().name == "complete":
            complete_loads += 1
            if complete_loads == 3:
                complete_loaded.set()
                assert controlled_lock.writer_attempted.wait(3)
                if not controlled_lock.owned_by_current_thread():
                    assert writer_done.wait(3)
        return loaded

    monkeypatch.setattr(complete_mod.registry, "load_registry", controlled_load)
    results = {}

    def run_complete():
        results["complete"] = complete_mod.cmd_complete(["s1", "--registry", str(reg)])

    def run_writer():
        try:
            assert complete_loaded.wait(3)
            results["writer"] = complete_mod.harvest_mod.cmd_harvest(["s2", "--registry", str(reg)])
        finally:
            writer_done.set()

    complete_thread = threading.Thread(target=run_complete, name="complete")
    writer_thread = threading.Thread(target=run_writer, name="harvest-writer")
    complete_thread.start()
    writer_thread.start()
    complete_thread.join(5)
    writer_thread.join(5)

    assert not complete_thread.is_alive()
    assert not writer_thread.is_alive()
    assert results == {"complete": 0, "writer": 0}
    saved = real_load(reg)
    assert [item["sessionId"] for item in saved["threads"]] == ["s2"]
    assert [item["sessionId"] for item in saved["archived"]] == ["s1"]
    assert len(saved["archived"][0]["harvestPath"]) == 1
    assert len(saved["threads"][0]["harvestPath"]) == 1
    from ringleader import harvests

    assert {item.session_id for item in harvests.load_harvests()} == {"s1", "s2"}
