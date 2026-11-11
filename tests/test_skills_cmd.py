import os
from pathlib import Path

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
        for name in ("ringleader-triage", "ringleader-publish"):
            link = base / name
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


def test_agent_detection_survives_a_stat_denial(tmp_path, monkeypatch, capsys):
    """An unstattable `~/.pi` used to crash `rl doctor` and `rl update all --yes`.

    Mocked rather than chmod'd because no permission bit makes one sibling
    unstattable while another stays readable — stat is gated by the shared parent."""
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    real_stat = os.stat
    denied = str(home / ".pi")

    def fake_stat(path, *args, **kwargs):
        if str(path) == denied:
            raise PermissionError(13, "Permission denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)

    assert skills.cmd_status([]) == 0
    out = capsys.readouterr()
    assert "unreadable" in (out.out + out.err).lower()
    assert str(home / ".claude" / "skills") in out.out  # the readable agent still detected


def test_status_reports_linked(tmp_path, monkeypatch, capsys):
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    skills.cmd_status([])
    out = capsys.readouterr().out
    assert "ringleader-triage" in out and "linked" in out.lower()


def test_status_reports_unreadable_entries_without_crashing(tmp_path, monkeypatch, capsys):
    """A sandbox that grants the skills dir itself but denies stat on a specific entry
    inside it (the real shape of this machine's nono sandbox) must not crash `rl doctor`
    — and must say `unreadable`, not `missing`, since those mean different things."""
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    skdir = home / ".claude" / "skills"
    os.chmod(skdir, 0o000)  # no execute bit -> children can't be lstat'd
    try:
        rc = skills.cmd_status([])
    finally:
        os.chmod(skdir, 0o700)
    assert rc == 0
    out = capsys.readouterr().out
    # the per-skill line, not a substring of the whole output: pytest names the temp dir
    # after the test, so "unreadable" appears in every printed path regardless
    assert "ringleader-triage: unreadable" in out


def test_uninstall_and_update_do_not_crash_on_unreadable_entries(tmp_path, monkeypatch):
    """`_owned` (reached via `_managed_links`) moved its `is_symlink()` call inside its
    try/except so a per-entry stat denial can't crash it — but unlike `cmd_status`,
    `_managed_links` must list the directory successfully first, so a plain chmod on the
    dir can't reproduce "listing works, one entry's stat doesn't" (chmod bits gate both
    the same way). Monkeypatch one entry's `is_symlink()` instead, matching this
    machine's real nono sandbox shape where a directory is listable but a specific named
    path inside it is denied."""
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])

    real_is_symlink = Path.is_symlink

    def flaky_is_symlink(self):
        if self.name == "ringleader-triage":
            raise PermissionError(1, "Operation not permitted")
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", flaky_is_symlink)

    assert isinstance(skills.cmd_update(["--yes"]), int)
    assert isinstance(skills.cmd_uninstall(["--yes"]), int)


def test_uninstall_and_update_report_unreadable_directory(tmp_path, monkeypatch, capsys):
    """A directory that denies *listing* (not just per-entry stat) must not crash
    `rl uninstall skills` / `rl update skills`, and must be reported as unreadable —
    not silently treated as "nothing installed", which is a different (and possibly
    false) claim. This matters because `just install` ends in an unattended
    `rl update all --yes`.

    An unreadable directory is an environment condition, not a failure of the
    operation the caller asked for — it must not turn the exit code nonzero on its
    own, or that same unattended `rl update all --yes` fails the whole install
    recipe over a sandbox that simply hides part of $HOME."""
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    skdir = home / ".claude" / "skills"
    os.chmod(skdir, 0o000)  # no read/execute -> iterdir() itself fails
    try:
        rc_uninstall = skills.cmd_uninstall(["--yes"])
        out_uninstall = capsys.readouterr()
        rc_update = skills.cmd_update(["--yes"])
        out_update = capsys.readouterr()
    finally:
        os.chmod(skdir, 0o700)

    assert rc_uninstall == 0
    assert "unreadable" in out_uninstall.err.lower()
    assert "nothing installed" not in out_uninstall.out.lower()

    assert rc_update == 0
    assert "unreadable" in out_update.err.lower()
    assert "no ringleader skills installed" not in out_update.out.lower()


def test_update_reports_unreadable_target_directory_without_crashing(tmp_path, monkeypatch, capsys):
    """The actual crash seen live: pathlib's `is_dir()`/`iterdir()` only swallow
    ENOENT-shaped errors and re-raise a genuine permission denial, so a directory the
    sandbox blocks outright (not just its listing) raised straight out of
    `_managed_links`'s `d.is_dir()` check before `cmd_update` ever reached `iterdir()`
    — this is the exact path `just install`'s trailing `rl update all --yes` hit.
    Reproduced by stripping traversal permission on the *parent* of the target skills
    dir: `is_dir()` on the child then fails to stat at all, without touching the
    child's own permission bits (chmod on the child itself can't produce this shape,
    since stat only needs execute permission on the parent)."""
    home = _fake_home(tmp_path, ".codex")  # sole target: the shared ~/.agents/skills
    monkeypatch.setenv("HOME", str(home))
    shared_parent = home / ".agents"
    shared_parent.mkdir()
    target = shared_parent / "skills"
    os.chmod(shared_parent, 0o000)
    try:
        rc = skills.cmd_update(["--yes"])
    finally:
        os.chmod(shared_parent, 0o700)

    assert rc == 0
    err = capsys.readouterr().err
    assert "unreadable" in err.lower()
    assert str(target) in err


