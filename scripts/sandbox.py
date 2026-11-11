#!/usr/bin/env python3
"""Rebuild the frozen real-session world used to test ringleader skills."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from ringleader.devtools.isolation import AGENT_STORE_VARS, agent_store_env

REPO = Path(__file__).resolve().parent.parent
DESCRIPTOR = REPO / "scripts" / "sandbox-fixture.json"
SANDBOX = REPO / ".sandbox"
DAY = 86400
TINY_MESSAGE_CEILING = 12
# droid's store predates the shared mapping's name for it
STORE_NAMES = {"factory": "droid"}
SANDBOX_DIRS = ("data", *(STORE_NAMES.get(store, store) for store in AGENT_STORE_VARS))
MEDIUM_MESSAGE_CEILING = 50
ExecProcess = Callable[[str, list[str], dict[str, str]], None]


class FixtureError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FixtureError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise FixtureError(f"{path} must contain a JSON object")
    return value


def _message_bucket(message_count: int) -> str:
    if message_count < TINY_MESSAGE_CEILING:
        return "tiny"
    if message_count <= MEDIUM_MESSAGE_CEILING:
        return "medium"
    return "long"


def validate_fixture(root: Path, descriptor: dict) -> tuple[dict, str]:
    manifest_path = root / "manifest.json"
    manifest = _load_json(manifest_path)
    fingerprint = _sha256(manifest_path)
    if fingerprint != descriptor.get("manifest_sha256"):
        raise FixtureError("fixture manifest fingerprint does not match its descriptor")
    if manifest.get("fixture_version") != descriptor.get("fixture_version"):
        raise FixtureError("fixture version does not match its descriptor")

    entries = manifest.get("files", [])
    expected = set()
    for entry in entries:
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise FixtureError(f"unsafe fixture path: {entry['path']}")
        if entry["path"] in expected:
            raise FixtureError(f"duplicate fixture path: {entry['path']}")
        expected.add(entry["path"])
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unexpected:
        raise FixtureError(f"unexpected file in fixture: {unexpected[0]}")
    if missing:
        raise FixtureError(f"fixture file is missing: {missing[0]}")

    source_counts = dict.fromkeys(descriptor.get("session_counts", {}), 0)
    raw_bytes = 0
    for entry in entries:
        path = root / entry["path"]
        if not path.is_file():
            raise FixtureError(f"fixture file is missing: {entry['path']}")
        if _sha256(path) != entry["sha256"]:
            raise FixtureError(f"fixture checksum mismatch: {entry['path']}")
        raw_bytes += path.stat().st_size
        if entry.get("kind") == "session":
            if entry.get("bucket") != _message_bucket(entry.get("message_count", 0)):
                raise FixtureError(f"fixture message bucket mismatch: {entry['path']}")
            if entry["source"] not in source_counts:
                raise FixtureError(f"fixture names an undeclared source: {entry['source']}")
            source_counts[entry["source"]] += 1

    if source_counts != descriptor.get("session_counts"):
        raise FixtureError("fixture session counts do not match its descriptor")
    if raw_bytes != descriptor.get("raw_bytes"):
        raise FixtureError("fixture byte count does not match its descriptor")
    return manifest, fingerprint


def build(
    fixture_root: Path,
    sandbox_root: Path,
    descriptor: dict,
    *,
    now: float | None = None,
    retention: int = 7,
) -> str:
    fixture_root = Path(fixture_root)
    sandbox_root = Path(sandbox_root)
    try:
        fixture_root.resolve().relative_to(sandbox_root.resolve())
    except ValueError:
        pass
    else:
        raise FixtureError("fixture cannot live inside the sandbox")
    manifest, fingerprint = validate_fixture(fixture_root, descriptor)
    if sandbox_root.name != ".sandbox":
        raise FixtureError(f"refusing to replace unexpected sandbox path: {sandbox_root}")

    if sandbox_root.exists():
        shutil.rmtree(sandbox_root)
    (sandbox_root / "data").mkdir(parents=True)
    # FACTORY_HOME_OVERRIDE is droid's HOME, so its store sits one .factory down
    (sandbox_root / "droid" / ".factory" / "sessions").mkdir(parents=True)
    for name in SANDBOX_DIRS:
        (sandbox_root / name).mkdir(exist_ok=True)

    stamp = time.time() if now is None else now
    for entry in manifest["files"]:
        source = fixture_root / entry["path"]
        if entry["kind"] == "session":
            destination = sandbox_root / entry["path"]
        else:
            destination = sandbox_root / "session_index.jsonl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if entry["kind"] == "session":
            age_days = entry["age_days"]
            mtime = stamp - age_days * DAY
            os.utime(destination, (mtime, mtime))

    settings = sandbox_root / "claude-settings.json"
    settings.write_text(json.dumps({"cleanupPeriodDays": retention}, indent=2) + "\n")

    print(f"sandbox: {sandbox_root}")
    print(f"  fixture: {descriptor['fixture_version']} ({fingerprint})")
    print(f"  sessions: {sum(descriptor['session_counts'].values())}; registry: empty\n")
    for name, value in sandbox_variables(sandbox_root).items():
        print(f'export {name}="{value}"')
    return fingerprint


def ensure_sandbox(
    fixture_root: Path,
    sandbox_root: Path,
    descriptor: dict,
    *,
    retention: int = 7,
) -> None:
    if (
        all((sandbox_root / name).is_dir() for name in SANDBOX_DIRS)
        and (sandbox_root / "claude-settings.json").is_file()
    ):
        return
    build(fixture_root, sandbox_root, descriptor, retention=retention)


def sandbox_variables(sandbox_root: Path) -> dict[str, str]:
    return {
        "XDG_DATA_HOME": str(sandbox_root / "data"),
        **agent_store_env(sandbox_root, prefix="", names=STORE_NAMES),
        "RINGLEADER_CLAUDE_SETTINGS": str(sandbox_root / "claude-settings.json"),
    }


def shell_environment(sandbox_root: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    current_path = environment.get("PATH")
    environment["PATH"] = str(REPO / ".venv" / "bin")
    if current_path:
        environment["PATH"] += os.pathsep + current_path
    environment.update(sandbox_variables(sandbox_root))
    return environment


def enter_shell(
    sandbox_root: Path,
    *,
    base: dict[str, str] | None = None,
    exec_process: ExecProcess = os.execvpe,
) -> None:
    environment = shell_environment(sandbox_root, base)
    shell = environment.get("SHELL") or "/bin/sh"
    print(f"sandbox: entering isolated shell at {sandbox_root}")
    print("  start an agent, then invoke the ringleader skill; exit when finished")
    exec_process(shell, [shell, "-i"], environment)


def main(
    argv: list[str] | None = None,
    *,
    exec_process: ExecProcess = os.execvpe,
) -> int:
    parser = argparse.ArgumentParser(prog="sandbox", description=__doc__)
    parser.add_argument("--retention", type=int, default=7)
    parser.add_argument("--shell", action="store_true")
    args = parser.parse_args(argv)
    try:
        descriptor = _load_json(DESCRIPTOR)
        fixture_root = (
            Path(os.environ["RINGLEADER_SANDBOX_FIXTURE_DIR"])
            if "RINGLEADER_SANDBOX_FIXTURE_DIR" in os.environ
            else Path.home()
            / ".local"
            / "share"
            / "ringleader"
            / "fixtures"
            / descriptor["fixture_version"]
        )
        if args.shell:
            ensure_sandbox(fixture_root, SANDBOX, descriptor, retention=args.retention)
            enter_shell(SANDBOX, exec_process=exec_process)
        else:
            build(fixture_root, SANDBOX, descriptor, retention=args.retention)
    except FixtureError as error:
        print(f"sandbox: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
