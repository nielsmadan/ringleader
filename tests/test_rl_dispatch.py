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
        (["session", "delete"], commands.delete.cmd_delete),
        (["session", "snooze"], commands.snooze.cmd_snooze),
        (["session", "wake"], commands.wake.cmd_wake),
        (["session", "refresh"], commands.sync.cmd_refresh),
        (["session", "set"], commands.sync.cmd_set),
        (["session", "name"], commands.name.cmd_name),
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
    ],
)
def test_new_paths_resolve(path, expected):
    node = commands.TABLE
    for tok in path:
        node = node[tok]
    assert node is expected


def test_group_with_no_subcommand_lists_subs(capsys):
    rc = main(["session"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "archive" in out and "snooze" in out


def test_unknown_subcommand_returns_2(capsys):
    rc = main(["session", "bogus"])
    assert rc == 2
    assert "bogus" in capsys.readouterr().err


@pytest.mark.parametrize("path", [["session", "enrich"], ["session", "complete"], ["config"]])
def test_reserved_verbs_return_2(path, capsys):
    rc = main(path)
    assert rc == 2
    assert "not yet implemented" in capsys.readouterr().err


def test_help_lists_groups(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "session" in out and "mem" in out and "data" in out


def test_help_marks_reserved_verbs_coming_soon(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    session_line = next(line for line in out.splitlines() if line.strip().startswith("session:"))
    assert "enrich" in session_line
    assert "(coming soon)" in session_line


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
