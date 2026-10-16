import json

from ringleader.commands import enrich as enrich_mod
from ringleader.models.base import ModelError
from ringleader.sources.base import NormalizedSession


class FakeModel:
    name = "fake"

    def __init__(self, out=None, raise_on=None):
        self._out = out or {
            "status": "finished",
            "whatItIs": "did a thing",
            "suggestedName": "did-a-thing",
        }
        self._raise_on = raise_on or set()

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        if any(k in prompt for k in self._raise_on):
            raise ModelError("boom")
        return self._out


def _ns(sid, cwd="/p", tail="hello world"):
    return NormalizedSession(
        source="claude-code",
        session_id=sid,
        cwd=cwd,
        mtime="2026-01-01T00:00:00+00:00",
        size_bytes=10,
        custom_title=None,
        summary=None,
        last_prompt="hi",
        file=f"/p/{sid}.jsonl",
        message_count=3,
        tail_text=tail,
    )


def test_enrich_classifies_pending(tmp_path, monkeypatch):
    reg = tmp_path / "registry.json"
    monkeypatch.setattr(enrich_mod.scanner, "scan", lambda **kw: [_ns("s1")])
    monkeypatch.setattr(enrich_mod.scanner, "active_sources", lambda **_kwargs: [])
    monkeypatch.setattr(enrich_mod, "active_model", lambda cfg, task: FakeModel())
    rc = enrich_mod.cmd_enrich(["--registry", str(reg)])
    assert rc == 0

    t = json.loads(reg.read_text())["threads"][0]
    assert t["status"] == "finished" and t["suggestedName"] == "did-a-thing"


def test_enrich_no_model_is_noop(tmp_path, monkeypatch, capsys):
    reg = tmp_path / "registry.json"
    monkeypatch.setattr(enrich_mod.scanner, "scan", lambda **kw: [_ns("s1")])
    monkeypatch.setattr(enrich_mod.scanner, "active_sources", lambda **_kwargs: [])
    monkeypatch.setattr(enrich_mod, "active_model", lambda cfg, task: None)
    rc = enrich_mod.cmd_enrich(["--registry", str(reg)])
    assert rc == 0
    assert "no model configured" in capsys.readouterr().out.lower()


def test_enrich_skips_on_model_error(tmp_path, monkeypatch):
    reg = tmp_path / "registry.json"
    monkeypatch.setattr(enrich_mod.scanner, "scan", lambda **kw: [_ns("s1", tail="EXPLODE now")])
    monkeypatch.setattr(enrich_mod.scanner, "active_sources", lambda **_kwargs: [])
    monkeypatch.setattr(
        enrich_mod, "active_model", lambda cfg, task: FakeModel(raise_on={"EXPLODE"})
    )
    rc = enrich_mod.cmd_enrich(["--registry", str(reg)])
    assert rc == 0

    assert json.loads(reg.read_text())["threads"][0]["status"] == "unclassified"


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

    monkeypatch.setattr(enrich_mod.scanner, "active_sources", unavailable_sources)
    monkeypatch.setattr(enrich_mod.scanner, "scan", lambda **kwargs: [])
    monkeypatch.setattr(enrich_mod, "active_model", lambda cfg, task: FailIfCalled())

    assert enrich_mod.cmd_enrich(["--registry", str(reg)]) == 0

    thread = json.loads(reg.read_text())["threads"][0]
    assert thread["status"] == "unclassified"
    assert "enriched 0, skipped 1" in capsys.readouterr().out
