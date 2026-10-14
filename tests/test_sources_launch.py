import pytest
from ringleader import scanner
from ringleader.sources.base import SessionLaunchingSource

RESUME = {
    "claude-code": ("claude", "--resume", "SID"),
    "codex": ("codex", "resume", "SID"),
    "droid": ("droid", "resume", "SID"),
    "opencode": ("opencode", "--session", "SID"),
    "pi": ("pi", "--session", "SID"),
}

LAUNCH = {
    "claude-code": ("claude", "read the file"),
    "codex": ("codex", "read the file"),
    "droid": ("droid", "read the file"),
    "opencode": ("opencode", "--prompt", "read the file"),
    "pi": ("pi", "--", "read the file"),
}


def _sources():
    return {cls().name: cls() for cls in scanner.SOURCE_TYPES}


@pytest.mark.parametrize("name", sorted(RESUME))
def test_every_source_declares_and_implements_launching(name):
    src = _sources()[name]
    assert scanner.supports(src, "launches_sessions", SessionLaunchingSource)


def test_the_launch_capability_covers_every_scanned_source():
    """A new Source that ringleader scans but cannot launch would show up in the resume
    picker as an unresumable row, so the capability has to keep pace with SOURCE_TYPES."""
    assert set(_sources()) == set(RESUME)


@pytest.mark.parametrize("name", sorted(RESUME))
def test_resume_argv_matches_the_agent_cli(name):
    assert _sources()[name].resume_argv("SID") == RESUME[name]


@pytest.mark.parametrize("name", sorted(LAUNCH))
def test_launch_argv_carries_the_prompt(name):
    assert _sources()[name].launch_argv("read the file") == LAUNCH[name]


def test_pi_terminates_its_options_so_a_prompt_is_not_read_as_a_flag():
    """`pi [options] [--] [@files...] [messages...]` — without the terminator a prompt
    opening with `-` is read as a flag. `@files` follow `--`, so it does not shield those."""
    argv = _sources()["pi"].launch_argv("-p please continue")
    assert argv == ("pi", "--", "-p please continue")


@pytest.mark.parametrize("name", sorted(RESUME))
def test_argv_starts_with_the_binary_and_holds_no_directory(name):
    """The caller chdirs before exec, so a cwd baked into argv would be a second,
    conflicting answer about where the agent starts."""
    src = _sources()[name]
    for argv in (src.resume_argv("SID"), src.fork_argv("SID"), src.launch_argv("p")):
        assert argv[0] in ("claude", "codex", "droid", "opencode", "pi")
        assert all("/" not in part for part in argv[1:])


@pytest.mark.parametrize("name", sorted(RESUME))
def test_a_session_id_is_passed_as_one_argv_element(name):
    """Not a shell string: an id or prompt containing a space must survive as a single
    argument rather than being re-split by a shell that never runs."""
    src = _sources()[name]
    assert "weird id" in src.resume_argv("weird id")
    assert "weird id" in src.fork_argv("weird id")
    assert "two words" in src.launch_argv("two words")


FORK = {
    "claude-code": ("claude", "--resume", "SID", "--fork-session"),
    "codex": ("codex", "fork", "SID"),
    "droid": ("droid", "resume", "SID", "--fork"),
    "opencode": ("opencode", "--session", "SID", "--fork"),
    "pi": ("pi", "--fork", "SID"),
}


@pytest.mark.parametrize("name", sorted(RESUME))
def test_fork_argv_matches_the_agent_cli(name):
    assert _sources()[name].fork_argv("SID") == FORK[name]
