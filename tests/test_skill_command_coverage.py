import re

from ringleader import commands

from conftest import SKILLS_ROOT

# Commands no skill has to name. A key covers itself and everything beneath it, so
# "workspace" exempts the whole group. Every entry is a hole in the gate below, which is
# why `test_every_exemption_is_load_bearing` makes an unused one fail.
UNDOCUMENTED_BY_DESIGN = {
    "inbox": "interactive TUI, plus a --json mode for the circus desktop app",
    "resume": "a human re-enters a session; not part of the curation track",
    "start": "a human starts an agent; not part of the curation track",
    "workspace": "relocation track, not the session track",
    "session sweep": "run by the session-end hook, not by an agent",
    "update hooks": "hook lifecycle; the user runs it directly",
    "uninstall hooks": "hook lifecycle; the user runs it directly",
    "uninstall statusline": "statusline lifecycle; the user runs it directly",
}


def _documents(prose: str, command: str) -> bool:
    # bounded on both sides: unbounded, "curl update" documents `update`, and
    # "rl session keeper" documents `rl session keep`
    return re.search(rf"\brl {re.escape(command)}\b", prose) is not None


def _exempt(command: str) -> bool:
    return any(command == key or command.startswith(key + " ") for key in UNDOCUMENTED_BY_DESIGN)


def _prose(skill_dirs) -> str:
    return (SKILLS_ROOT / "PLAYBOOK.md").read_text() + "".join(
        (d / "SKILL.md").read_text() for d in skill_dirs
    )


def test_the_command_surface_is_enumerable():
    """Anchors the gate below: if `command_paths()` silently returned little or nothing,
    every coverage assertion would pass by having nothing to check."""
    paths = commands.command_paths()
    assert len(paths) > 50, f"only {len(paths)} commands enumerated — TABLE shape changed?"
    # one from each dispatch style: nested group, and a sub-verb parsed inside a leaf
    assert "session archive" in paths
    assert "config filter" in paths


def test_every_command_is_documented_somewhere_in_the_skills(skill_dirs):
    """A verb no skill prose names is a verb no skill can use. Catches the drift a
    hand-written skill set is prone to: the CLI grows a command and the prose driving it
    never hears about it. The shared playbook and the per-skill SKILL.md files both
    count — `rl session whoami` belongs to ringleader-finish alone."""
    prose = _prose(skill_dirs)
    missing = sorted(
        c for c in commands.command_paths() if not _exempt(c) and not _documents(prose, c)
    )
    assert not missing, (
        "no skill names: "
        + ", ".join(f"rl {c}" for c in missing)
        + " — document them, or add them to UNDOCUMENTED_BY_DESIGN with a reason"
    )


def test_every_exemption_names_a_real_command():
    """An exemption for a command that no longer exists would silently mask a later
    command of the same name."""
    paths = commands.command_paths()
    stale = sorted(
        key
        for key in UNDOCUMENTED_BY_DESIGN
        if not any(p == key or p.startswith(key + " ") for p in paths)
    )
    assert not stale, f"UNDOCUMENTED_BY_DESIGN names commands that do not exist: {stale}"


def test_every_exemption_is_load_bearing(skill_dirs):
    """An exemption for something the skills already document is dead weight that hides
    the day that documentation disappears."""
    prose = _prose(skill_dirs)
    documented = sorted(c for c in commands.command_paths() if _exempt(c) and _documents(prose, c))
    assert not documented, (
        "exempted but already documented: "
        + ", ".join(f"rl {c}" for c in documented)
        + " — drop the exemption"
    )
