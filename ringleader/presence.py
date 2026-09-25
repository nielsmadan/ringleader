"""Read which agent sessions are live right now, by asking hooklinesinker.

Everything here goes through the `hooklinesinker` CLI. Its state lives under
`$XDG_STATE_HOME/hooklinesinker` and stays private to it: ringleader never opens
those files, so the protocol below is the whole contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTOCOL = 1
TIMEOUT_SECONDS = 2
CONSUMER = "ringleader"
SETUP_COMMAND = "rl data presence install"
VENDOR_COMMAND = "./scripts/vendor-hooklinesinker.sh && just install"
CHECKSUM_MANIFEST = "checksums.txt"
AGENTS = ("claude", "codex", "opencode", "pi", "droid", "qwen", "kimi")

_ARTIFACTS = {
    ("Darwin", "arm64"): "macos-universal",
    ("Darwin", "x86_64"): "macos-universal",
    ("Linux", "aarch64"): "linux-aarch64",
    ("Linux", "x86_64"): "linux-x86_64",
}


class PresenceUnavailable(RuntimeError):
    """Hooklinesinker cannot answer. Never degrade this to "nothing is running"."""


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[list[str]], RunResult]


def _default_runner(cmd: list[str]) -> RunResult:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False)
    return RunResult(out.returncode, out.stdout, out.stderr)


@dataclass(frozen=True)
class PresenceSession:
    agent: str
    session_id: str
    running: bool
    cwd: str | None = None
    binding_id: str | None = None


@dataclass(frozen=True)
class PresenceResult:
    sessions: tuple[PresenceSession, ...] = ()
    problems: tuple[str, ...] = ()
    available: bool = True


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...] = ()
    exit_code: int = 0
    problems: tuple[str, ...] = ()
    available: bool = True
    installed: bool = True
    version: str | None = None


@dataclass(frozen=True)
class ConsumerList:
    names: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    available: bool = True


@dataclass(frozen=True)
class BinaryInfo:
    version: str
    protocol: int


@dataclass(frozen=True)
class HookOutcome:
    agent: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class InstallOutcome:
    binary: str
    reused_active: bool
    detail: str
    hooks: tuple[HookOutcome, ...]
    ok: bool


@dataclass(frozen=True)
class UninstallOutcome:
    ok: bool
    detail: str
    ran: bool


def artifact_dir(system: str | None = None, machine: str | None = None) -> str:
    key = (system or platform.system(), machine or platform.machine())
    artifact = _ARTIFACTS.get(key)
    if artifact is None:
        supported = ", ".join(f"{s}/{m}" for s, m in _ARTIFACTS)
        raise PresenceUnavailable(
            f"no hooklinesinker artifact for {key[0]}/{key[1]} — supported: {supported}"
        )
    return artifact


def vendor_root() -> Path:
    override = os.environ.get("RINGLEADER_VENDOR_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "_vendor"


def active_binary_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "hooklinesinker" / "bin" / "hooklinesinker"


def _agent_home_override(root: Path, env_var: str, default_relative: str) -> Path:
    override = os.environ.get(env_var)
    return Path(override) if override else root / default_relative


def detected_agents(home: Path | None = None) -> tuple[str, ...]:
    root = home or Path.home()
    config_root = _agent_home_override(root, "XDG_CONFIG_HOME", ".config")
    homes = {
        "claude": root / ".claude",
        "codex": root / ".codex",
        "opencode": _agent_home_override(config_root, "OPENCODE_CONFIG_DIR", "opencode"),
        "pi": _agent_home_override(root, "PI_CODING_AGENT_DIR", ".pi/agent"),
        # FACTORY_HOME_OVERRIDE replaces HOME, not the .factory dir
        "droid": _agent_home_override(root, "FACTORY_HOME_OVERRIDE", ".") / ".factory",
        "qwen": _agent_home_override(root, "QWEN_HOME", ".qwen"),
        "kimi": _agent_home_override(root, "KIMI_CODE_HOME", ".kimi-code"),
    }
    return tuple(agent for agent in AGENTS if homes[agent].is_dir())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(root: Path) -> dict[str, str]:
    manifest = root / CHECKSUM_MANIFEST
    try:
        text = manifest.read_text()
    except OSError as e:
        raise PresenceUnavailable(f"cannot read {manifest}: {e}") from e
    entries = {}
    for line in text.splitlines():
        digest, _, name = line.strip().partition(" ")
        relative = name.strip().lstrip("*")
        if digest and relative:
            entries[relative] = digest
    return entries


def _not_installed() -> str:
    return f"hooklinesinker is not installed — run `{SETUP_COMMAND}`"


def _semver(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return ()


def _load_json(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _record(raw: object) -> PresenceSession | None:
    if not isinstance(raw, dict) or raw.get("protocol", PROTOCOL) != PROTOCOL:
        return None
    agent = raw.get("agent")
    identity = raw.get("session")
    if not isinstance(agent, str) or not isinstance(identity, dict):
        return None
    session_id = identity.get("id")
    if not isinstance(session_id, str) or not session_id:
        return None
    return PresenceSession(
        agent=agent,
        session_id=session_id,
        running=bool(raw.get("running")),
        cwd=identity.get("cwd"),
        binding_id=raw.get("bindingId"),
    )


def _envelope_problems(raw: object) -> tuple[str, ...]:
    out = []
    for problem in raw if isinstance(raw, list) else []:
        if isinstance(problem, dict):
            out.append(str(problem.get("message", problem)))
        else:
            out.append(str(problem))
    return tuple(out)


def _parse_sessions(text: str) -> PresenceResult:
    payload = _load_json(text)
    if payload is None:
        return PresenceResult(
            problems=(f"could not parse hooklinesinker sessions --json: {text[:200]}",),
            available=False,
        )
    if payload.get("protocol") != PROTOCOL:
        return PresenceResult(
            problems=(
                (
                    f"hooklinesinker speaks protocol {payload.get('protocol')}, "
                    f"ringleader speaks protocol {PROTOCOL}"
                ),
            ),
            available=False,
        )
    sessions = []
    problems = list(_envelope_problems(payload.get("problems")))
    raw_sessions = payload.get("sessions")
    for raw in raw_sessions if isinstance(raw_sessions, list) else []:
        parsed = _record(raw)
        if parsed is None:
            problems.append(f"skipped an unrecognized status record: {json.dumps(raw)[:120]}")
        else:
            sessions.append(parsed)
    return PresenceResult(tuple(sessions), tuple(problems))


class PresenceClient:
    def __init__(
        self,
        executable: Path | str | None = None,
        runner: Runner | None = None,
        system: str | None = None,
        machine: str | None = None,
        vendor_dir: Path | str | None = None,
    ) -> None:
        self.executable = Path(executable) if executable is not None else active_binary_path()
        self._run: Runner = runner if runner is not None else _default_runner
        self._system = system
        self._machine = machine
        self._vendor_dir = Path(vendor_dir) if vendor_dir is not None else None

    @property
    def vendor_dir(self) -> Path:
        return self._vendor_dir if self._vendor_dir is not None else vendor_root()

    @property
    def system(self) -> str:
        return self._system or platform.system()

    @property
    def machine(self) -> str:
        return self._machine or platform.machine()

    @property
    def artifact(self) -> str:
        return artifact_dir(self.system, self.machine)

    def is_installed(self) -> bool:
        return self.executable.is_file()

    def _invoke(self, binary: Path, args: Sequence[str]) -> RunResult | str:
        """The run result, or a failure message when the process never produced one."""
        try:
            return self._run([str(binary), *args])
        except FileNotFoundError:
            return _not_installed()
        except subprocess.TimeoutExpired:
            return f"`hooklinesinker {args[0]}` timed out after {TIMEOUT_SECONDS}s"
        except OSError as e:
            return f"could not run {binary}: {e}"

    def candidate_path(self) -> Path:
        relative = f"{self.artifact}/hooklinesinker"
        path = self.vendor_dir / relative
        if not path.is_file():
            raise PresenceUnavailable(
                f"no hooklinesinker artifact staged at {path} — run `{VENDOR_COMMAND}`"
            )
        expected = read_manifest(self.vendor_dir).get(relative)
        if expected is None:
            raise PresenceUnavailable(
                f"{self.vendor_dir / CHECKSUM_MANIFEST} has no entry for {relative} — "
                f"run `{VENDOR_COMMAND}`"
            )
        actual = sha256_file(path)
        if actual != expected:
            raise PresenceUnavailable(
                f"checksum mismatch for {relative}: manifest says {expected}, staged file is {actual}"
            )
        return path

    def sessions(self) -> PresenceResult:
        result = self._invoke(self.executable, ["sessions", "--json"])
        if isinstance(result, str):
            return PresenceResult(problems=(result,), available=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "no output"
            return PresenceResult(
                problems=(
                    f"`hooklinesinker sessions --json` exited {result.returncode}: {detail}",
                ),
                available=False,
            )
        return _parse_sessions(result.stdout)

    def running_session_ids(self) -> frozenset[tuple[str, str]]:
        return _running_ids(self.sessions())

    def require_running_session_ids(self) -> frozenset[tuple[str, str]]:
        result = self.sessions()
        if not result.available:
            message = "; ".join(result.problems) or "hooklinesinker gave no answer"
            if SETUP_COMMAND not in message:
                message = f"{message} — run `{SETUP_COMMAND}`"
            raise PresenceUnavailable(message)
        return _running_ids(result)

    def doctor(self) -> DoctorReport:
        result = self._invoke(self.executable, ["doctor", "--json"])
        if isinstance(result, str):
            return DoctorReport(
                exit_code=1,
                problems=(result,),
                available=False,
                installed=result != _not_installed(),
            )
        payload = _load_json(result.stdout)
        if payload is None:
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            return DoctorReport(
                exit_code=result.returncode or 1,
                problems=(f"`hooklinesinker doctor --json` produced no readable report: {detail}",),
                available=False,
            )
        if payload.get("protocol") != PROTOCOL:
            return DoctorReport(
                exit_code=result.returncode or 1,
                problems=(
                    (
                        f"hooklinesinker speaks protocol {payload.get('protocol')}, "
                        f"ringleader speaks protocol {PROTOCOL}"
                    ),
                ),
                available=False,
            )
        checks = tuple(
            DoctorCheck(str(c.get("name")), bool(c.get("ok")), str(c.get("detail", "")))
            for c in payload.get("checks", [])
            if isinstance(c, dict)
        )
        return DoctorReport(
            checks=checks,
            exit_code=int(payload.get("exitCode", result.returncode)),
            problems=tuple(f"{c.name}: {c.detail}" for c in checks if not c.ok),
            version=payload.get("version"),
        )

    def consumers(self) -> ConsumerList:
        result = self._invoke(self.executable, ["consumers", "--json"])
        if isinstance(result, str):
            return ConsumerList(problems=(result,), available=False)
        payload = _load_json(result.stdout)
        if payload is None or payload.get("protocol") != PROTOCOL:
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            return ConsumerList(
                problems=(f"could not read the hooklinesinker consumer list: {detail}",),
                available=False,
            )
        names = tuple(
            str(c.get("name"))
            for c in payload.get("consumers", [])
            if isinstance(c, dict) and c.get("name")
        )
        return ConsumerList(names, _envelope_problems(payload.get("problems")))

    def hook_state(self, agent: str) -> str:
        state, _ = self._hook_status(self.executable, agent)
        return state

    def _hook_status(self, binary: Path, agent: str) -> tuple[str, str]:
        args = ["hooks", "status", "--agent", agent, "--json"]
        command = f"`hooklinesinker {' '.join(args)}`"
        result = self._invoke(binary, args)
        if isinstance(result, str):
            return "unknown", result
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "no output"
            return "unknown", f"{command} exited {result.returncode}: {detail}"
        payload = _load_json(result.stdout)
        if payload is None:
            return "unknown", f"could not parse {command}: {result.stdout[:200]}"
        if payload.get("protocol") != PROTOCOL:
            return "unknown", (
                f"hooklinesinker speaks protocol {payload.get('protocol')}, "
                f"ringleader speaks protocol {PROTOCOL}"
            )
        if payload.get("agent") != agent:
            return (
                "unknown",
                f"{command} reported agent {payload.get('agent')!r}, expected {agent!r}",
            )
        state = payload.get("state")
        if state not in ("installed", "missing", "drifted", "unsupported"):
            return "unknown", f"{command} reported unrecognized state {state!r}"
        path = payload.get("path")
        return state, f"{state} at {path}" if path else state

    def _probe(self, binary: Path) -> BinaryInfo | None:
        result = self._invoke(binary, ["version", "--json"])
        if isinstance(result, str) or result.returncode != 0:
            return None
        payload = _load_json(result.stdout)
        if payload is None:
            return None
        try:
            return BinaryInfo(str(payload["version"]), int(payload["protocol"]))
        except (KeyError, TypeError, ValueError):
            return None

    def _choose_binary(self) -> tuple[Path, bool]:
        """The binary to install from, and whether it is the already-active one."""
        active = self._probe(self.executable) if self.is_installed() else None
        if active is not None and active.protocol != PROTOCOL:
            raise PresenceUnavailable(
                f"the active hooklinesinker at {self.executable} speaks protocol "
                f"{active.protocol}; ringleader speaks protocol {PROTOCOL} — refusing to change it"
            )
        try:
            candidate = self.candidate_path()
        except PresenceUnavailable:
            if active is None:
                raise
            return self.executable, True
        bundled = self._probe(candidate)
        if bundled is None or bundled.protocol != PROTOCOL:
            if active is None:
                raise PresenceUnavailable(
                    f"the staged artifact at {candidate} is not a protocol-{PROTOCOL} hooklinesinker"
                )
            return self.executable, True
        if active is None or _semver(bundled.version) > _semver(active.version):
            return candidate, False
        return self.executable, True

    def install_consumer(self, agents: Sequence[str] | None = None) -> InstallOutcome:
        binary, reused = self._choose_binary()
        result = self._invoke(binary, ["install", "--consumer", CONSUMER])
        if isinstance(result, str):
            raise PresenceUnavailable(result)
        detail = (result.stdout or result.stderr).strip()
        if result.returncode != 0:
            return InstallOutcome(str(binary), reused, detail, (), False)
        hooks = tuple(self._install_hook(binary, agent) for agent in (agents or detected_agents()))
        return InstallOutcome(str(binary), reused, detail, hooks, all(h.ok for h in hooks))

    def _install_hook(self, binary: Path, agent: str) -> HookOutcome:
        result = self._invoke(binary, ["hooks", "install", "--agent", agent])
        if isinstance(result, str):
            return HookOutcome(agent, False, result)
        detail = (result.stdout or result.stderr).strip()
        if result.returncode != 0:
            return HookOutcome(agent, False, detail)
        state, status_detail = self._hook_status(binary, agent)
        return HookOutcome(agent, state == "installed", status_detail)

    def uninstall_consumer(self) -> UninstallOutcome:
        if not self.is_installed():
            return UninstallOutcome(
                True, f"hooklinesinker is not installed at {self.executable}", False
            )
        result = self._invoke(self.executable, ["uninstall", "--consumer", CONSUMER])
        if isinstance(result, str):
            return UninstallOutcome(False, result, True)
        return UninstallOutcome(
            result.returncode == 0, (result.stdout or result.stderr).strip(), True
        )


def _running_ids(result: PresenceResult) -> frozenset[tuple[str, str]]:
    return frozenset((s.agent, s.session_id) for s in result.sessions if s.running)


def _row(label: str, state: str, detail: str) -> dict[str, Any]:
    return {"label": label, "state": state, "detail": detail}


def presence_rows(client: PresenceClient) -> list[dict[str, Any]]:
    """`rl doctor`'s presence section: binary, consumer, hooks, status ledger.

    A missing hooklinesinker is a warning — nothing in ringleader needs it until a
    caller asks for live sessions. An installed but unreadable one is an error."""
    report = client.doctor()
    if not report.installed:
        return [_row("hooklinesinker", "warn", _not_installed())]
    if not report.available:
        return [_row("hooklinesinker", "error", "; ".join(report.problems))]

    version = report.version or "unknown version"
    rows = [_row("hooklinesinker", "ok", f"{version} at {client.executable}")]
    consumers = client.consumers()
    if not consumers.available:
        rows.append(_row("consumer", "error", "; ".join(consumers.problems)))
    elif CONSUMER in consumers.names:
        others = [name for name in consumers.names if name != CONSUMER]
        detail = f"registered (sharing with {', '.join(others)})" if others else "registered"
        rows.append(_row("consumer", "ok", detail))
    else:
        rows.append(
            _row("consumer", "warn", f"{CONSUMER} is not registered — run `{SETUP_COMMAND}`")
        )
    for check in report.checks:
        if check.name == "version":
            continue
        rows.append(_row(check.name, "ok" if check.ok else "error", check.detail))
    return rows
