from pathlib import Path

from ringleader.harvests import Harvest
from ringleader.home.harvest_view import harvest_groups, preview_line, publish_all_cwd


def _harvest(sid, *, folder=None, repo=None, content="body"):
    return Harvest(
        path=Path(f"/h/{sid}.md"),
        harvest_id=sid,
        digest="d",
        session_id=sid,
        name=sid,
        folder_path=folder,
        repo_url=repo,
        clone_url=None,
        harvested_at=None,
        content=content,
    )


def test_harvests_group_by_repository_largest_first(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    items = (
        _harvest("a1", folder=str(app), repo="git@github.com:me/app.git"),
        _harvest("a2", folder=str(tmp_path / "gone-clone"), repo="https://github.com/me/app"),
        _harvest("r1", repo="https://github.com/me/remote-only.git"),
        _harvest("n1"),
    )
    groups = harvest_groups(items)
    assert [(g.repo_key, g.label, g.folder, len(g.items)) for g in groups] == [
        ("github.com/me/app", "app", str(app), 2),
        ("github.com/me/remote-only", "me/remote-only", None, 1),
        (None, "no repository", None, 1),
    ]


def test_publish_all_cwd_is_the_common_path_of_existing_folders(tmp_path):
    (tmp_path / "w" / "a").mkdir(parents=True)
    (tmp_path / "w" / "b").mkdir(parents=True)
    items = (
        _harvest("a", folder=str(tmp_path / "w" / "a")),
        _harvest("b", folder=str(tmp_path / "w" / "b")),
        _harvest("c", folder=str(tmp_path / "missing")),
    )
    assert publish_all_cwd(items, Path("/home/me")) == str(tmp_path / "w")


def test_publish_all_cwd_falls_back_to_home(tmp_path):
    home = Path("/home/me")
    assert publish_all_cwd((_harvest("a", folder=str(tmp_path / "missing")),), home) == str(home)
    assert publish_all_cwd((_harvest("a", folder="/"), _harvest("b", folder="/")), home) == str(
        home
    )
    assert publish_all_cwd(
        (_harvest("a", folder=str(tmp_path)), _harvest("b", folder="/")), home
    ) == str(home)


def test_preview_line_is_the_first_prose_line_collapsed():
    item = _harvest("a", content="# Title\n\n   ## Sub\n  first   real\tline \nsecond")
    assert preview_line(item) == "first real line"
