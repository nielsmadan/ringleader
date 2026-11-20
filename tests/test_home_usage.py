import json
import shlex
import subprocess
import sys
import threading
from asyncio import to_thread
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ringleader import registry
from ringleader.devtools.tui_fixture import build_fixture
from ringleader.home.app import InboxApp
from ringleader.home.screens.output import run_commands
from ringleader.home.screens.usage import UsageScreen, UsageView
from ringleader.tui import runner
from ringleader.tui.background import CancelToken
from ringleader.tui.runner import CommandRun
from ringleader.tui.snapshot import screen_text
from ringleader.usage.remote_cache import RemoteCache
from ringleader.usage.remote_render import inbox_reports_json
from ringleader.usage.remote_types import CreditsData, Diagnostic, ProviderResult, SubscriptionData
from textual.widgets import Button, Log

from tui_helpers import drawn_text, until

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

    def __call__(self, argv, *, cancelled, columns, stdout_only=False):
        self.calls.append(tuple(argv))
        if self.gate is not None:
            while not self.gate.wait(0.01):
                if cancelled():
                    self.results.append(None)
                    return CommandRun(tuple(argv), None, "", 0.2)
        self.results.append(0)
        if "--_inbox" in argv:
            return CommandRun(tuple(argv), 0, _report_output(argv[1]), 0.5)
        return CommandRun(tuple(argv), 0, f"BODY of {' '.join(argv)}\n", 0.5)


def _report_output(view, *, fresh=False, code=0):
    provider, scope, data = (
        (
            "claude",
            "subscription",
            SubscriptionData("Latest remote plan" if fresh else "Fixture Claude", None, ()),
        )
        if view == "subscription"
        else ("openrouter", "credits", CreditsData(10, 4, 6) if fresh else CreditsData(5, 7, -2))
    )
    if code:
        result = ProviderResult(
            provider,
            scope,
            None,
            "error",
            None,
            False,
            False,
            None,
            Diagnostic("fixture_failed", "Latest remote diagnostic."),
        )
    else:
        result = ProviderResult(
            provider,
            scope,
            None,
            "ok",
            NOW_DT.isoformat(),
            not fresh,
            False,
            data,
            None,
        )
    return inbox_reports_json((result,))


class RemoteRun(FakeRun):
    def __init__(self, *, code=0, wait_after_cancel=False):
        super().__init__()
        self.code = code
        self.wait_after_cancel = wait_after_cancel
        self.release = threading.Event()
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def __call__(self, argv, *, cancelled, columns, stdout_only=False):
        if "--refresh" not in argv:
            return super().__call__(
                argv, cancelled=cancelled, columns=columns, stdout_only=stdout_only
            )
        self.calls.append(tuple(argv))
        self.started.set()
        while not self.release.wait(0.01):
            if cancelled():
                self.cancelled.set()
                if not self.wait_after_cancel:
                    break
        return CommandRun(
            tuple(argv), self.code, _report_output(argv[1], fresh=True, code=self.code), 0.5
        )


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
        golden("inbox/usage", await drawn_text(pilot, app), replace={str(tmp_path): "<root>"})


async def test_usage_view_navigation_at_narrow_width(tmp_path, monkeypatch, golden):
    fake = FakeRun()
    monkeypatch.setattr(runner, "run_rl", fake)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(38, 20)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("7")
        await until(pilot, lambda: "BODY of usage windows" in screen_text(app))
        await pilot.press("tab")
        await until(pilot, lambda: "Plan Fixture Claude" in screen_text(app))
        assert "Subscription" in screen_text(app)
        assert app.screen.query_one("#usage-subscription", Button).variant == "primary"
        golden("inbox/usage-subscription-narrow", await drawn_text(pilot, app))
        await pilot.click("#usage-token")
        await until(pilot, lambda: "Balance -$2.0" in screen_text(app))
        assert app.screen.query_one("#usage-token", Button).variant == "primary"
        await pilot.press("shift+tab")
        await until(pilot, lambda: "Plan Fixture Claude" in screen_text(app))
        await pilot.click("#usage-local")
        await until(pilot, lambda: "BODY of usage windows" in screen_text(app))
        assert app.screen.view is UsageView.LOCAL
        assert len(fake.calls) == 5
        await pilot.press("r")
        await until(pilot, lambda: len(fake.calls) == 7 and "exit 0" in screen_text(app))
    assert fake.calls == [
        ("usage",),
        ("usage", "windows"),
        ("usage", "subscription", "--cached", "--_inbox"),
        ("usage", "token", "--cached", "--_inbox"),
        ("usage", "subscription", "--cached", "--_inbox"),
        ("usage",),
        ("usage", "windows"),
    ]


