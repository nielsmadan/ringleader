import json

from ringleader import registry
from ringleader.commands import enrich as enrich_mod
from ringleader.models.base import ModelError

from tui_e2e import write_claude_session


class FakeModel:
    name = "fake"

    def __init__(self, out=None, raise_on=None):
        self._out = out or {
            "status": "finished",
            "whatItIs": "did a thing",
            "suggestedName": "did-a-thing",
        }
        self._raise_on = raise_on or set()
        self.prompts = []

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        self.prompts.append(prompt)
        if any(k in prompt for k in self._raise_on):
            raise ModelError("boom")
        return self._out


def _session(tmp_path, sid="s1", prompt="fix the login bug"):
    project = tmp_path / "work" / "proj"
    project.mkdir(parents=True, exist_ok=True)
    return write_claude_session(tmp_path / "_claude", project, sid, prompt)


def test_enrich_classifies_pending_from_transcript_on_disk(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "registry.json"
    _session(tmp_path)
    model = FakeModel()
    monkeypatch.setattr(enrich_mod, "active_model", lambda cfg, task: model)

    assert enrich_mod.cmd_enrich(["--registry", str(reg)]) == 0

    t = registry.find_session(registry.load_registry(reg), "claude-code:s1")
    assert (t["status"], t["whatItIs"], t["suggestedName"]) == (
        "finished",
        "did a thing",
        "did-a-thing",
    )
    assert t["classifiedAtMtime"] == t["mtime"]
    assert "fix the login bug" in model.prompts[0]
    assert "enriched 1, skipped 0 (model: fake (default model))" in capsys.readouterr().out


def test_enrich_no_model_is_noop(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "registry.json"
    _session(tmp_path)
    monkeypatch.setattr(enrich_mod, "active_model", lambda cfg, task: None)
    rc = enrich_mod.cmd_enrich(["--registry", str(reg)])
    assert rc == 0
    assert "no model configured" in capsys.readouterr().out.lower()


def test_enrich_skips_on_model_error(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "registry.json"
    _session(tmp_path, prompt="EXPLODE now")
    monkeypatch.setattr(
        enrich_mod, "active_model", lambda cfg, task: FakeModel(raise_on={"EXPLODE"})
    )
    rc = enrich_mod.cmd_enrich(["--registry", str(reg)])
    assert rc == 0

    assert json.loads(reg.read_text())["threads"][0]["status"] == "unclassified"
    captured = capsys.readouterr()
    assert "skipped s1: boom" in captured.err
    assert "enriched 0, skipped 1" in captured.out


def test_enrich_skips_cached_threads_from_an_unavailable_source(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "registry.json"
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "lastScan": None,
                "archived": [],
                "deleted": [],
                "threads": [
                    {
                        "source": "opencode",
                        "sessionId": "ses_1",
                        "file": None,
                        "mtime": "2026-08-30T00:00:00+00:00",
                        "status": "unclassified",
                    }
                ],
            }
        )
    )

    def unavailable_sources(*, unavailable_sources, recorded_threads):
        assert [thread["sessionId"] for thread in recorded_threads] == ["ses_1"]
        unavailable_sources.add("opencode")
        return []

    class FailIfCalled(FakeModel):
        def complete_json(self, prompt, schema):
            raise AssertionError("unavailable transcript was sent to the model")

    monkeypatch.setattr("ringleader.scanner.active_sources", unavailable_sources)
    monkeypatch.setattr("ringleader.scanner.scan", lambda **kwargs: [])
    monkeypatch.setattr(enrich_mod, "active_model", lambda cfg, task: FailIfCalled())

    assert enrich_mod.cmd_enrich(["--registry", str(reg)]) == 0

    thread = json.loads(reg.read_text())["threads"][0]
    assert thread["status"] == "unclassified"
    assert "enriched 0, skipped 1" in capsys.readouterr().out
