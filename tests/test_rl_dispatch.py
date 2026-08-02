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
