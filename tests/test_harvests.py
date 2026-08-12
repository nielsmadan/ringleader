def test_write_and_load_harvest(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader import harvests

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
    from ringleader import harvests

    assert harvests.load_harvests() == []


def test_empty_name_round_trips_to_empty_string(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader import harvests

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
