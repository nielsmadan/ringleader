import json

from ringleader.commands import relocate
from ringleader.gitinfo import GitProject


def test_relocate_preserves_raw_clone_url(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({"version": 2, "threads": [{"sessionId": "s1"}]}))
    repo = tmp_path / "repo"
    repo.mkdir()
    clone_url = "ssh://git@gitlab.example.com:2222/team/service.git"
    monkeypatch.setattr(
        relocate.gitinfo,
        "resolve_git_project",
        lambda _: GitProject(
            root=str(repo),
            remote="gitlab.example.com/team/service",
            key="gitlab.example.com/team/service",
            label="service",
            clone_url=clone_url,
        ),
    )

    assert relocate.cmd_relocate(["s1", str(repo), "--registry", str(registry_path)]) == 0
    assert json.loads(registry_path.read_text())["threads"][0]["gitCloneUrl"] == clone_url
