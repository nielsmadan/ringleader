from dataclasses import replace
from pathlib import Path

from ringleader.harvests import Harvest
from ringleader.home.harvest_view import harvest_groups, preview_line, publish_all_cwd
from ringleader.memory_types import MemoryOrigin, SessionOrigin


def _harvest(sid, *, folder=None, repo=None, content="body"):
    return Harvest(
        path=Path(f"/h/{sid}.md"),
        harvest_id=sid,
        digest="d",
        origin=SessionOrigin(sid),
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


def test_memory_harvests_group_by_checkout_even_with_shared_remote(tmp_path):
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    for checkout in (a, b, c):
        checkout.mkdir()
    remote = "git@github.com:me/app.git"
    memory_a, memory_b = (
        replace(
            _harvest(f"memory-{checkout.name}", folder=str(checkout), repo=remote),
            origin=MemoryOrigin(checkout.name, "codex", "fp", "incarnation", "global", None),
        )
        for checkout in (a, b)
    )
    session_a = _harvest("session-a", folder=str(a), repo=remote)
    session_c = _harvest("session-c", folder=str(c), repo=remote)

    groups = harvest_groups((session_c, memory_a, session_a, memory_b))

    assert [(group.folder, group.items) for group in groups] == [
        (str(a), (memory_a, session_a)),
        (str(b), (memory_b,)),
        (str(c), (session_c,)),
    ]


def test_mixed_harvest_group_uses_memory_destination(tmp_path):
    old = tmp_path / "old"
    selected = tmp_path / "selected"
    old.mkdir()
    selected.mkdir()
    remote = "https://github.com/me/app"
    session = _harvest("s1", folder=str(old), repo=remote)
    memory = replace(
        _harvest("memory", folder=str(selected), repo=remote),
        origin=MemoryOrigin("memory", "codex", "fp", "incarnation", "project", str(old)),
    )

    (group,) = harvest_groups((session, memory))

    assert (group.label, group.folder, group.items) == (
        "selected",
        str(selected),
        (session, memory),
    )


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
