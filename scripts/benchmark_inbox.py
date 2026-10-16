from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from ringleader import prune, registry, revalidate
from ringleader.decisions import PruneOp
from ringleader.home.app import InboxApp
from ringleader.home.screens.projects import ProjectsScreen
from ringleader.home.screens.triage import TriageScreen
from ringleader.home.state import project_queue
from scripts.tui_fixture import Fixture, build_fixture
from textual import events
from textual.pilot import Pilot
from textual.widgets import OptionList, Static

from tui_e2e import Expect, Scenario, isolated_env, make_fake_agents, run_scenario

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
THREADS = 2000
SIZE = (120, 40)
DOWN_PRESSES = 20
GOING_PRESSES = 10
WAIT_SECONDS = 60.0
COLD_TIMEOUT = 20.0
FIRST_PAINT_BUDGET = 1.0
KEY_BUDGET = 0.1
P95_MIN_SAMPLES = 2
KEY_POLL_SECONDS = 0.001


async def wait_for(pilot: Pilot[Any], predicate: Callable[[], bool], what: str) -> None:
    deadline = monotonic() + WAIT_SECONDS
    while not predicate():
        if monotonic() >= deadline:
            raise RuntimeError(f"timed out waiting for {what}")
        await pilot.pause(0.01)


# pilot.press waits for CPU idle, which under a background scan times the scan instead of the key
async def timed_key(
    app: InboxApp, pilot: Pilot[Any], key: str, landed: Callable[[], bool]
) -> float:
    start = perf_counter()
    app.post_message(events.Key(key, key if len(key) == 1 else None))
    while not landed():
        if perf_counter() - start >= WAIT_SECONDS:
            raise RuntimeError(f"{key} was not handled")
        await asyncio.sleep(KEY_POLL_SECONDS)
    await pilot.pause(0)
    return perf_counter() - start


def highlighted(app: InboxApp, list_id: str) -> int | None:
    return app.screen.query_one(list_id, OptionList).highlighted


async def timed_down(app: InboxApp, pilot: Pilot[Any], list_id: str) -> float:
    before = highlighted(app, list_id)
    return await timed_key(app, pilot, "down", lambda: highlighted(app, list_id) != before)


def summary(samples: list[float]) -> dict[str, Any]:
    p95 = (
        statistics.quantiles(samples, n=20, method="inclusive")[18]
        if len(samples) >= P95_MIN_SAMPLES
        else max(samples, default=0.0)
    )
    return {
        "samples": [round(sample, 4) for sample in samples],
        "median": round(statistics.median(samples), 4) if samples else None,
        "p95": round(p95, 4),
        "max": round(max(samples, default=0.0), 4),
    }


def counts_painted(app: InboxApp) -> bool:
    found = list(app.screen.query("#counts").results(Static))
    return bool(found) and bool(str(found[0].content).strip())


def row_ids(app: InboxApp) -> list[str | None]:
    rows = app.screen.query_one("#rows", OptionList)
    return [rows.get_option_at_index(index).id for index in range(rows.option_count)]


def app_for(fixture: Fixture, **kwargs: Any) -> InboxApp:
    return InboxApp(
        fixture.registry_path,
        presence=frozenset,
        now=lambda: NOW,
        models=lambda _task: None,
        strict=True,
        **kwargs,
    )


async def open_largest_project(app: InboxApp, pilot: Pilot[Any]) -> None:
    projects = project_queue(app.view_threads(), app.now_iso())
    largest = max(range(len(projects)), key=lambda index: projects[index].pending)
    app.screen.query_one("#projects", OptionList).highlighted = largest
    await pilot.press("enter")
    await wait_for(pilot, lambda: isinstance(app.screen, TriageScreen), "the triage screen")


async def keep_going(app: InboxApp, pilot: Pilot[Any]) -> float:
    rows = app.screen.query_one("#rows", OptionList)
    option = rows.highlighted_option
    target = option.id if option is not None else None
    return await timed_key(app, pilot, "g", lambda: target not in row_ids(app))


