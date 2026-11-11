import json
from datetime import UTC, datetime, timedelta

import pytest
from rich.text import Text
from ringleader import cli, registry
from ringleader.devtools.tui_fixture import build_fixture
from ringleader.home import app as app_module
from ringleader.home.app import InboxApp
from ringleader.home.base import ScreenTarget
from ringleader.home.palette import PALETTE_EXCLUDED, PALETTE_VERBS, Entry, InboxCommands, verb_argv
from ringleader.home.screens.harvests import HarvestsScreen
from ringleader.home.screens.home import HomeScreen
from ringleader.home.screens.output import OutputScreen
from ringleader.home.screens.projects import ProjectsScreen
from ringleader.home.screens.prune import PruneScreen
from ringleader.home.screens.roundup import RoundupScreen
from ringleader.home.screens.triage import TriageScreen
from ringleader.home.screens.usage import UsageScreen
from ringleader.home.state import NextStep
from ringleader.picker.app import PickerScreen
from ringleader.tui.modals import ConfirmModal
from ringleader.tui.runner import CommandRun
from ringleader.tui.snapshot import screen_text
from textual.content import Content

from tui_helpers import drawn_text, until

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)


def _iso(days):
    return (NOW_DT - timedelta(days=days)).isoformat()


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


def _write(path, *threads):
    reg = {**registry.empty_registry(), "threads": list(threads), "lastRoundupAt": _iso(0.1)}
    path.write_text(json.dumps(reg))
    return path


def _thread(sid, *, days, **extra):
    return {
        "sessionId": sid,
        "source": "claude-code",
        "file": f"/nowhere/{sid}.jsonl",
        "project": "/w/a",
        "title": f"title {sid}",
        "mtime": _iso(days),
        "status": "unclassified",
        "decision": None,
        **extra,
    }


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, cancelled, columns, python=None, env=None):
        self.calls.append(tuple(argv))
        return CommandRun(tuple(argv), 0, f"BODY of {' '.join(argv)}\n", 0.5)


@pytest.fixture(autouse=True)
def fake_run(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr("ringleader.tui.runner.run_rl", fake)
    return fake


def _toasts(app):
    return [n.message for n in app._notifications]


async def _type(pilot, app, query):
    await until(pilot, lambda: app.idle)
    await pilot.press("ctrl+p")
    await until(pilot, lambda: app.screen.query("CommandList"))
    keys = ["space" if c == " " else c for c in query]
    await pilot.press(*keys)


async def _choose(pilot, app, needle):
    await until(pilot, lambda: needle in screen_text(app))
    await until(pilot, lambda: app.screen.query_one("CommandList").highlighted is not None)
    await pilot.press("enter")


def test_every_flat_verb_is_allowed_or_excluded_exactly_once():
    allowed = {verb.argv[0] for verb in PALETTE_VERBS}
    excluded = set(PALETTE_EXCLUDED)
    assert not allowed & excluded
    assert allowed | excluded == set(cli._FLAT)


def test_registry_is_appended_only_for_verbs_that_take_it():
    by_argv = {verb.argv: verb for verb in PALETTE_VERBS}
    assert verb_argv(by_argv[("harvests",)], "/r.json") == ("harvests",)
    assert verb_argv(by_argv[("doctor",)], "/r.json") == ("doctor", "--registry", "/r.json")


async def test_typing_a_verb_opens_an_output_screen_that_ran_it(tmp_path, fake_run):
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _type(pilot, app, "usage w")
        await _choose(pilot, app, "rl usage windows")
        await until(pilot, lambda: isinstance(app.screen, OutputScreen))
        await until(pilot, lambda: "BODY of usage windows" in screen_text(app))
    assert fake_run.calls == [("usage", "windows")]


async def test_registry_verbs_run_against_the_apps_registry(tmp_path, fake_run):
    path = build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _type(pilot, app, "rl doctor")
        await _choose(pilot, app, "check tools")
        await until(pilot, lambda: isinstance(app.screen, OutputScreen))
        await until(pilot, lambda: bool(fake_run.calls))
    assert fake_run.calls == [("doctor", "--registry", str(path))]


async def test_go_to_projects_navigates(tmp_path):
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _type(pilot, app, "go to proj")
        await _choose(pilot, app, "Go to Projects")
        await until(pilot, lambda: isinstance(app.screen, ProjectsScreen))


async def test_a_next_step_hit_activates_it(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _type(pilot, app, "next")
        await _choose(pilot, app, "Next: Triage 1 session")
        await until(pilot, lambda: isinstance(app.screen, TriageScreen))


@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
async def test_brackets_in_a_session_derived_hit_render_literally(tmp_path, key):
    path = _write(tmp_path / "registry.json", _thread("a", days=30, gitProjectLabel="[cadet-city]"))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _type(pilot, app, "cadet")
        await until(pilot, lambda: "[cadet-city]" in screen_text(app))
        await pilot.press(key)
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))