def test_update_succeeds_when_only_one_target_directory_is_unreadable(
    tmp_path, monkeypatch, capsys
):
    """The mixed case: one readable target dir holding owned links, plus one the
    sandbox hides. The readable dir carries `cmd_update` past its early return into
    the relink step, where the hidden dir's own `mkdir` denial used to be counted as a
    link failure and turned `rl update all --yes` nonzero. An unreadable directory is
    an environment condition — it must be named, but never fail the run."""
    home = _fake_home(tmp_path, ".claude", ".codex")
    monkeypatch.setenv("HOME", str(home))
    assert skills.cmd_install(["--yes"]) == 0
    assert (home / ".claude" / "skills" / "ringleader-triage").is_symlink()

    shared_parent = home / ".agents"  # the codex/opencode/pi target's parent
    target = shared_parent / "skills"
    os.chmod(shared_parent, 0o000)
    try:
        rc = skills.cmd_update(["--yes"])
    finally:
        os.chmod(shared_parent, 0o700)

    assert rc == 0
    err = capsys.readouterr().err
    assert "unreadable" in err.lower()
    assert str(target) in err
    assert (home / ".claude" / "skills" / "ringleader-triage").is_symlink()


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


def test_resolved_shows_symlink_target(tmp_path):
    real = (tmp_path / "store").resolve()
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    out = skills._resolved(link)
    assert "->" in out and str(real) in out
    assert "->" not in skills._resolved(real)


def test_install_reports_permission_failure_gracefully(tmp_path, monkeypatch, capsys):
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    skdir = home / ".claude" / "skills"
    skdir.mkdir()
    os.chmod(skdir, 0o500)  # read+execute, no write → symlink creation fails
    try:
        rc = skills.cmd_install(["--yes"])
    finally:
        os.chmod(skdir, 0o700)
    assert rc == 1  # non-zero when a link fails
    assert "could not link" in capsys.readouterr().err


def test_uninstall_reports_permission_failure_gracefully(tmp_path, monkeypatch, capsys):
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    skdir = home / ".claude" / "skills"
    os.chmod(skdir, 0o500)  # can't unlink entries in a non-writable dir
    try:
        rc = skills.cmd_uninstall(["--yes"])
    finally:
        os.chmod(skdir, 0o700)
    assert rc == 1
    assert "could not remove" in capsys.readouterr().err


def test_update_does_not_install_when_nothing_is_wired_up(tmp_path, monkeypatch, capsys):
    """`update` syncs an existing install; it must not opt an agent in on its own —
    install recipes call it unattended."""
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    assert skills.cmd_update(["--yes"]) == 0
    assert "no ringleader skills installed" in capsys.readouterr().out
    assert not (home / ".claude" / "skills").exists()


def test_update_syncs_once_installed(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    stale = home / ".claude" / "skills" / "ringleader-gone"
    stale.symlink_to(skills.bundled_skills_dir() / "ringleader-triage")
    assert skills.cmd_update(["--yes"]) == 0
    assert not stale.exists()
    assert (home / ".claude" / "skills" / "ringleader-triage").is_symlink()


def _droid_skills_dir(tmp_path):
    """Droid's native personal skills dir, under the suite's `FACTORY_HOME_OVERRIDE`
    (see conftest)."""
    return tmp_path / "_factory" / ".factory" / "skills"


def test_install_links_the_bundled_skills_for_droid(tmp_path, monkeypatch):
    """Droid reads its own ~/.factory/skills, just as Claude Code reads ~/.claude/skills."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "_factory" / ".factory").mkdir(parents=True)

    assert skills.cmd_install(["--yes"]) == 0

    for name in ("ringleader-triage", "ringleader-publish"):
        link = _droid_skills_dir(tmp_path) / name
        assert link.is_symlink() and (link / "SKILL.md").exists()


def test_uninstall_leaves_a_foreign_entry_in_droids_skills_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "_factory" / ".factory").mkdir(parents=True)
    droid_skills = _droid_skills_dir(tmp_path)
    droid_skills.mkdir(parents=True)
    (droid_skills / "someone-elses").mkdir()
    skills.cmd_install(["--yes"])

    assert skills.cmd_uninstall(["--yes"]) == 0

    assert [p.name for p in droid_skills.iterdir()] == ["someone-elses"]


def test_droid_is_offered_only_once_its_home_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _fake_home(tmp_path, ".claude")

    assert [label for label, _ in skills._install_dirs(Path.home())] == ["claude"]

    (tmp_path / "_factory" / ".factory").mkdir(parents=True)
    assert skills._install_dirs(Path.home()) == [
        ("claude", tmp_path / ".claude" / "skills"),
        ("droid", _droid_skills_dir(tmp_path)),
    ]


def test_droids_skills_dir_sits_under_the_real_home_without_an_override(tmp_path, monkeypatch):
    """Every user runs the un-overridden branch of `paths.factory_home()`; the suite
    sets FACTORY_HOME_OVERRIDE everywhere else (see conftest)."""
    monkeypatch.delenv("FACTORY_HOME_OVERRIDE")
    monkeypatch.setenv("HOME", str(tmp_path))
    _fake_home(tmp_path, ".factory")

    assert skills._install_dirs(Path.home()) == [("droid", tmp_path / ".factory" / "skills")]


def test_install_links_the_shared_skills_dir_for_cursor_alone(tmp_path, monkeypatch):
    """Cursor reads ~/.agents/skills as its own, so a machine with only Cursor still
    gets the shared dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / "_cursor"  # where conftest points CURSOR_CONFIG_DIR
    config.mkdir()
    (config / "cli-config.json").write_text("{}")

    assert skills.cmd_install(["--yes"]) == 0

    link = tmp_path / ".agents" / "skills" / "ringleader-triage"
    assert link.is_symlink() and (link / "SKILL.md").exists()