async def test_refresh_cancel_retains_previous_report_and_waits_for_worker(tmp_path, monkeypatch):
    fake = RemoteRun(wait_after_cancel=True)
    monkeypatch.setattr(runner, "run_rl", fake)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: app.idle)
            await pilot.press("7", "tab")
            await until(pilot, lambda: "Saved reports" in screen_text(app))
            await pilot.press("r")
            await until(pilot, fake.started.is_set)
            screen = screen_text(app)
            assert "Refreshing… c to cancel" in screen
            assert "Previous report\nClaude subscription" in screen
            assert "Plan Fixture Claude" in screen
            assert "Latest remote plan" not in screen
            await pilot.press("r", "c")
            await until(pilot, fake.cancelled.is_set)
            await pilot.press("r")
            assert "Cancelling…" in screen_text(app)
            fake.release.set()
            await until(pilot, lambda: "Refresh cancelled" in screen_text(app))
            screen = screen_text(app)
            assert "Previous report\nClaude subscription" in screen
            assert "Plan Fixture Claude" in screen
            assert "Latest remote plan" not in screen
        finally:
            fake.release.set()
    assert fake.calls.count(("usage", "subscription", "--refresh", "--_inbox")) == 1


async def test_failed_refresh_retains_previous_report_and_shows_latest_diagnostics(
    tmp_path, monkeypatch
):
    fake = RemoteRun(code=1)
    monkeypatch.setattr(runner, "run_rl", fake)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("7", "shift+tab")
        await until(pilot, lambda: "Saved reports" in screen_text(app))
        fake.release.set()
        await pilot.press("r")
        await until(pilot, lambda: "Refresh failed" in screen_text(app))
        screen = screen_text(app)
        assert "Latest remote diagnostic." in screen
        assert "Previous report\nOpenRouter credits" in screen
        assert "Balance -$2.0" in screen
    assert fake.calls[-2:] == [
        ("usage", "token", "--cached", "--_inbox"),
        ("usage", "token", "--refresh", "--_inbox"),
    ]


async def test_switching_views_cancels_refresh_and_discards_its_late_result(tmp_path, monkeypatch):
    fake = RemoteRun(wait_after_cancel=True)
    token_started = threading.Event()
    token_release = threading.Event()
    refresh_delivered = threading.Event()

    def run(argv, **kwargs):
        if tuple(argv[:2]) == ("usage", "token"):
            token_started.set()
            token_release.wait()
        return fake(argv, **kwargs)

    finish = UsageScreen._finished

    def finished(self, generation, refresh, result):
        finish(self, generation, refresh, result)
        if refresh:
            refresh_delivered.set()

    monkeypatch.setattr(runner, "run_rl", run)
    monkeypatch.setattr(UsageScreen, "_finished", finished)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: app.idle)
            await pilot.press("7", "tab")
            await until(pilot, lambda: "Saved reports" in screen_text(app))
            await pilot.press("r")
            await until(pilot, fake.started.is_set)
            await pilot.click("#usage-token")
            await until(pilot, fake.cancelled.is_set)
            await until(pilot, token_started.is_set)
            fake.release.set()
            await until(pilot, refresh_delivered.is_set)
            assert "Reading saved reports… c to cancel" in screen_text(app)
            token_release.set()
            await until(pilot, lambda: app.idle)
            screen = screen_text(app)
            assert "Balance -$2.0" in screen
            assert app.screen.view is UsageView.TOKEN
        finally:
            fake.release.set()
            token_release.set()


def _openrouter_cli(tmp_path, monkeypatch, replies):
    driver = Path(__file__).parent / "fixtures" / "openrouter_cli.py"
    fixture = tmp_path / "openrouter.json"
    fixture.write_text(
        json.dumps(
            {
                "now": NOW_DT.isoformat(),
                "replies": replies,
            }
        )
    )
    requests = fixture.with_suffix(".requests")
    launcher = tmp_path / "fixture-python"
    launcher.write_text(
        f"#!/bin/sh\nshift 3\nexec {shlex.quote(sys.executable)} "
        f'{shlex.quote(str(driver))} {shlex.quote(str(fixture))} "$@"\n'
    )
    launcher.chmod(0o700)
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "fixture-only-management-key")
    run_rl = runner.run_rl

    def run(argv, *, cancelled, columns, stdout_only=False):
        python = str(launcher) if tuple(argv[:2]) == ("usage", "token") else None
        return run_rl(
            argv,
            cancelled=cancelled,
            columns=columns,
            python=python,
            timeout=5,
            stdout_only=stdout_only,
        )

    return run, requests


