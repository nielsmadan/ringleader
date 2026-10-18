import pytest
from ringleader import commands
from ringleader.cli import main


def test_unknown_command_returns_2(capsys):
    rc = main(["definitely-not-a-command"])
    assert rc == 2
    assert "unknown command" in capsys.readouterr().err


def test_help_lists_commands(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "overview" in out and "triage" in out


@pytest.mark.parametrize(
    "path,expected",
    [
        (["session", "archive"], commands.archive.cmd_archive),
        (["session", "enrich"], commands.enrich.cmd_enrich),
        (["session", "delete"], commands.delete.cmd_delete),
        (["session", "snooze"], commands.snooze.cmd_snooze),
        (["session", "wake"], commands.wake.cmd_wake),
        (["session", "keep"], commands.keep.cmd_keep),
        (["session", "unkeep"], commands.keep.cmd_unkeep),
        (["session", "refresh"], commands.sync.cmd_refresh),
        (["session", "set"], commands.sync.cmd_set),
        (["session", "name"], commands.name.cmd_name),
        (["session", "search"], commands.search.cmd_search),
        (["session", "relocate"], commands.relocate.cmd_relocate),
        (["session", "mark", "harvested"], commands.mark_harvested.cmd_mark_harvested),
        (["session", "mark", "reviewed"], commands.mark_reviewed.cmd_mark_reviewed),
        (["session", "mark", "cleaned"], commands.cleaned.cmd_cleaned),
        (["mem", "refresh"], commands.mem_scan.cmd_mem_scan),
        (["mem", "prune"], commands.mem_prune.cmd_mem_prune),
        (["mem", "mark", "cleaned"], commands.mem_cleaned.cmd_mem_cleaned),
        (["mem", "mark", "reviewed"], commands.mark_mem_reviewed.cmd_mark_mem_reviewed),
        (["mem", "triage"], commands.triage.cmd_mem_triage),
        (["mem", "overview"], commands.overview.cmd_mem_overview),
        (["data", "export"], commands.export.cmd_export),
        (["data", "tokens"], commands.tokens.cmd_tokens),
        (["data", "migrate"], commands.migrate.cmd_migrate),
        (["data", "presence", "install"], commands.presence.cmd_install),
        (["data", "presence", "status"], commands.presence.cmd_status),
        (["data", "presence", "uninstall"], commands.presence.cmd_uninstall),
        (["harvests"], commands.harvests.cmd_harvests),
        (["config"], commands.config.cmd_config),
        (["session", "complete"], commands.complete.cmd_complete),
    ],
)
def test_new_paths_resolve(path, expected):
    node = commands.TABLE
    for tok in path:
        node = node[tok]
    assert node.resolve() is expected


def test_presence_group_lists_its_verbs(capsys):
    rc = main(["data", "presence"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "install" in out and "status" in out and "uninstall" in out


def test_group_with_no_subcommand_lists_subs(capsys):
    rc = main(["session"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "archive" in out and "snooze" in out


def test_unknown_subcommand_returns_2(capsys):
    rc = main(["session", "bogus"])
    assert rc == 2
    assert "bogus" in capsys.readouterr().err


def test_help_lists_groups(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "session" in out and "mem" in out and "data" in out


@pytest.mark.parametrize(
    "name",
    [
        "sync",
        "archive",
        "delete",
        "snooze",
        "wake",
        "name",
        "relocate",
        "mark-harvested",
        "mark-reviewed",
        "cleaned",
        "mem-scan",
        "mem-prune",
        "mem-cleaned",
        "mark-mem-reviewed",
        "export",
        "tokens",
        "migrate",
    ],
)
def test_old_flat_names_are_unknown(name, capsys):
    rc = main([name])
    assert rc == 2
    assert "unknown command" in capsys.readouterr().err


def test_triage_memory_flag_removed():
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "ringleader", "triage", "--memory"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode != 0
    assert "unrecognized arguments" in r.stderr or "--memory" in r.stderr


def test_every_table_leaf_resolves_to_a_callable():
    """The rows above pin particular wirings by hand; this pins that none is broken,
    without growing a roster that has to be remembered."""
    checked = 0
    stack = [([], commands.TABLE)]
    while stack:
        crumbs, node = stack.pop()
        for key, value in node.items():
            path = [*crumbs, key]
            if isinstance(value, dict):
                stack.append((path, value))
                continue
            checked += 1
            assert callable(value.resolve()), f"rl {' '.join(path)} does not resolve to a callable"
    assert checked > 40, f"only {checked} leaves walked — TABLE shape changed?"


def test_bare_rl_opens_the_inbox_when_allowed(monkeypatch):
    from ringleader import cli

    calls = []
    monkeypatch.setattr(cli, "_opens_inbox", lambda: True)
    monkeypatch.setitem(cli.TABLE, "inbox", lambda argv: calls.append(argv) or 0)
    assert main([]) == 0
    assert calls == [[]]


def test_bare_rl_reports_registry_errors_like_rl_inbox(monkeypatch, capsys):
    from ringleader import cli, registry

    def conflict(argv):
        raise registry.RegistryConflict("registry changed")

    monkeypatch.setattr(cli, "_opens_inbox", lambda: True)
    monkeypatch.setitem(cli.TABLE, "inbox", conflict)
    assert main([]) == 1
    assert "rl: registry changed" in capsys.readouterr().err


def test_bare_rl_prints_usage_when_the_inbox_is_not_allowed(monkeypatch, capsys):
    from ringleader import cli

    monkeypatch.setattr(cli, "_opens_inbox", lambda: False)
    assert main([]) == 0
    assert "bare `rl` in a terminal opens the inbox" in capsys.readouterr().out


@pytest.mark.parametrize("opens", [True, False])
@pytest.mark.parametrize("flag", ["-h", "--help", "help"])
def test_help_prints_usage_either_way(monkeypatch, capsys, opens, flag):
    from ringleader import cli

    monkeypatch.setattr(cli, "_opens_inbox", lambda: opens)
    assert main([flag]) == 0
    assert "bare `rl` in a terminal opens the inbox" in capsys.readouterr().out


class _Stream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def _terminal(monkeypatch, *, stdin=True, stdout=True, term="xterm-256color"):
    from ringleader import cli, session_env

    monkeypatch.setattr(cli.sys, "stdin", _Stream(stdin))
    monkeypatch.setattr(cli.sys, "stdout", _Stream(stdout))
    monkeypatch.setenv("TERM", term)
    for var in session_env.PROBES.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)


def test_opens_inbox_on_a_plain_terminal(monkeypatch):
    from ringleader import cli

    _terminal(monkeypatch)
    assert cli._opens_inbox() is True


@pytest.mark.parametrize("kwargs", [{"stdin": False}, {"stdout": False}, {"term": "dumb"}], ids=str)
def test_does_not_open_inbox_without_a_real_terminal(monkeypatch, kwargs):
    from ringleader import cli

    _terminal(monkeypatch, **kwargs)
    assert cli._opens_inbox() is False


def test_does_not_open_inbox_inside_an_agent_session(monkeypatch):
    from ringleader import cli

    _terminal(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    assert cli._opens_inbox() is False


def test_stream_without_isatty_is_not_a_terminal():
    from ringleader import cli

    assert cli._is_tty(object()) is False
