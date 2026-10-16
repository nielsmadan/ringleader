from ringleader.home.intents import (
    AgentHandoffIntent,
    ResumeIntent,
    handoff_cwd,
    publish_prompt,
    triage_prompt,
)
from ringleader.home.launch import handoff_argv, run_intent
from ringleader.picker.app import Choice

THREAD = {"sessionId": "s1", "source": "claude-code", "project": "/w/a", "mtime": "2026-10-01"}


class Recorder:
    def __init__(self, result=0):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _handoff(cwd, agent="claude-code"):
    return AgentHandoffIntent(agent, str(cwd), "publish them", "publish", "all harvests")


def _present(binary):
    return f"/bin/{binary}"


def test_a_resume_intent_runs_the_resume_command_with_a_picker_returning_the_choice():
    choice = Choice(thread=THREAD, target="codex")
    command = Recorder(result=7)
    runner = Recorder()
    status = run_intent(
        ResumeIntent(choice),
        "/r/registry.json",
        dry_run=False,
        resume_command=command,
        runner=runner,
        which=_present,
    )
    (argv,), kwargs = command.calls[0]
    assert (status, argv) == (7, ["--registry", "/r/registry.json"])
    assert kwargs["picker"]("/r/registry.json", "/", None, "", None) is choice
    assert (kwargs["runner"], kwargs["which"]) == (runner, _present)


def test_a_dry_run_resume_passes_dry_run():
    command = Recorder()
    run_intent(
        ResumeIntent(Choice(thread=THREAD, target="claude-code")),
        "/r/registry.json",
        dry_run=True,
        resume_command=command,
        runner=Recorder(),
        which=_present,
    )
    assert command.calls[0][0] == (["--registry", "/r/registry.json", "--dry-run"],)


def test_a_handoff_runs_the_agent_with_the_prompt_in_its_directory(tmp_path):
    runner = Recorder(result=3)
    status = run_intent(
        _handoff(tmp_path),
        "/r/registry.json",
        dry_run=False,
        resume_command=Recorder(),
        runner=runner,
        which=_present,
    )
    assert (status, runner.calls) == (3, [((("claude", "publish them"), str(tmp_path)), {})])


def test_a_missing_binary_fails_with_a_message(tmp_path, capsys):
    status = run_intent(
        _handoff(tmp_path),
        "/r/registry.json",
        dry_run=False,
        resume_command=Recorder(),
        runner=Recorder(),
        which=lambda _binary: None,
    )
    assert status == 1
    assert "rl-inbox: claude is not installed — put it on PATH first" in capsys.readouterr().err


def test_a_dry_run_handoff_prints_the_directory_and_argv_and_launches_nothing(tmp_path, capsys):
    runner = Recorder()
    status = run_intent(
        _handoff(tmp_path, agent="opencode"),
        "/r/registry.json",
        dry_run=True,
        resume_command=Recorder(),
        runner=runner,
        which=_present,
    )
    assert status == 0
    assert capsys.readouterr().out.splitlines() == [
        "dry run — nothing launched",
        f"  directory: {tmp_path}",
        "  argv: opencode --prompt 'publish them'",
    ]
    assert runner.calls == []


def test_an_agent_that_cannot_launch_fails_with_a_message(tmp_path, capsys):
    status = run_intent(
        _handoff(tmp_path, agent="nope"),
        "/r/registry.json",
        dry_run=False,
        resume_command=Recorder(),
        runner=Recorder(),
        which=_present,
    )
    assert status == 1
    assert "rl-inbox: nope cannot be launched by rl" in capsys.readouterr().err


def test_a_missing_directory_fails_with_a_message(tmp_path, capsys):
    status = run_intent(
        _handoff(tmp_path / "gone"),
        "/r/registry.json",
        dry_run=False,
        resume_command=Recorder(),
        runner=Recorder(),
        which=_present,
    )
    assert status == 1
    assert f"rl-inbox: {tmp_path / 'gone'} is not a directory" in capsys.readouterr().err


def test_handoff_argv_uses_the_agents_launch_argv(tmp_path):
    assert handoff_argv(_handoff(tmp_path, agent="codex")) == ("codex", "publish them")


def test_triage_prompt_scopes_the_skill_to_the_quoted_project():
    prompt = triage_prompt("my repo", "/w/my repo")
    assert prompt.startswith(
        "Use the ringleader-triage skill to triage the dormant sessions of the project my repo."
    )
    assert "`rl prune --project '/w/my repo'`" in prompt
    assert "`rl triage --project '/w/my repo' --json`" in prompt
    assert prompt.endswith("If the ringleader-triage skill is not available, say so and stop.")


def test_publish_prompt_names_the_repository_when_given():
    assert publish_prompt("ringleader") == (
        "Use the ringleader-publish skill to publish the pending ringleader harvests"
        " for ringleader only."
    )
    assert publish_prompt(None) == (
        "Use the ringleader-publish skill to publish the pending ringleader harvests."
    )


def test_handoff_cwd_takes_the_first_existing_directory_else_home(tmp_path):
    there = tmp_path / "there"
    there.mkdir()
    home = tmp_path / "home"
    assert handoff_cwd([None, str(tmp_path / "gone"), str(there)], home) == str(there)
    assert handoff_cwd([None, str(tmp_path / "gone")], home) == str(home)
