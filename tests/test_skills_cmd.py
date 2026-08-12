from ringleader.commands import skills


def _fake_home(tmp_path, *agents):
    for a in agents:
        (tmp_path / a).mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_install_links_into_agents_dirs(tmp_path, monkeypatch, capsys):
    home = _fake_home(tmp_path, ".claude", ".codex")
    monkeypatch.setenv("HOME", str(home))
    assert skills.cmd_install(["--yes"]) == 0
    # claude gets ~/.claude/skills, codex the shared ~/.agents/skills
    for base in (home / ".claude" / "skills", home / ".agents" / "skills"):
        link = base / "ringleader-triage"
        assert link.is_symlink() and (link / "SKILL.md").exists()


def test_install_is_idempotent(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    before = sorted((home / ".claude" / "skills").iterdir())
    skills.cmd_install(["--yes"])
    assert sorted((home / ".claude" / "skills").iterdir()) == before


def test_install_no_agents_is_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))  # no agent dirs
    assert skills.cmd_install(["--yes"]) == 0
    assert "no supported agents" in capsys.readouterr().out.lower()


def test_status_reports_linked(tmp_path, monkeypatch, capsys):
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    skills.cmd_status([])
    out = capsys.readouterr().out
    assert "ringleader-triage" in out and "linked" in out.lower()


def test_owned(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    (bundled / "ringleader-triage").mkdir(parents=True)
    monkeypatch.setattr(skills, "bundled_skills_dir", lambda: bundled)

    links = tmp_path / "links"
    links.mkdir()

    # genuine link into the bundled dir -> owned
    real = links / "ringleader-triage"
    real.symlink_to(bundled / "ringleader-triage")
    assert skills._owned(real) is True

    # sibling dir whose name merely extends the bundled dir's name -> not owned
    evil_target = tmp_path / "bundled-evil" / "x"
    evil_target.mkdir(parents=True)
    evil = links / "ringleader-evil"
    evil.symlink_to(evil_target)
    assert skills._owned(evil) is False

    # dangling link whose (deleted) target was inside the bundled dir -> owned
    broken = links / "ringleader-broken"
    broken.symlink_to(bundled / "ringleader-ghost")
    assert skills._owned(broken) is True

    # link name doesn't start with the prefix -> not owned
    other = links / "not-ringleader"
    other.symlink_to(bundled / "ringleader-triage")
    assert skills._owned(other) is False

    # a real (non-symlink) dir named like a bundled skill -> not owned
    real_dir = links / "ringleader-foo"
    real_dir.mkdir()
    assert skills._owned(real_dir) is False


def test_uninstall_removes_only_ours(tmp_path, monkeypatch):
    home = tmp_path
    (home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    # a foreign skill sharing the dir must survive
    foreign = home / ".claude" / "skills" / "someone-else"
    foreign.mkdir()
    assert skills.cmd_uninstall(["--yes"]) == 0
    assert not (home / ".claude" / "skills" / "ringleader-triage").exists()
    assert foreign.exists()


def test_update_removes_stale_ringleader_link(tmp_path, monkeypatch):
    home = tmp_path
    (home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    # simulate a previously-installed skill that's no longer bundled
    stale = home / ".claude" / "skills" / "ringleader-gone"
    stale.symlink_to(skills.bundled_skills_dir() / "ringleader-triage")
    assert skills.cmd_update(["--yes"]) == 0
    assert not stale.exists()  # removed (ours, not in bundled set)
    assert (home / ".claude" / "skills" / "ringleader-triage").is_symlink()  # kept
