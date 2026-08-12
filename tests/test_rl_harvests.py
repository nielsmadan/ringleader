def test_harvests_lists_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader import harvests
    from ringleader.commands import harvests as cmd

    harvests.write_harvest(
        session_id="s1",
        name="fix-auth",
        folder_path="/repo",
        repo_url="git@github.com:me/repo.git",
        harvested_at="2026-01-01T00:00:00+00:00",
        content="## Summary\n\nx\n",
    )
    rc = cmd.cmd_harvests([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fix-auth" in out and "git@github.com:me/repo.git" in out and "/repo" in out


def test_harvests_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    import json

    from ringleader import harvests
    from ringleader.commands import harvests as cmd

    harvests.write_harvest(
        session_id="s1",
        name="n",
        folder_path="/r",
        repo_url="u",
        harvested_at="t",
        content="body text",
    )
    rc = cmd.cmd_harvests(["--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["repoUrl"] == "u" and "body text" in data[0]["content"]


def test_harvests_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    from ringleader.commands import harvests as cmd

    assert cmd.cmd_harvests([]) == 0
