from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import contract, registry, resume
from ..agents import source_of
from .tui_fixture import build_fixture

MARKER = ".gui-world"
MIN_THREADS = 2
FORBIDDEN_HOME_DIRS = (".local/share", ".claude", ".codex", ".factory", ".pi")
RUNNING_WINDOW = timedelta(days=7)
EMPTY_HARVEST = {"summary": "", "gotchas": [], "decisions": [], "whatChanged": []}
FULL_HARVEST = {
    "summary": "Synthetic harvest from the GUI world.",
    "gotchas": ["The GUI world's fake-harvest model never reads the transcript."],
    "decisions": [],
    "whatChanged": [],
}
CLAUDE_STUB = """#!/bin/sh
echo "sandbox stub: the real claude must not run here" >&2
exit 97
"""
FAKE_HARVEST = """#!/bin/sh
prompt=$(cat)
if printf '%s\\n' "$prompt" | grep -q '^project: .*/proj-00$'; then
  printf '%s\\n' {empty}
else
  sleep "${{GUI_WORLD_HARVEST_DELAY:-3}}"
  printf '%s\\n' {full}
fi
"""
HOOKLINESINKER = """#!/bin/sh
if [ "$1" = "sessions" ] && [ "$2" = "--json" ]; then
  printf '%s\\n' {envelope}
else
  printf '%s\\n' '{{}}'
fi
"""


@dataclass(frozen=True)
class World:
    root: Path
    registry_path: Path
    env: dict[str, str]
    running_id: str
    running_label: str


def _structured(payload: Mapping[str, object]) -> str:
    return shlex.quote(json.dumps({"structured_output": payload}))


def _running_thread(reg: dict[str, object], now: datetime) -> dict[str, object]:
    visible = [
        t for t in contract.inbox_threads(reg, now.isoformat()) if source_of(t) == "claude-code"
    ]
    if not visible:
        raise RuntimeError("the fixture produced no visible claude-code session")
    newest = max(visible, key=lambda t: datetime.fromisoformat(str(t["mtime"])))
    recent = [t for t in visible if now - datetime.fromisoformat(str(t["mtime"])) <= RUNNING_WINDOW]
    return max(recent, key=lambda t: datetime.fromisoformat(str(t["mtime"])), default=newest)


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _write_config(root: Path, config_path: Path) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["ai"] = "claude"
    config["ai_command_prefix"] = [str(root / "bin" / "fake-harvest")]
    config_path.write_text(json.dumps(config), encoding="utf-8")


@contextlib.contextmanager
def _isolated_environ(env: dict[str, str]) -> Iterator[None]:
    saved = dict(os.environ)
    os.environ.update(env)
    os.environ.pop("OPENCODE_DB", None)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def build_world(root: Path, *, now: datetime, threads: int = 80) -> World:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("", encoding="utf-8")
    fixture = build_fixture(root, threads=threads, now=now, transcripts=True)
    with _isolated_environ(fixture.env):
        reg = registry.load_registry(fixture.registry_path)
        running = _running_thread(reg, now)
    _write_config(root, root / "xdg-data" / "ringleader" / "config.json")
    (root / "claude-settings.json").write_text(
        json.dumps({"cleanupPeriodDays": 30}), encoding="utf-8"
    )
    bin_dir = root / "bin"
    _write_executable(bin_dir / "claude", CLAUDE_STUB)
    _write_executable(
        bin_dir / "fake-harvest",
        FAKE_HARVEST.format(empty=_structured(EMPTY_HARVEST), full=_structured(FULL_HARVEST)),
    )
    envelope = {
        "protocol": 1,
        "sessions": [
            {
                "protocol": 1,
                "agent": "claude",
                "running": True,
                "session": {"id": running["sessionId"]},
            }
        ],
        "problems": [],
    }
    _write_executable(
        root / "xdg-data" / "hooklinesinker" / "bin" / "hooklinesinker",
        HOOKLINESINKER.format(envelope=shlex.quote(json.dumps(envelope))),
    )
    env = {**fixture.env, "RINGLEADER_CLAUDE_SETTINGS": str(root / "claude-settings.json")}
    return World(
        root=root,
        registry_path=fixture.registry_path,
        env=env,
        running_id=registry.qualified_id(running),
        running_label=resume.label(running),
    )


def _refusal(root: Path, home: Path) -> str | None:
    for relative in FORBIDDEN_HOME_DIRS:
        guarded = (home / relative).resolve()
        if root == guarded or guarded in root.parents:
            return f"refusing to build inside {guarded}"
    if root == home.resolve() or root in home.resolve().parents:
        return f"refusing to replace {root}"
    if root.exists() and not root.is_dir():
        return f"{root} exists and is not a directory"
    if root.exists() and not (root / MARKER).exists():
        return f"{root} exists and has no {MARKER} marker; refusing to replace it"
    return None


def _exports(world: World) -> list[str]:
    lines = [f"export {key}={shlex.quote(value)}" for key, value in world.env.items()]
    return [*lines, f'export PATH={shlex.quote(str(world.root / "bin"))}:"$PATH"']


def _summary(world: World) -> list[str]:
    return [
        f"GUI world built at {world.root}",
        f"registry: {world.registry_path}",
        f"running session: {world.running_label} ({world.running_id})",
        "sessions under proj-00 harvest as empty; every other project harvests after a short delay",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m ringleader.devtools.gui_world",
        description="Build a disposable circus world.",
    )
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--threads", type=int, default=80)
    ap.add_argument("--exports", action="store_true")
    args = ap.parse_args(argv)
    if args.threads < MIN_THREADS:
        print(f"gui_world: --threads must be at least {MIN_THREADS}", file=sys.stderr)
        return 1
    root = args.root.resolve()
    problem = _refusal(root, Path.home())
    if problem is not None:
        print(f"gui_world: {problem}", file=sys.stderr)
        return 1
    if root.exists():
        shutil.rmtree(root)
    world = build_world(root, now=datetime.now(UTC), threads=args.threads)
    print("\n".join(_summary(world)), file=sys.stderr)
    if args.exports:
        print("\n".join(_exports(world)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
