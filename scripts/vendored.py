"""Fetch and vendor the helper binaries ringleader bundles (hooklinesinker, slimetrail).

uv run python scripts/vendored.py vendor [TOOL ...] [--dist DIR]   # stage the pinned releases

The pins live in scripts/pins.json; `curtains pins update TOOL` moves them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import Request, urlopen

from ringleader import presence

ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "scripts" / "pins.json"
STAGER = ROOT / "scripts" / "vendor-binary.sh"
MANIFEST = "SHA256SUMS"
CHECKSUM_FIELDS = 2
VERSION_TIMEOUT = 15

Fetch = Callable[[str], bytes]


@dataclass(frozen=True)
class Pin:
    version: str
    protocol: int
    sha256: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Tool:
    name: str
    protocol: int
    version_args: tuple[str, ...]
    reports: Callable[[str, Pin], bool]

    @property
    def repository(self) -> str:
        return f"nielsmadan/{self.name}"

    @property
    def cache(self) -> Path:
        return ROOT / "build" / self.name / "downloads"

    @property
    def artifacts(self) -> tuple[str, ...]:
        return tuple(f"{self.name}-{platform}" for platform in presence.VENDOR_PLATFORMS)

    @property
    def dist_variable(self) -> str:
        return f"{self.name.upper()}_DIST"


def _reports_version_json(stdout: str, pin: Pin) -> bool:
    try:
        reported = json.loads(stdout)
    except ValueError:
        return False
    return bool(reported == {"version": pin.version, "protocol": pin.protocol})


def _reports_version_line(stdout: str, pin: Pin) -> bool:
    return stdout.strip() == f"slimetrail {pin.version}"


TOOLS = {
    tool.name: tool
    for tool in (
        Tool("hooklinesinker", presence.PROTOCOL, ("version", "--json"), _reports_version_json),
        Tool("slimetrail", 1, ("--version",), _reports_version_line),
    )
}


def read_pin(tool: Tool, path: Path = PINS) -> Pin:
    entry = (json.loads(path.read_text()) if path.exists() else {}).get(tool.name)
    if entry is None:
        raise ValueError(f"{path} has no {tool.name} pin; add one with `curtains pins add`")
    digests = {
        name: digest.removeprefix("sha256:")
        for name, digest in entry["assets"].items()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    }
    missing = [name for name in tool.artifacts if name not in digests]
    if missing:
        raise ValueError(f"{path} needs a sha256 digest for {', '.join(missing)}")
    return Pin(entry["version"], tool.protocol, {name: digests[name] for name in tool.artifacts})


def http_fetch(url: str) -> bytes:
    request = Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ringleader-vendor"},
    )
    with urlopen(request, timeout=30) as response:
        body: bytes = response.read()
    return body


def download(tool: Tool, version: str, destination: Path, fetch: Fetch = http_fetch) -> None:
    base = f"https://github.com/{tool.repository}/releases/download/v{version}"
    destination.mkdir(parents=True, exist_ok=True)
    for name in (*tool.artifacts, MANIFEST):
        (destination / name).write_bytes(fetch(f"{base}/{name}"))


def manifest_entries(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == CHECKSUM_FIELDS:
            entries[parts[1].lstrip("*").removeprefix("./")] = parts[0].lower()
    return entries


def verify_checksums(tool: Tool, dist: Path) -> None:
    entries = manifest_entries((dist / MANIFEST).read_text())
    for name in tool.artifacts:
        expected = entries.get(name)
        if expected is None or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(f"{MANIFEST} has no valid checksum for {name}")
        if hashlib.sha256((dist / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"checksum mismatch for {name}")


def artifact_hashes(tool: Tool, dist: Path) -> dict[str, str]:
    return {name: hashlib.sha256((dist / name).read_bytes()).hexdigest() for name in tool.artifacts}


def verify_pinned(tool: Tool, dist: Path, pin: Pin) -> None:
    actual = artifact_hashes(tool, dist)
    for name in tool.artifacts:
        if actual[name] != pin.sha256.get(name):
            raise ValueError(f"{name} does not match the sha256 pinned in {PINS.name}")


def verify_binary(tool: Tool, dist: Path, pin: Pin) -> str:
    binary = dist / f"{tool.name}-{presence.artifact_dir()}"
    binary.chmod(0o755)
    result = subprocess.run(
        [str(binary), *tool.version_args],
        capture_output=True,
        text=True,
        timeout=VERSION_TIMEOUT,
        check=False,
    )
    if result.returncode:
        raise ValueError(
            f"{binary.name} {' '.join(tool.version_args)} failed: {result.stderr.strip()}"
        )
    if not tool.reports(result.stdout, pin):
        raise ValueError(f"{binary.name} reports {result.stdout.strip()!r}, pinned {pin.version}")
    return binary.name


def fetch_verified(tool: Tool, pin: Pin, fetch: Fetch = http_fetch) -> Path:
    temporary = Path(tempfile.mkdtemp(prefix=f"ringleader-{tool.name}-"))
    try:
        download(tool, pin.version, temporary, fetch)
        verify_checksums(tool, temporary)
        verify_pinned(tool, temporary, pin)
        verify_binary(tool, temporary, pin)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return temporary


def cached_release(
    tool: Tool, pin: Pin, cache: Path | None = None, fetch: Fetch = http_fetch
) -> Path:
    target = (cache or tool.cache) / pin.version
    if target.is_dir():
        try:
            verify_checksums(tool, target)
            verify_pinned(tool, target, pin)
            verify_binary(tool, target, pin)
        except (OSError, ValueError):
            shutil.rmtree(target)
        else:
            return target
    fetched = fetch_verified(tool, pin, fetch)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(fetched, target)
    return target


def stage(tool: Tool, dist: Path, stager: Path = STAGER) -> None:
    subprocess.run(["bash", str(stager), tool.name, str(dist)], check=True, timeout=120)


def vendor(tool: Tool, dist: Path | None, pin_path: Path = PINS) -> None:
    if dist is None:
        pin = read_pin(tool, pin_path)
        dist = cached_release(tool, pin)
        print(f"{tool.name} {pin.version} (pinned) from {dist}", flush=True)
    stage(tool, dist)


def _tools(names: list[str]) -> list[Tool]:
    return [TOOLS[name] for name in names] if names else list(TOOLS.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    vendor_parser = commands.add_parser("vendor", help="stage pinned releases into _vendor")
    vendor_parser.add_argument("tools", nargs="*", metavar="TOOL", help="default: all")
    vendor_parser.add_argument(
        "--dist",
        type=Path,
        help="stage a local release build of one TOOL instead of its pinned download "
        "($HOOKLINESINKER_DIST / $SLIMETRAIL_DIST do the same per tool)",
    )
    args = parser.parse_args(argv)

    unknown = sorted(set(args.tools) - set(TOOLS))
    if unknown:
        parser.error(f"unknown tool {', '.join(unknown)}; choose from {', '.join(TOOLS)}")
    tools = _tools(args.tools)
    if args.dist is not None and len(tools) != 1:
        parser.error("--dist stages one tool; name it, e.g. `vendor slimetrail --dist DIR`")
    # every tool is attempted, so one unreachable release still stages the others
    failures = [
        _attempt(tool, lambda tool: vendor(tool, args.dist or _dist_from_env(tool)))
        for tool in tools
    ]
    return max(failures)


def _dist_from_env(tool: Tool) -> Path | None:
    value = os.environ.get(tool.dist_variable)
    return Path(value) if value else None


def _attempt(tool: Tool, action: Callable[[Tool], None]) -> int:
    try:
        action(tool)
    except (OSError, ValueError, subprocess.SubprocessError, presence.PresenceUnavailable) as error:
        print(f"{tool.name}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
