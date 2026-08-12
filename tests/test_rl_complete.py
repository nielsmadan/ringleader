import json

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


def _seed(reg, tmp_path):
    f = tmp_path / "s1.jsonl"
    f.write_text('{"type":"user","message":{"content":"hi"}}\n')
    reg.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [
                    {
                        "sessionId": "s1",
                        "file": str(f),
                        "project": "/repo",
                        "gitProjectLabel": "repo",
                        "title": "t",
                    }
                ],
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
    assert any(ts["sessionId"] == "s1" for ts in r["tombstones"])  # archived
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
