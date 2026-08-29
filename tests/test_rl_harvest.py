import json

from ringleader import harvests
from ringleader.commands import harvest as harvest_mod
from ringleader.models.base import ModelError


class FakeModel:
    name = "fake"

    def __init__(self, out=None, boom=False):
        self._out = out or {"summary": "did the thing", "gotchas": ["watch the cache"]}
        self._boom = boom

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        if self._boom:
            raise ModelError("boom")
        return self._out


def _seed(reg_path, tmp_path):
    f = tmp_path / "s1.jsonl"
    f.write_text('{"type":"user","message":{"content":"hi"}}\n', encoding="utf-8")
    reg_path.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [
                    {
                        "sessionId": "s1",
                        "file": str(f),
                        "project": "/repo",
                        "gitRoot": "/repo",
                        "gitRemote": "git@github.com:me/repo.git",
                        "gitCloneUrl": "git@github.com:me/repo.git",
                        "title": "fix-auth",
                    }
                ],
                "tombstones": [],
            }
        ),
        encoding="utf-8",
    )


def test_harvest_writes_store_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"
    _seed(reg, tmp_path)
    monkeypatch.setattr(harvest_mod, "active_model", lambda cfg: FakeModel())
    monkeypatch.setattr(harvest_mod.export, "render_conversation", lambda p: "## USER\n\nhi\n")
    rc = harvest_mod.cmd_harvest(["s1", "--registry", str(reg)])
    assert rc == 0
    from ringleader import harvests

    got = harvests.load_harvests()
    assert len(got) == 1 and "did the thing" in got[0].content
    assert got[0].repo_url == "git@github.com:me/repo.git"
    assert got[0].clone_url == "git@github.com:me/repo.git"
    # harvested flag set
    assert json.loads(reg.read_text())["threads"][0]["decision"] == "harvested"


def test_harvest_no_model_is_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"
    _seed(reg, tmp_path)
    monkeypatch.setattr(harvest_mod, "active_model", lambda cfg: None)
    rc = harvest_mod.cmd_harvest(["s1", "--registry", str(reg)])
    assert rc == 0 and "no model configured" in capsys.readouterr().out.lower()
    from ringleader import harvests

    assert harvests.load_harvests() == []


def test_harvest_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"
    _seed(reg, tmp_path)
    monkeypatch.setattr(harvest_mod, "active_model", lambda cfg: FakeModel())
    monkeypatch.setattr(harvest_mod.export, "render_conversation", lambda p: "hi")
    rc = harvest_mod.cmd_harvest(["s1", "--dry-run", "--registry", str(reg)])
    assert rc == 0
    from ringleader import harvests

    assert harvests.load_harvests() == []


def test_harvest_appends_to_existing_session_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"
    _seed(reg, tmp_path)
    monkeypatch.setattr(harvest_mod, "active_model", lambda cfg: FakeModel())
    monkeypatch.setattr(harvest_mod.export, "render_conversation", lambda p: "## USER\n\nhi\n")
    assert harvest_mod.cmd_harvest(["s1", "--registry", str(reg)]) == 0
    assert harvest_mod.cmd_harvest(["s1", "--registry", str(reg)]) == 0
    assert len(json.loads(reg.read_text())["threads"][0]["harvestPath"]) == 2


def _registry(tmp_path, session_id="s1"):
    p = tmp_path / "registry.json"
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [
                    {
                        "sessionId": session_id,
                        "file": str(tmp_path / "t.jsonl"),
                        "title": "wired the sweep",
                        "project": "/w/ringleader",
                        "gitRoot": "/w/ringleader",
                        "gitRemote": None,
                    }
                ],
                "archived": [],
                "deleted": [],
            }
        )
    )
    return p


def test_from_file_stores_content_verbatim_without_a_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg = _registry(tmp_path)
    md = tmp_path / "h.md"
    md.write_text("# wired the sweep\n\n## Summary\n\nmtime polling beats an fd.\n")

    assert harvest_mod.cmd_harvest(["s1", "--registry", str(reg), "--from-file", str(md)]) == 0

    stored = harvests.load_harvests()
    assert len(stored) == 1
    assert "mtime polling beats an fd." in stored[0].content
    assert json.loads(reg.read_text())["threads"][0]["decision"] == "harvested"


def test_from_file_reads_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg = _registry(tmp_path)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("# t\n\nfrom a pipe.\n"))

    assert harvest_mod.cmd_harvest(["s1", "--registry", str(reg), "--from-file", "-"]) == 0
    assert "from a pipe." in harvests.load_harvests()[0].content


def test_from_file_rejects_empty_content(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    reg = _registry(tmp_path)
    md = tmp_path / "h.md"
    md.write_text("   \n")

    assert harvest_mod.cmd_harvest(["s1", "--registry", str(reg), "--from-file", str(md)]) == 1
    assert harvests.load_harvests() == []


def test_current_resolves_the_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    reg = _registry(tmp_path)
    md = tmp_path / "h.md"
    md.write_text("# t\n\nresolved.\n")

    assert (
        harvest_mod.cmd_harvest(["--current", "--registry", str(reg), "--from-file", str(md)]) == 0
    )
    assert "resolved." in harvests.load_harvests()[0].content
