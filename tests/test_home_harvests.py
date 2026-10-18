from datetime import UTC, datetime, timedelta

from ringleader import harvests, registry
from ringleader.home.app import InboxApp
from ringleader.home.intents import AgentHandoffIntent
from ringleader.home.screens import harvests as harvests_screen
from ringleader.home.screens.harvests import HarvestContentScreen, HarvestsScreen
from ringleader.resume import launchable_agents
from ringleader.tui.modals import ConfirmModal
from ringleader.tui.snapshot import screen_text
from scripts.tui_fixture import build_fixture

from tui_helpers import until

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)


def _reload(path, now_iso):
    return registry.load_registry(path), None


def _app(path):
    return InboxApp(
        path,
        refresher=_reload,
        presence=frozenset,
        now=lambda: NOW_DT,
        models=lambda task: None,
        cwd=path.parent,
        strict=True,
    )


def _harvest(sid, folder, *, days=1, content="## Notes\n\nFirst durable line.\nSecond."):
    harvests.write_harvest(
        session_id=sid,
        name=f"name-{sid}",
        folder_path=folder,
        repo_url=None,
        harvested_at=(NOW_DT - timedelta(days=days)).isoformat(),
        content=content,
    )


def _exited(app):
    return not app.is_running


async def _open(app, pilot):
    await until(pilot, lambda: app.idle)
    await pilot.press("6")
    await until(pilot, lambda: isinstance(app.screen, HarvestsScreen))


def _fixture(tmp_path):
    return build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path


async def test_empty_state(tmp_path):
    app = _app(_fixture(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        assert "No pending harvests." in screen_text(app)


async def test_groups_headers_and_previews(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _harvest("a1", str(proj), days=1)
    _harvest("a2", str(proj), days=3)
    _harvest("b1", None, days=5)
    app = _app(_fixture(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        text = screen_text(app)
    assert "proj — 2 harvests" in text
    assert "no repository — 1 harvest" in text
    assert "name-a1" in text
    assert "First durable line." in text


async def test_unreadable_files_are_counted(tmp_path):
    _harvest("a1", None)
    (harvests.harvests_dir() / "broken.md").write_text("no frontmatter\n")
    app = _app(_fixture(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        assert "1 harvest file could not be read" in screen_text(app)


async def test_p_hands_the_group_to_an_agent_in_its_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    proj.mkdir()
    _harvest("a1", str(proj))
    app = _app(_fixture(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        await pilot.press("p")
        await until(pilot, lambda: "with which agent?" in screen_text(app))
        await pilot.press("enter")
        await until(pilot, lambda: _exited(app))
    intent = app.return_value
    assert isinstance(intent, AgentHandoffIntent)
    assert intent.agent in launchable_agents()
    assert (intent.cwd, intent.purpose, intent.label) == (str(proj), "publish", "proj")
    assert "for proj only" in intent.prompt


async def test_capital_p_with_a_collapsing_root_asks_before_publishing_everything(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _harvest("a1", None)
    app = _app(_fixture(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        await pilot.press("P")
        await until(pilot, lambda: "with which agent?" in screen_text(app))
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        assert "may touch any repository" in str(app.screen.query_one("#confirm-message").content)
        await pilot.press("y")
        await until(pilot, lambda: _exited(app))
    intent = app.return_value
    assert isinstance(intent, AgentHandoffIntent)
    assert (intent.cwd, intent.label) == (str(home), "all harvests")


async def test_enter_shows_the_harvest_content(tmp_path):
    _harvest("a1", None, content="# Title\n\nDurable fact about the build.")
    app = _app(_fixture(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HarvestContentScreen))
        await until(pilot, lambda: "Durable fact about the build." in screen_text(app))
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, HarvestsScreen))


async def test_golden_harvests(tmp_path, golden):
    proj = tmp_path / "proj"
    proj.mkdir()
    _harvest("a1", str(proj), days=1)
    _harvest("a2", str(proj), days=3)
    _harvest("b1", None, days=5)
    app = _app(_fixture(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        golden("inbox/harvests", screen_text(app), replace={str(tmp_path): "<root>"})


async def test_a_rerender_with_the_same_harvests_does_not_regroup_them(tmp_path, monkeypatch):
    grouped = []
    original = harvests_screen.harvest_groups

    def counted(items):
        grouped.append(items)
        return original(items)

    monkeypatch.setattr(harvests_screen, "harvest_groups", counted)
    _harvest("a1", None)
    app = _app(_fixture(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open(app, pilot)
        await until(pilot, lambda: "name-a1" in screen_text(app))
        before = len(grouped)
        app._set()
        app._rerender()
        await until(pilot, lambda: app.idle)
        assert (len(grouped), "name-a1" in screen_text(app)) == (before, True)