async def test_palette_does_not_open_over_a_modal(tmp_path):
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        app.push_screen(ConfirmModal(Text("Sure? [y/n]")))
        await until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)


TARGET_SCREENS = {
    ScreenTarget.HOME: HomeScreen,
    ScreenTarget.PROJECTS: ProjectsScreen,
    ScreenTarget.RESUME: PickerScreen,
    ScreenTarget.ROUNDUP: RoundupScreen,
    ScreenTarget.PRUNE: PruneScreen,
    ScreenTarget.HARVESTS: HarvestsScreen,
    ScreenTarget.USAGE: UsageScreen,
}


async def test_go_reaches_every_target_without_a_not_built_notice(tmp_path):
    assert set(TARGET_SCREENS) == set(ScreenTarget)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        for target, screen_class in TARGET_SCREENS.items():
            await pilot.pause()
            app.go(target)
            await until(
                pilot, lambda screen_class=screen_class: isinstance(app.screen, screen_class)
            )
        await pilot.pause()
        assert not [t for t in _toasts(app) if "not built" in t]


async def test_activate_handles_every_step_kind(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("a", days=2), _thread("b", days=30))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        project = app.home_state().projects[0]
        steps = {
            "roundup": RoundupScreen,
            "woke": TriageScreen,
            "recent": TriageScreen,
            "project": TriageScreen,
        }
        for kind, screen_class in steps.items():
            await pilot.pause()
            app.go(ScreenTarget.HOME)
            app.activate(NextStep(kind, "t", "d", False, False, project=project))
            await until(
                pilot, lambda screen_class=screen_class: isinstance(app.screen, screen_class)
            )
        await pilot.pause()
        app.go(ScreenTarget.HOME)
        app.activate(NextStep("publish", "t", "d", False, False))
        await until(pilot, lambda: "no pending harvests to publish" in _toasts(app))
        app.activate(NextStep("resume", "t", "d", False, False))
        await pilot.pause()
        assert not [t for t in _toasts(app) if "not built" in t]


async def test_golden_palette(tmp_path, golden):
    app = _app(build_fixture(tmp_path, threads=40, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _type(pilot, app, "us")
        await until(pilot, lambda: "rl usage windows" in screen_text(app))
        await until(pilot, lambda: app.screen.query_one("CommandList").highlighted is not None)
        golden("inbox/palette", await drawn_text(pilot, app), replace={str(tmp_path): "<root>"})


async def test_help_with_brackets_is_escaped_for_literal_rendering(tmp_path, monkeypatch):
    entry = Entry("Open thing", "wipes [bold]all[/bold] of it", lambda: None)
    monkeypatch.setattr("ringleader.home.palette.entries", lambda app: [entry])
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        hits = [hit async for hit in InboxCommands(app.screen).discover()]
    assert [Content.from_markup(hit.help).plain for hit in hits] == ["wipes [bold]all[/bold] of it"]


async def test_typing_in_the_palette_reuses_the_home_state(tmp_path, monkeypatch):
    builds = []
    original = app_module.build_home_state

    def counted(*args, **kwargs):
        builds.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module, "build_home_state", counted)
    path = _write(tmp_path / "registry.json", _thread("a", days=2))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        built = len(builds)
        await _type(pilot, app, "next tri")
        await until(pilot, lambda: "Next: Triage 1 session" in screen_text(app))
        assert len(builds) == built
