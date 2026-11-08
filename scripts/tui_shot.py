from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ringleader import registry
from ringleader.devtools.tui_fixture import build_fixture
from ringleader.home.app import InboxApp
from ringleader.picker.app import PickerApp
from ringleader.resume import Scope
from ringleader.tui.snapshot import screen_text
from textual.app import App
from textual.pilot import Pilot
from textual.worker import WorkerCancelled

SETTLE = "settle"
SETTLE_TIMEOUT = 5.0
DEFAULT_NOW = "2026-10-15T12:00:00+00:00"


@dataclass(frozen=True)
class ShotContext:
    registry_path: Path
    now: datetime
    read_only: bool


@dataclass(frozen=True)
class Shot:
    after: str
    text: str


@dataclass(frozen=True)
class ShotResult:
    shots: tuple[Shot, ...]
    exited: bool
    return_value: object


type Settle = Callable[[App[Any], Pilot[Any]], Awaitable[None]]
type AppFactory = Callable[[ShotContext], tuple[App[Any], Settle]]


async def settle_workers(app: App[Any], pilot: Pilot[Any]) -> None:
    try:
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=SETTLE_TIMEOUT)
    except (TimeoutError, WorkerCancelled):
        print(
            f"settle timed out after {SETTLE_TIMEOUT}s; a worker is still running", file=sys.stderr
        )
    await pilot.pause()


def _reload(path: str, now: str) -> tuple[dict[str, Any], str | None]:
    return registry.load_registry(path), None


def _picker(ctx: ShotContext) -> tuple[App[Any], Settle]:
    app = PickerApp(
        ctx.registry_path,
        "/",
        scope=Scope.ALL,
        refresher=_reload,
        presence=frozenset,
        now=lambda: ctx.now,
    )
    return app, settle_workers


async def settle_inbox(app: App[Any], pilot: Pilot[Any]) -> None:
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while not (isinstance(app, InboxApp) and app.idle):
        if time.monotonic() >= deadline:
            print(f"settle timed out after {SETTLE_TIMEOUT}s; inbox is still busy", file=sys.stderr)
            break
        await pilot.pause(0.02)
    await pilot.pause()


def _inbox(ctx: ShotContext) -> tuple[App[Any], Settle]:
    app = InboxApp(
        ctx.registry_path,
        refresher=_reload,
        presence=frozenset,
        now=lambda: ctx.now,
        models=lambda task: None,
        read_only=ctx.read_only,
    )
    return app, settle_inbox


APPS: dict[str, AppFactory] = {"inbox": _inbox, "picker": _picker}


def parse_keys(spec: str) -> list[str]:
    return [key.strip() for key in spec.split(",") if key.strip()]


def _finished(app: App[Any]) -> bool:
    return app.return_value is not None or app._exit or not app.is_running


async def shoot(
    app: App[Any], *, size: tuple[int, int], keys: Sequence[str], settle: Settle, each: bool = False
) -> ShotResult:
    shots: list[Shot] = []
    exited = False
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        if each:
            shots.append(Shot(after="", text=screen_text(app)))
        for key in keys:
            if key == SETTLE:
                await settle(app, pilot)
            else:
                await pilot.press(key)
                await pilot.pause()
            if _finished(app):
                exited = True
                break
            if each:
                shots.append(Shot(after=key, text=screen_text(app)))
        if not exited and not each:
            shots.append(Shot(after=keys[-1] if keys else "", text=screen_text(app)))
    return ShotResult(shots=tuple(shots), exited=exited, return_value=app.return_value)


def render(result: ShotResult) -> str:
    blocks = [f"--- after: {shot.after or '(start)'} ---\n{shot.text}" for shot in result.shots]
    tail = [f"exited: {result.return_value!r}\n"] if result.exited else []
    return "".join([*blocks, *tail])


def _size(spec: str) -> tuple[int, int]:
    width, _, height = spec.partition("x")
    return int(width), int(height)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Print a Textual app's screen as plain text.")
    ap.add_argument("--app", choices=sorted(APPS), default="picker")
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--registry", type=Path, help="read-only copy of this registry")
    source.add_argument("--fixture", type=int, default=40, help="synthetic threads")
    ap.add_argument("--size", default="100x30")
    ap.add_argument("--keys", default="", help="comma-separated Textual keys; 'settle' waits")
    ap.add_argument("--each", action="store_true", help="a shot after every key")
    ap.add_argument("--now", help="ISO time the app sees")
    return ap


def _empty_stores(workdir: Path) -> dict[str, str]:
    empty = workdir / "empty-home"
    empty.mkdir(exist_ok=True)
    return {
        "HOME": str(empty),
        "SESSION_TRACKER_PROJECTS_DIR": str(empty / "claude"),
        "CODEX_SESSIONS_DIR": str(empty / "codex"),
        "PI_SESSIONS_DIR": str(empty / "pi"),
        "FACTORY_HOME_OVERRIDE": str(empty / "factory"),
    }


def _context(args: argparse.Namespace, workdir: Path) -> ShotContext:
    if args.registry is not None:
        copy = workdir / "registry.json"
        shutil.copyfile(args.registry, copy)
        os.environ["XDG_DATA_HOME"] = str(workdir / "xdg-data")
        os.environ["XDG_STATE_HOME"] = str(workdir / "xdg-state")
        os.environ.update(_empty_stores(workdir))
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(UTC)
        return ShotContext(registry_path=copy, now=now, read_only=True)
    now = datetime.fromisoformat(args.now or DEFAULT_NOW)
    fixture = build_fixture(workdir, threads=args.fixture, now=now)
    os.environ.update(fixture.env)
    os.environ["HOME"] = str(workdir / "fixture-home")
    return ShotContext(registry_path=fixture.registry_path, now=now, read_only=False)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="tui-shot-") as tmp:
        ctx = _context(args, Path(tmp))
        app, settle = APPS[args.app](ctx)
        result = asyncio.run(
            shoot(
                app,
                size=_size(args.size),
                keys=parse_keys(args.keys),
                settle=settle,
                each=args.each,
            )
        )
    sys.stdout.write(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