async def idle_phase(fixture: Fixture) -> dict[str, Any]:
    gate = threading.Event()

    def gated(path: str, _now_iso: str) -> tuple[dict[str, Any], str | None]:
        gate.wait()
        return registry.load_registry(path), None

    app = app_for(fixture, refresher=gated)
    start = perf_counter()
    try:
        async with app.run_test(size=SIZE) as pilot:
            await wait_for(pilot, lambda: counts_painted(app), "the first paint")
            first_paint = perf_counter() - start
            await pilot.press("2")
            await wait_for(pilot, lambda: isinstance(app.screen, ProjectsScreen), "projects")
            projects = [await timed_down(app, pilot, "#projects") for _ in range(DOWN_PRESSES)]
            await open_largest_project(app, pilot)
            triage = [await timed_down(app, pilot, "#rows") for _ in range(DOWN_PRESSES)]
            going = [await keep_going(app, pilot) for _ in range(GOING_PRESSES)]
            await wait_for(pilot, lambda: app.writer.idle, "the writer to drain")
    finally:
        gate.set()
    return {
        "first_paint_seconds": round(first_paint, 4),
        "idle_projects_down": summary(projects),
        "idle_triage_down": summary(triage),
        "idle_keep_going": summary(going),
    }


def busy(app: InboxApp) -> bool:
    return app.scans.running or not app.writer.idle


async def load_phase(fixture: Fixture) -> dict[str, Any]:
    app = app_for(fixture)
    samples: list[float] = []
    rescans = 0
    async with app.run_test(size=SIZE) as pilot:
        await wait_for(pilot, lambda: counts_painted(app), "the first paint")
        await pilot.press("ctrl+r")
        candidates = prune.prune_candidates(app.state.snapshot.reg, app.now_iso())
        app.submit(
            [PruneOp(tuple(revalidate.seen(t) for t in candidates))], label="benchmark prune"
        )
        await pilot.press("2")
        await wait_for(pilot, lambda: isinstance(app.screen, ProjectsScreen), "projects")
        while len(samples) < DOWN_PRESSES:
            if not busy(app):
                rescans += 1
                app.rescan()
                await wait_for(pilot, lambda: app.scans.running, "a scan to start")
            samples.append(await timed_down(app, pilot, "#projects"))
        await wait_for(pilot, lambda: app.idle, "the scan and the prune to finish")
    return {
        "load_projects_down": summary(samples),
        "load_pruned": len(candidates),
        "load_extra_rescans": rescans,
    }


def cold_phase(fixture: Fixture, env: dict[str, str]) -> dict[str, Any]:
    scenario = Scenario(
        argv=("inbox", "--registry", str(fixture.registry_path)),
        env=env,
        steps=(Expect("Next:", timeout=COLD_TIMEOUT),),
        cols=SIZE[0],
        rows=SIZE[1],
    )
    start = perf_counter()
    try:
        run_scenario(scenario)
    except AssertionError as error:
        reason = str(error).splitlines()[0]
        return {"cold_seconds": None, "cold_error": reason}
    return {"cold_seconds": round(perf_counter() - start, 4)}


def verdict(result: dict[str, Any]) -> dict[str, bool]:
    def median(name: str) -> float:
        value = result[name]["median"]
        return float("inf") if value is None else float(value)

    return {
        "first_paint": result["first_paint_seconds"] < FIRST_PAINT_BUDGET,
        "idle_keypress_median": max(median("idle_projects_down"), median("idle_triage_down"))
        < KEY_BUDGET,
        "load_keypress_median": median("load_projects_down") < KEY_BUDGET,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Time the rl inbox home on a synthetic registry.")
    ap.add_argument("--threads", type=int, default=THREADS)
    ap.add_argument("--skip-cold", action="store_true", help="skip the pexpect cold start")
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="benchmark-inbox-") as tmp:
        root = Path(tmp)
        env = isolated_env(root, root / "bin")
        make_fake_agents(root / "bin")
        os.environ.update(env)
        fixture = build_fixture(root, threads=args.threads, now=NOW, transcripts=True)
        result: dict[str, Any] = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "textual": version("textual"),
            "threads": args.threads,
            **asyncio.run(idle_phase(fixture)),
            **asyncio.run(load_phase(fixture)),
        }
        if not args.skip_cold:
            result |= cold_phase(fixture, env)
    result["within_budget"] = verdict(result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
