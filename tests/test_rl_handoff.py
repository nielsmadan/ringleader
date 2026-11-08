import json
import os

import pytest
from ringleader import cli
from ringleader.commands import handoff as handoff_cmd
from tests.test_json_commands import gui_world


@pytest.fixture
def world(monkeypatch, tmp_path):
    return gui_world(monkeypatch, tmp_path, threads=12, transcripts=False)


def _reg_arg(fixture):
    return ["--registry", str(fixture.registry_path)]


def _first_project(fixture, capsys):
    assert cli.main(["inbox", "--json", *_reg_arg(fixture)]) == 0
    return json.loads(capsys.readouterr().out)["home"]["projects"][0]


def _handoff(fixture, capsys, *argv):
    code = cli.main(["handoff", "triage", *argv, *_reg_arg(fixture)])
    out, err = capsys.readouterr()
    return code, out, err


def _missing(_name):
    return None


def test_dry_run_json_returns_the_directory_and_argv(world, capsys):
    key = _first_project(world, capsys)["key"]
    code, out, err = _handoff(
        world, capsys, "--project", key, "--agent", "claude", "--dry-run", "--json"
    )
    doc = json.loads(out)
    assert (code, err) == (0, "")
    assert (doc["kind"], doc["purpose"], doc["status"], doc["code"], doc["reason"]) == (
        "handoff",
        "triage",
        "ok",
        None,
        None,
    )
    assert (doc["source"], doc["agent"], doc["project_key"]) == ("claude-code", "claude", key)
    assert doc["argv"][0] == "claude"
    assert "ringleader-triage" in doc["argv"][1]
    assert os.path.isdir(doc["cwd"])


def test_project_accepts_a_label_that_only_one_waiting_project_has(world, capsys):
    project = _first_project(world, capsys)
    code, out, _ = _handoff(
        world, capsys, "--project", project["label"], "--agent", "claude", "--dry-run", "--json"
    )
    doc = json.loads(out)
    assert (code, doc["status"], doc["project_key"]) == (0, "ok", project["key"])


def test_dry_run_json_refuses_a_project_with_nothing_to_triage(world, capsys):
    code, out, _ = _handoff(
        world, capsys, "--project", "/nowhere", "--agent", "claude", "--dry-run", "--json"
    )
    doc = json.loads(out)
    assert code == 0
    assert (doc["status"], doc["code"], doc["reason"], doc["cwd"], doc["argv"]) == (
        "refused",
        "nothing_to_triage",
        "no sessions of /nowhere are waiting for triage",
        None,
        None,
    )


def test_dry_run_json_refuses_an_agent_that_is_not_installed(world, capsys):
    key = _first_project(world, capsys)["key"]
    argv = ["--project", key, "--agent", "claude", "--dry-run", "--json", *_reg_arg(world)]
    code = handoff_cmd.cmd_triage(argv, which=_missing)
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (doc["status"], doc["code"], doc["reason"], doc["cwd"], doc["argv"]) == (
        "refused",
        "agent_not_installed",
        "claude is not installed — put it on PATH first",
        None,
        None,
    )


def test_plain_refusal_of_a_missing_agent_explains_itself_on_stderr(world, capsys):
    key = _first_project(world, capsys)["key"]
    argv = ["--project", key, "--agent", "claude", "--dry-run", *_reg_arg(world)]
    code = handoff_cmd.cmd_triage(argv, which=_missing)
    out, err = capsys.readouterr()
    assert (code, out) == (1, "")
    assert err == "rl-handoff: claude is not installed — put it on PATH first\n"


def test_json_without_dry_run_is_a_usage_error(world, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["handoff", "triage", "--project", "x", "--agent", "claude", "--json"])
    out, err = capsys.readouterr()
    assert (exit_info.value.code, out) == (2, "")
    assert "--json needs --dry-run" in err


def test_plain_dry_run_prints_what_it_would_launch(world, capsys):
    key = _first_project(world, capsys)["key"]
    code, out, _ = _handoff(world, capsys, "--project", key, "--agent", "claude", "--dry-run")
    assert code == 0
    assert out.startswith("dry run — nothing launched\n  directory: ")
    assert "  argv: claude " in out


def test_plain_refusal_explains_itself_on_stderr(world, capsys):
    code, out, err = _handoff(world, capsys, "--project", "/nowhere", "--agent", "claude")
    assert (code, out) == (1, "")
    assert err == "rl-handoff: no sessions of /nowhere are waiting for triage\n"


def test_an_unreadable_registry_is_an_error_in_both_modes(world, capsys):
    world.registry_path.write_text("{not json")
    code, out, err = _handoff(world, capsys, "--project", "x", "--agent", "claude", "--dry-run")
    assert (code, out) == (1, "")
    assert err.startswith("rl-handoff: ")
    code, out, _ = _handoff(
        world, capsys, "--project", "x", "--agent", "claude", "--dry-run", "--json"
    )
    doc = json.loads(out)
    assert (code, doc["kind"], doc["version"]) == (1, "error", 1)


def test_launch_runs_the_agent_in_the_planned_directory(world, capsys):
    key = _first_project(world, capsys)["key"]
    launched = []

    def runner(argv, cwd):
        launched.append((tuple(argv), cwd))
        return 0

    argv = ["--project", key, "--agent", "claude", *_reg_arg(world)]
    assert handoff_cmd.cmd_triage(argv, runner=runner) == 0
    assert cli.main(["handoff", "triage", *argv, "--dry-run", "--json"]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert launched == [(tuple(planned["argv"]), planned["cwd"])]