async def test_real_cli_openrouter_reports_only_fetch_on_explicit_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPROFILEIMPORTTIME", "1")
    run, requests = _openrouter_cli(
        tmp_path,
        monkeypatch,
        {
            "activity": [200, {}, {"data": []}],
            "credits": [200, {}, {"data": {"total_credits": 5, "total_usage": 7}}],
        },
    )
    monkeypatch.setattr(runner, "run_rl", run)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("7", "shift+tab")
        await until(pilot, lambda: "Saved reports" in screen_text(app))
        assert not requests.exists()
        await pilot.press("r")
        await until(pilot, lambda: "Refresh complete" in screen_text(app))
        screen = screen_text(app)
        assert "2026-09-15 through 2026-10-14 UTC (inclusive)" in screen
        assert "Balance -$2.0" in screen
        assert "OPENROUTER_API_KEY" in screen
        await pilot.press("tab", "shift+tab")
        await until(pilot, lambda: "Saved reports" in screen_text(app))
        screen = screen_text(app)
        assert "last fetched credential:" in screen
        assert "Balance -$2.0" in screen
    assert requests.read_text().splitlines() == ["activity", "credits"]


async def test_partial_refresh_retains_both_scopes_through_later_failures(tmp_path, monkeypatch):
    local = FakeRun()
    attempts = 0
    release = threading.Event()
    claude = ProviderResult(
        "claude",
        "subscription",
        None,
        "ok",
        NOW_DT.isoformat(),
        True,
        False,
        SubscriptionData("Earlier Claude", None, ()),
        None,
    )
    codex = ProviderResult(
        "codex",
        "subscription",
        "account:deadbeef12345678",
        "ok",
        NOW_DT.isoformat(),
        True,
        False,
        SubscriptionData("Earlier Codex", "account:deadbeef12345678", ()),
        None,
    )
    codex_error = ProviderResult(
        "codex",
        "subscription",
        None,
        "error",
        None,
        False,
        False,
        None,
        Diagnostic("codex_timeout", "Codex timed out."),
    )
    next_claude = ProviderResult(
        "claude",
        "subscription",
        None,
        "ok",
        (NOW_DT + timedelta(minutes=5)).isoformat(),
        False,
        False,
        SubscriptionData("Latest Claude", None, ()),
        None,
    )

    def run(argv, *, cancelled, columns, stdout_only=False):
        nonlocal attempts
        if "--cached" in argv:
            return CommandRun(tuple(argv), 0, inbox_reports_json((claude, codex)), 0.1)
        if "--refresh" not in argv:
            return local(argv, cancelled=cancelled, columns=columns)
        attempts += 1
        if attempts == 1:
            return CommandRun(tuple(argv), 1, inbox_reports_json((next_claude, codex_error)), 0.1)
        if attempts == 2:
            while not cancelled() and not release.wait(0.01):
                pass
            return CommandRun(tuple(argv), None, "", 0.1)
        return CommandRun(tuple(argv), 1, "", 0.1)

    monkeypatch.setattr(runner, "run_rl", run)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: app.idle)
            await pilot.press("7", "tab")
            await until(pilot, lambda: "Plan Earlier Codex" in screen_text(app))
            await pilot.press("r")
            await until(pilot, lambda: "Refresh failed" in screen_text(app))
            screen = screen_text(app)
            assert "Plan Latest Claude" in screen
            assert "Codex timed out." in screen
            assert "Previous report\nCodex subscription" in screen
            assert f"last fetched account:deadbeef12345678 at {NOW_DT.isoformat()}" in screen
            assert "Plan Earlier Codex" in screen
            await pilot.press("r")
            await until(pilot, lambda: attempts == 2)
            screen = screen_text(app)
            assert "Refreshing… c to cancel" in screen
            assert "Previous report\nClaude subscription" in screen
            assert "Plan Latest Claude" in screen and "Plan Earlier Codex" in screen
            await pilot.press("c")
            await until(pilot, lambda: "Refresh cancelled" in screen_text(app))
            screen = screen_text(app)
            assert "Plan Latest Claude" in screen and "Plan Earlier Codex" in screen
            await pilot.press("r")
            await until(pilot, lambda: "Could not read the usage report." in screen_text(app))
            screen = screen_text(app)
            assert "Refresh failed" in screen
            assert "Previous report\nClaude subscription" in screen
            assert "Plan Latest Claude" in screen and "Plan Earlier Codex" in screen
        finally:
            release.set()
            await until(pilot, lambda: app.idle)


