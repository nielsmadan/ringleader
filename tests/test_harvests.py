import pytest
from ringleader import harvests

LINE_BOUNDARIES = ("\r", "\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")


def test_write_and_load_harvest(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    p = harvests.write_harvest(
        session_id="s1",
        name="fix-auth",
        folder_path="/repo",
        repo_url="git@github.com:me/repo.git",
        harvested_at="2026-01-01T00:00:00+00:00",
        content="## Summary\n\ndid the auth fix\n",
    )
    assert p.exists() and p.parent == harvests.harvests_dir()
    got = harvests.load_harvests()
    assert len(got) == 1
    h = got[0]
    assert h.session_id == "s1" and h.repo_url == "git@github.com:me/repo.git"
    assert h.folder_path == "/repo" and "did the auth fix" in h.content


def test_load_empty_store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert harvests.load_harvests() == []


def test_empty_name_round_trips_to_empty_string(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    harvests.write_harvest(
        session_id="s2",
        name="",
        folder_path="/repo",
        repo_url="git@github.com:me/repo.git",
        harvested_at="2026-01-01T00:00:00+00:00",
        content="body\n",
    )
    got = harvests.load_harvests()
    assert len(got) == 1
    assert got[0].name == ""
    assert isinstance(got[0].name, str)


def test_same_session_writes_distinct_exact_items(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    first = harvests.write_harvest(
        session_id="s1",
        name="n",
        folder_path="/repo",
        repo_url="github.com/me/repo",
        harvested_at="t1",
        content="first",
        clone_url="git@github.com:me/repo.git",
    )
    second = harvests.write_harvest(
        session_id="s1",
        name="n",
        folder_path="/repo",
        repo_url="github.com/me/repo",
        harvested_at="t2",
        content="second",
        clone_url="git@github.com:me/repo.git",
    )
    snapshot = harvests.snapshot_harvests()
    assert first != second
    assert len(snapshot.items) == 2
    assert len({item.harvest_id for item in snapshot.items}) == 2
    assert all(item.token == f"{item.harvest_id}:{item.digest}" for item in snapshot.items)
    assert {item.clone_url for item in snapshot.items} == {"git@github.com:me/repo.git"}


def test_legacy_id_is_stable_without_rewriting(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path = harvests.harvests_dir() / "legacy.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nsessionId: s1\nrepoUrl: github.com/me/repo\n---\n\nbody\n")
    before = path.read_bytes()
    first = harvests.snapshot_harvests().items[0]
    second = harvests.snapshot_harvests().items[0]
    assert first.harvest_id == second.harvest_id
    assert first.harvest_id.startswith("legacy-")
    assert path.read_bytes() == before


def test_snapshot_rejects_explicit_empty_harvest_id(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    path = harvests.harvests_dir() / "invalid.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nharvestId: \nsessionId: s1\n---\n\nbody\n")
    snapshot = harvests.snapshot_harvests()
    assert snapshot.items == ()
    assert [problem.path for problem in snapshot.problems] == [path]


def test_snapshot_reports_malformed_file_and_keeps_valid_items(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    harvests.write_harvest(
        session_id="s1",
        name="n",
        folder_path="/repo",
        repo_url=None,
        harvested_at="t",
        content="valid",
    )
    bad = harvests.harvests_dir() / "bad.md"
    bad.write_bytes(b"\xff")
    snapshot = harvests.snapshot_harvests()
    assert len(snapshot.items) == 1
    assert [problem.path for problem in snapshot.problems] == [bad]


@pytest.mark.parametrize(
    "field",
    ("session_id", "name", "folder_path", "repo_url", "clone_url", "harvested_at"),
)
@pytest.mark.parametrize("unsafe", (*LINE_BOUNDARIES, "\x00"))
def test_write_rejects_line_unsafe_metadata_before_creating_an_item(
    tmp_path, monkeypatch, field, unsafe
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    values = {
        "session_id": "session-1",
        "name": "safe-name",
        "folder_path": "/repo",
        "repo_url": "github.com/me/repo",
        "clone_url": "git@github.com:me/repo.git",
        "harvested_at": "2026-08-29T00:00:00+00:00",
    }
    values[field] = f"before{unsafe}after"

    with pytest.raises(ValueError, match=field):
        harvests.write_harvest(content="arbitrary\n---\nMarkdown", **values)

    assert harvests.snapshot_harvests().items == ()
    assert not harvests.harvests_dir().exists()


@pytest.mark.parametrize("separator", LINE_BOUNDARIES)
def test_write_rejects_frontmatter_identity_injection(tmp_path, monkeypatch, separator):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    with pytest.raises(ValueError, match="session_id"):
        harvests.write_harvest(
            session_id=(f"trusted{separator}sessionId: attacker{separator}harvestId: attacker"),
            name="safe-name",
            folder_path="/repo",
            repo_url="github.com/me/repo",
            clone_url=None,
            harvested_at="2026-08-29T00:00:00+00:00",
            content="body",
        )

    assert harvests.snapshot_harvests().items == ()
    assert not harvests.harvests_dir().exists()


def test_write_preserves_line_boundaries_inside_markdown_content(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    content = f"before{''.join(LINE_BOUNDARIES)}after"

    path = harvests.write_harvest(
        session_id="trusted",
        name="safe-name",
        folder_path="/repo",
        repo_url="github.com/me/repo",
        clone_url=None,
        harvested_at="2026-08-29T00:00:00+00:00",
        content=content,
    )

    assert harvests.load_harvest_path(path).content == content
