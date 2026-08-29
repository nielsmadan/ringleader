import pytest
from ringleader.commands import setup


def _spy(calls, name):
    def run(argv):
        calls.append((name, argv))
        return 0

    return run


@pytest.fixture
def nouns(monkeypatch):
    calls = []
    monkeypatch.setattr(
        setup,
        "NOUNS",
        {
            "skills": {
                "install": _spy(calls, "skills.install"),
                "update": _spy(calls, "skills.update"),
                "uninstall": _spy(calls, "skills.uninstall"),
            },
            "statusline": {
                "install": _spy(calls, "statusline.install"),
                "uninstall": _spy(calls, "statusline.uninstall"),
            },
        },
    )
    return calls


def test_bare_install_covers_every_noun(nouns):
    assert setup.cmd_install([]) == 0
    assert [n for n, _ in nouns] == ["skills.install", "statusline.install"]


def test_install_one_noun_passes_flags_through(nouns):
    assert setup.cmd_install(["skills", "--yes"]) == 0
    assert nouns == [("skills.install", ["--yes"])]


def test_bare_update_is_an_error(nouns, capsys):
    assert setup.cmd_update([]) == 2
    assert nouns == []
    assert "which" in capsys.readouterr().err.lower()


def test_update_all_skips_a_noun_with_no_update(nouns, capsys):
    assert setup.cmd_update(["all"]) == 0
    assert [n for n, _ in nouns] == ["skills.update"]
    assert "statusline" in capsys.readouterr().out


def test_unknown_noun_reports_the_valid_ones(nouns, capsys):
    assert setup.cmd_install(["bogus"]) == 2
    assert "skills" in capsys.readouterr().err


def test_nonzero_from_any_noun_propagates(monkeypatch):
    monkeypatch.setattr(
        setup,
        "NOUNS",
        {
            "skills": {"install": lambda argv: 1},
            "statusline": {"install": lambda argv: 0},
        },
    )
    assert setup.cmd_install([]) == 1
