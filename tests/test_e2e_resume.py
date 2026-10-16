from datetime import UTC, datetime

from ringleader import refresh

from tui_e2e import (
    KEY_ENTER,
    KEY_ESCAPE,
    Expect,
    ExpectExit,
    Scenario,
    Send,
    isolated_env,
    make_fake_agents,
    run_scenario,
    write_claude_session,
)

PROMPT = "tighten the flaky login retry"


def _world(tmp_path):
    project = tmp_path / "work" / "proj"
    project.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    records = make_fake_agents(bin_dir)
    env = isolated_env(tmp_path, bin_dir)
    projects_dir = tmp_path / "_claude"
    write_claude_session(projects_dir, project, "s0", PROMPT)
    reg = tmp_path / "registry.json"
    refresh.auto_refresh(
        reg,
        datetime.now(UTC).isoformat(),
        claude_projects_dir=projects_dir,
        git_resolver=lambda _: None,
        name_filters=[],
    )
    return project, env, records, reg


def test_choosing_a_session_execs_its_agent_in_the_project(tmp_path):
    project, env, records, reg = _world(tmp_path)

    result = run_scenario(
        Scenario(
            argv=("resume", "--all", "--registry", str(reg)),
            env=env,
            cwd=project,
            steps=(
                Expect(PROMPT),
                Send(KEY_ENTER),
                Expect("resume with which agent?"),
                Send(KEY_ENTER),
                ExpectExit(),
            ),
        )
    )

    assert result.exit_status == 0
    assert records["claude"].read_text().splitlines() == [str(project), "--resume", "s0"]


def test_escape_quits_without_launching_anything(tmp_path):
    project, env, records, reg = _world(tmp_path)

    result = run_scenario(
        Scenario(
            argv=("resume", "--all", "--registry", str(reg)),
            env=env,
            cwd=project,
            steps=(Expect(PROMPT), Send(KEY_ESCAPE), ExpectExit()),
        )
    )

    assert result.exit_status == 0
    assert [name for name, record in records.items() if record.exists()] == []
