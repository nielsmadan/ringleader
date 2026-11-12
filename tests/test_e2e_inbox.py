from datetime import UTC, datetime
from pathlib import Path

from ringleader import registry
from ringleader.devtools.tui_fixture import Fixture, build_fixture
from ringleader.home.state import project_queue

from tui_e2e import (
    KEY_ENTER,
    Expect,
    ExpectExit,
    Scenario,
    Send,
    isolated_env,
    make_fake_agents,
    run_scenario,
)

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def _world(tmp_path: Path) -> tuple[Fixture, dict[str, str], dict[str, Path]]:
    bin_dir = tmp_path / "bin"
    records = make_fake_agents(bin_dir)
    env = isolated_env(tmp_path, bin_dir)
    fixture = build_fixture(tmp_path, threads=40, now=NOW, transcripts=True)
    return fixture, env, records


def _plain_session(fixture: Fixture) -> dict:
    threads = registry.load_registry(fixture.registry_path)["threads"]
    return next(
        t
        for t in threads
        if t.get("title")
        and not t.get("snoozedUntil")
        and not t.get("keep")
        and not t.get("filteredBy")
        and not t.get("finishOnExit")
        and not t.get("harvestedAt")
    )


def _first_project(fixture: Fixture):
    reg = registry.load_registry(fixture.registry_path)
    return project_queue(reg["threads"], NOW.isoformat())[0]


def _triage_steps():
    return (
        Expect("Next:"),
        Send("2"),
        Expect("Projects · Repos —"),
        Send("t"),
        Expect("with which agent?"),
        Send(KEY_ENTER),
        ExpectExit(),
    )


def test_inbox_starts_and_quits(tmp_path):
    fixture, env, _ = _world(tmp_path)

    result = run_scenario(
        Scenario(
            argv=("inbox", "--registry", str(fixture.registry_path)),
            env=env,
            steps=(Expect("Next:"), Send("q"), ExpectExit()),
        )
    )

    assert result.exit_status == 0


def test_bare_rl_opens_the_inbox_on_the_default_registry(tmp_path):
    _, env, _ = _world(tmp_path)

    result = run_scenario(
        Scenario(argv=(), env=env, steps=(Expect("Next:"), Send("q"), ExpectExit()))
    )

    assert result.exit_status == 0


def test_resuming_a_session_from_the_picker_execs_its_agent(tmp_path):
    fixture, env, records = _world(tmp_path)
    session = _plain_session(fixture)
    title = session["title"]

    result = run_scenario(
        Scenario(
            argv=("inbox", "--registry", str(fixture.registry_path)),
            env=env,
            steps=(
                Expect("Next:"),
                Send("3"),
                Expect("filter…"),
                Send(title),
                Expect(title),
                Send(KEY_ENTER),
                Expect("resume with which agent?"),
                Send(KEY_ENTER),
                ExpectExit(),
            ),
        )
    )

    assert result.exit_status == 0
    assert records["claude"].read_text().splitlines() == [
        session["project"],
        "--resume",
        session["sessionId"],
    ]


def test_triage_handoff_launches_the_agent_in_the_project(tmp_path):
    fixture, env, records = _world(tmp_path)
    project = _first_project(fixture)

    result = run_scenario(
        Scenario(
            argv=("inbox", "--registry", str(fixture.registry_path)),
            env=env,
            steps=_triage_steps(),
        )
    )

    lines = records["claude"].read_text().splitlines()
    assert result.exit_status == 0
    assert lines[0] == project.key
    assert "ringleader-triage" in "\n".join(lines[1:])


def test_read_only_handoff_prints_a_dry_run_and_launches_nothing(tmp_path):
    fixture, env, records = _world(tmp_path)

    result = run_scenario(
        Scenario(
            argv=("inbox", "--read-only", "--registry", str(fixture.registry_path)),
            env=env,
            steps=_triage_steps(),
        )
    )

    assert result.exit_status == 0
    assert "dry run — nothing launched" in result.screens[-1]
    assert [name for name, record in records.items() if record.exists()] == []
