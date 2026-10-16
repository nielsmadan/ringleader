from __future__ import annotations

import codecs
import json
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

import pexpect
import pyte
from ringleader import session_env
from ringleader.sources.claude_code import ClaudeCodeSource

KEY_ENTER = "\r"
KEY_ESCAPE = "\x1b"
KEY_DOWN = "\x1b[B"
KEY_UP = "\x1b[A"

FAKE_AGENTS = ("claude", "codex", "opencode", "pi", "droid")


@dataclass(frozen=True)
class Expect:
    text: str
    timeout: float = 10.0


@dataclass(frozen=True)
class Send:
    keys: str


@dataclass(frozen=True)
class ExpectExit:
    timeout: float = 10.0


type Step = Expect | Send | ExpectExit


@dataclass(frozen=True)
class Scenario:
    argv: tuple[str, ...]
    env: dict[str, str]
    steps: tuple[Step, ...]
    cwd: Path | None = None
    rows: int = 30
    cols: int = 100


@dataclass(frozen=True)
class ScenarioResult:
    exit_status: int | None
    screens: tuple[str, ...]
    transcript: str


class _Terminal:
    def __init__(self, child: pexpect.spawn, rows: int, cols: int) -> None:
        self.child = child
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.chunks: list[str] = []
        self.eof = False

    def display(self) -> str:
        return "\n".join(self.screen.display)

    def _feed(self, data: bytes) -> None:
        text = self.decoder.decode(data)
        self.chunks.append(text)
        self.stream.feed(text)

    def pump(self, done: Callable[[], bool], timeout: float, what: str) -> None:
        deadline = monotonic() + timeout
        while not done():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"timed out waiting for {what}; last screen:\n{self.display()}"
                )
            if self.eof:
                raise AssertionError(f"exited before {what}; last screen:\n{self.display()}")
            try:
                self._feed(self.child.read_nonblocking(65536, timeout=min(0.1, remaining)))
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                self.eof = True


def _run_steps(terminal: _Terminal, steps: tuple[Step, ...]) -> tuple[int | None, list[str]]:
    screens: list[str] = []
    exit_status: int | None = None
    for step in steps:
        match step:
            case Expect(text=text, timeout=timeout):
                terminal.pump(lambda text=text: text in terminal.display(), timeout, repr(text))
                screens.append(terminal.display())
            case Send(keys=keys):
                terminal.child.send(keys.encode())
            case ExpectExit(timeout=timeout):
                terminal.pump(lambda: terminal.eof, timeout, "exit")
                terminal.child.close()
                exit_status = terminal.child.exitstatus
                screens.append(terminal.display())
    return exit_status, screens


def run_scenario(scenario: Scenario) -> ScenarioResult:
    child = pexpect.spawn(
        sys.executable,
        ["-P", "-m", "ringleader", *scenario.argv],
        env=scenario.env,
        cwd=str(scenario.cwd) if scenario.cwd is not None else None,
        dimensions=(scenario.rows, scenario.cols),
        encoding=None,
    )
    terminal = _Terminal(child, scenario.rows, scenario.cols)
    try:
        exit_status, screens = _run_steps(terminal, scenario.steps)
    finally:
        if child.isalive():
            child.terminate(force=True)
    return ScenarioResult(
        exit_status=exit_status, screens=tuple(screens), transcript="".join(terminal.chunks)
    )


def isolated_env(root: Path, bin_dir: Path) -> dict[str, str]:
    dropped = {"COLUMNS", "LINES", "CODEX_SESSION_ID", *session_env.PROBES.values()}
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        **{key: value for key, value in os.environ.items() if key not in dropped},
        "PATH": f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(home),
        "TERM": "xterm-256color",
        "SESSION_TRACKER_PROJECTS_DIR": str(root / "_claude"),
        "CODEX_SESSIONS_DIR": str(root / "_codex"),
        "PI_SESSIONS_DIR": str(root / "_pi"),
        "FACTORY_HOME_OVERRIDE": str(root / "_factory"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "CLAUDE_CONFIG_DIR": str(root / "_claude-config"),
        "CODEX_HOME": str(root / "_codex-home"),
        "OPENCODE_DB": str(root / "_opencode" / "opencode.db"),
        "OPENCODE_CONFIG_DIR": str(root / "_opencode-config"),
        "PI_CODING_AGENT_DIR": str(root / "_pi-agent"),
    }


def _fake_agent(record: Path) -> str:
    return (
        "#!/bin/sh\n"
        f'{{ printf \'%s\\n\' "$PWD"; for arg in "$@"; do printf \'%s\\n\' "$arg"; done; }}'
        f" > '{record}'\n"
        "exit 0\n"
    )


def make_fake_agents(bin_dir: Path) -> dict[str, Path]:
    bin_dir.mkdir(parents=True, exist_ok=True)
    records = {name: bin_dir / f"{name}.record" for name in FAKE_AGENTS}
    for name, record in records.items():
        script = bin_dir / name
        script.write_text(_fake_agent(record), encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return records


def write_claude_session(projects_dir: Path, cwd: Path, session_id: str, prompt: str) -> Path:
    path = ClaudeCodeSource(projects_dir=projects_dir).storage_for(str(cwd)) / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "type": "user",
            "cwd": str(cwd),
            "sessionId": session_id,
            "message": {"role": "user", "content": prompt},
        },
        {
            "type": "assistant",
            "cwd": str(cwd),
            "sessionId": session_id,
            "message": {"role": "assistant", "content": "done"},
        },
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path