@pytest.mark.parametrize("cache_write_fails", [False, True])
async def test_large_activity_keeps_credits_in_inbox_output(
    tmp_path, monkeypatch, cache_write_fails
):
    row = {
        "date": "2026-10-14",
        "model": "m" * 400,
        "model_permaslug": "m-v1",
        "provider_name": "fixture provider",
        "endpoint_id": "fixture endpoint",
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "requests": 1,
        "usage": 0.1,
        "byok_usage_inference": 0,
    }
    row_count = 31 if cache_write_fails else 4400
    if cache_write_fails:
        RemoteCache().path.with_suffix(".lock").mkdir(parents=True)
    run, requests = _openrouter_cli(
        tmp_path,
        monkeypatch,
        {
            "activity": [200, {}, {"data": [row] * row_count}],
            "credits": [200, {}, {"data": {"total_credits": 5, "total_usage": 7}}],
        },
    )
    monkeypatch.setattr(runner, "run_rl", run)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("7", "shift+tab")
        await until(pilot, lambda: "Saved reports" in screen_text(app))
        await pilot.press("r")
        await until(pilot, lambda: "Refresh complete" in screen_text(app))
        lines = app.screen.query_one("#output-log", Log).lines
        assert "OpenRouter key" in lines
        assert "OpenRouter credits" in lines
        assert "  Balance -$2.0" in lines
        assert sum(line.startswith("    2026-10-14") for line in lines) == 30
        if cache_write_fails:
            hint = (
                "  Showing 30 of 31 rows. "
                "Run rl usage token --report activity --refresh to fetch a new full report."
            )
            assert hint in lines
            await pilot.press("tab", "shift+tab")
            await until(pilot, lambda: "Saved reports" in screen_text(app))
            retained = screen_text(app)
            assert "Previous report\nOpenRouter activity" in retained
            assert "Usage cache could not be saved." in retained
            assert f"fetched {NOW_DT.isoformat()} (stale)" in retained
            assert hint in app.screen.query_one("#output-log", Log).lines
        else:
            assert (
                "  Showing 30 of 4,400 rows. "
                "Run rl usage token --report activity --cached to read the latest saved report."
                in lines
            )
    assert requests.read_text().splitlines() == ["activity", "credits"]
    if cache_write_fails:
        return
    monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY")
    full = await to_thread(
        subprocess.run,
        [
            sys.executable,
            "-P",
            "-m",
            "ringleader",
            "usage",
            "token",
            "--report",
            "activity",
            "--cached",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert full.returncode == 0
    (saved,) = json.loads(full.stdout)["results"]
    assert saved["cached"] is True
    assert len(saved["data"]["rows"]) == 4400


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


async def test_jumping_home_mid_run_cancels_the_child(tmp_path, monkeypatch):
    gate = threading.Event()
    fake = FakeRun(gate)
    monkeypatch.setattr("ringleader.tui.runner.run_rl", fake)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            await until(pilot, lambda: app.idle)
            await pilot.press("7")
            await until(pilot, lambda: len(fake.calls) == 1)
            await pilot.press("1")
            await until(pilot, lambda: fake.results == [None])
        finally:
            gate.set()
    assert fake.calls == [("usage",)]


class LongRun:
    def __call__(self, argv, *, cancelled, columns, python=None, env=None):
        body = "".join(f"line {n}\n" for n in range(100))
        return CommandRun(tuple(argv), 0, body, 0.5)


async def test_a_state_change_does_not_rewrite_the_output(tmp_path, monkeypatch):
    monkeypatch.setattr("ringleader.tui.runner.run_rl", LongRun())
    cleared = []
    original = Log.clear

    def counted(self):
        cleared.append(self.id)
        return original(self)

    monkeypatch.setattr(Log, "clear", counted)
    app = _app(build_fixture(tmp_path, threads=5, now=NOW_DT).registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await until(pilot, lambda: app.idle)
        await pilot.press("7")
        await until(pilot, lambda: "exit 0" in screen_text(app))
        before = len(cleared)
        app._set()
        app._rerender()
        await until(pilot, lambda: app.idle)
        lines = app.screen.query_one("#output-log", Log).line_count
        assert (len(cleared), lines) == (before, 201)
