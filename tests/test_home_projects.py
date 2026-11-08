from datetime import UTC, datetime, timedelta

from ringleader import registry
from ringleader.decisions import Outcome
from ringleader.devtools.tui_fixture import build_fixture
from ringleader.home.app import InboxApp
from ringleader.home.intents import AgentHandoffIntent
from ringleader.home.screens.projects import ProjectsScreen
from ringleader.home.screens.triage import TriageScreen
from ringleader.resume import launchable_agents
from ringleader.tui.snapshot import screen_text
from textual.widgets import OptionList

from tui_helpers import until

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)


def _iso(days):
    return (NOW_DT - timedelta(days=days)).isoformat()


def _thread(sid, *, days, project, status="unclassified", **extra):
    return {
        "sessionId": sid,
        "source": "claude-code",
        "file": f"/nowhere/{sid}.jsonl",
        "project": project,
        "title": f"title {sid}",
        "mtime": _iso(days),
        "status": status,
        "decision": None,
        **extra,
    }


def _write(path, *threads):
    reg = {**registry.empty_registry(), "threads": list(threads), "lastRoundupAt": _iso(0.1)}
    registry.save_registry(path, reg)
    return path


def _reload(path, now_iso):
    return registry.load_registry(path), None


def _execute(op, ctx):
    return Outcome(ok=True, message="", wrote=False)


def _app(path):
    return InboxApp(
        path,
        refresher=_reload,
        presence=frozenset,
        now=lambda: NOW_DT,
        models=lambda task: None,
        execute=_execute,
        cwd=path.parent,
        strict=True,
    )


def _project_ids(app):
    rows = app.screen.query_one("#projects", OptionList)
    return [rows.get_option_at_index(i).id for i in range(rows.option_count)]


async def _open_projects(app, pilot):
    await until(pilot, lambda: app.idle)
    await pilot.press("2")
    await until(pilot, lambda: "Projects —" in screen_text(app))


async def test_projects_are_listed_oldest_activity_first(tmp_path):
    path = _write(
        tmp_path / "registry.json",
        _thread("a1", days=2, project="/w/newer"),
        _thread("b1", days=30, project="/w/older"),
        _thread("b2", days=1, project="/w/older", status="finished"),
        _thread("c1", days=10, project="/w/middle"),
    )
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_projects(app, pilot)
        assert _project_ids(app) == ["/w/older", "/w/middle", "/w/newer"]
        text = screen_text(app)
        assert "Projects — 4 waiting across 3 projects" in text
        assert "/w/older" in text
        assert "2 waiting" in text


async def test_enter_opens_that_projects_triage(tmp_path):
    path = _write(
        tmp_path / "registry.json",
        _thread("a1", days=2, project="/w/newer"),
        _thread("b1", days=30, project="/w/older"),
    )
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_projects(app, pilot)
        await pilot.press("down", "enter")
        await until(pilot, lambda: isinstance(app.screen, TriageScreen))
        await until(pilot, lambda: "/w/newer · 1 waiting" in screen_text(app))
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, ProjectsScreen))


async def test_t_hands_the_project_to_an_agent_in_its_directory(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    path = _write(
        tmp_path / "registry.json",
        _thread("a1", days=2, project=str(project), gitProjectKey="github.com/me/proj"),
    )
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_projects(app, pilot)
        await pilot.press("t")
        await until(pilot, lambda: "with which agent?" in screen_text(app))
        await pilot.press("enter")
        await until(pilot, lambda: app._exit or not app.is_running)
    intent = app.return_value
    assert isinstance(intent, AgentHandoffIntent)
    assert (intent.agent, intent.cwd, intent.purpose) == (
        launchable_agents()[0],
        str(project),
        "triage",
    )
    assert "ringleader-triage" in intent.prompt
    assert "rl prune --project github.com/me/proj" in intent.prompt


async def test_golden_projects(tmp_path, golden):
    fixture = build_fixture(tmp_path, threads=40, now=NOW_DT)
    app = _app(fixture.registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_projects(app, pilot)
        golden("inbox/projects", screen_text(app), replace={str(tmp_path): "<root>"})
