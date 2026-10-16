import threading
from datetime import UTC, datetime

from ringleader import registry
from ringleader.home.app import InboxApp
from ringleader.home.screens.output import run_commands
from ringleader.home.screens.usage import UsageScreen
from ringleader.tui.background import CancelToken
from ringleader.tui.runner import CommandRun
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


class FakeRun:
    def __init__(self, gate=None):
        self.calls = []
        self.gate = gate
        self.results = []

    def __call__(self, argv, *, cancelled, columns, python=None, env=None):
        self.calls.append(tuple(argv))
        if self.gate is not None:
            while not self.gate.wait(0.01):
                if cancelled():
                    self.results.append(None)
                    return CommandRun(tuple(argv), None, "", 0.2)
        self.results.append(0)
        return CommandRun(tuple(argv), 0, f"BODY of {' '.join(argv)}\n", 0.5)


async def test_both_outputs_show_separated_by_a_blank_line(tmp_path, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr("ringleader.tui.runner.run_rl", fake)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("7")
        await until(pilot, lambda: isinstance(app.screen, UsageScreen))
        await until(pilot, lambda: "BODY of usage windows" in screen_text(app))
        text = screen_text(app)
    assert fake.calls == [("usage",), ("usage", "windows")]
    assert "BODY of usage\n\nBODY of usage windows" in text
    assert "exit 0 · 1.0s" in text


async def test_r_reruns_and_c_cancels_while_running(tmp_path, monkeypatch):
    gate = threading.Event()
    fake = FakeRun(gate)
    monkeypatch.setattr("ringleader.tui.runner.run_rl", fake)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: app.idle)
            await pilot.press("7")
            await until(pilot, lambda: "running… c to cancel" in screen_text(app))
            await pilot.press("c")
            await until(pilot, lambda: "cancelled" in screen_text(app))
            gate.set()
            await pilot.press("r")
            await until(pilot, lambda: "BODY of usage windows" in screen_text(app))
        finally:
            gate.set()
    assert fake.calls == [("usage",), ("usage",), ("usage", "windows")]


async def test_escape_leaves_and_cancels(tmp_path, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr("ringleader.tui.runner.run_rl", FakeRun(gate))
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: app.idle)
            await pilot.press("7")
            await until(pilot, lambda: isinstance(app.screen, UsageScreen))
            await pilot.press("escape")
            await until(pilot, lambda: not isinstance(app.screen, UsageScreen))
            await until(pilot, lambda: app.idle)
        finally:
            gate.set()


async def test_quitting_mid_run_cancels_the_child_before_exiting(tmp_path, monkeypatch):
    gate = threading.Event()
    fake = FakeRun(gate)
    monkeypatch.setattr("ringleader.tui.runner.run_rl", fake)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: app.idle)
            await pilot.press("7")
            await until(pilot, lambda: len(fake.calls) == 1)
            await pilot.press("q")
            await until(pilot, lambda: app._exit)
        finally:
            gate.set()
    assert fake.results == [None]


async def test_golden_usage(tmp_path, monkeypatch, golden):
    monkeypatch.setattr("ringleader.tui.runner.run_rl", FakeRun())
    app = _app(build_fixture(tmp_path, threads=40, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("7")
        await until(pilot, lambda: "BODY of usage windows" in screen_text(app))
        golden("inbox/usage", screen_text(app), replace={str(tmp_path): "<root>"})


def test_run_commands_hands_the_child_the_app_environment_and_registry_argv(tmp_path, monkeypatch):
    script = tmp_path / "fake-python"
    script.write_text('#!/bin/sh\necho "$XDG_DATA_HOME"\necho "$@"\n')
    script.chmod(0o755)
    monkeypatch.setattr("ringleader.tui.runner.sys.executable", str(script))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    job = run_commands([("doctor", "--registry", "/r.json")], 80)
    (run,) = job(CancelToken(), lambda _update: None)
    assert run.output.splitlines() == [
        str(tmp_path / "xdg"),
        "-P -m ringleader doctor --registry /r.json",
    ]
