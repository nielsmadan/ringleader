"""Pin, fetch and vendor the helper binaries ringleader bundles (hooklinesinker, slimetrail).

uv run python scripts/vendored.py vendor [TOOL ...] [--dist DIR]   # stage the pinned releases
uv run python scripts/vendored.py update TOOL [VERSION]            # move a pin to a release
uv run python scripts/vendored.py check [TOOL ...]                 # warn about newer releases
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
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ringleader import presence

ROOT = Path(__file__).resolve().parents[1]
STAGER = ROOT / "scripts" / "vendor-binary.sh"
MANIFEST = "SHA256SUMS"
NOT_FOUND = 404
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
    def pin_path(self) -> Path:
        return ROOT / "scripts" / f"{self.name}.json"

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


def version_tuple(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version)
    if match is None:
        raise ValueError(f"expected a stable version such as 1.0.3, got {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def read_pin(tool: Tool, path: Path | None = None) -> Pin:
    path = path or tool.pin_path
    if not path.exists():
        raise ValueError(f"no {tool.name} pin at {path}; run `just update-{tool.name}`")
    raw = json.loads(path.read_text())
    version, protocol, hashes = raw.get("version"), raw.get("protocol"), raw.get("sha256")
    if not isinstance(version, str) or type(protocol) is not int:
        raise ValueError(f"{path} needs a string version and an integer protocol")
    version_tuple(version)
    if protocol != tool.protocol:
        raise ValueError(
            f"{path} pins {tool.name} protocol {protocol}, but ringleader speaks {tool.protocol}"
        )
    if not isinstance(hashes, dict) or not all(
        isinstance(hashes.get(name), str) and re.fullmatch(r"[0-9a-f]{64}", hashes[name])
        for name in tool.artifacts
    ):
        raise ValueError(f"{path} needs a sha256 for each of {', '.join(tool.artifacts)}")
    return Pin(version, protocol, {name: hashes[name] for name in tool.artifacts})


def write_pin(pin: Pin, path: Path) -> None:
    body = {"version": pin.version, "protocol": pin.protocol, "sha256": pin.sha256}
    text = json.dumps(body, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        handle.write(text)
    os.replace(handle.name, path)


def http_fetch(url: str) -> bytes:
    request = Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ringleader-vendor"},
    )
    with urlopen(request, timeout=30) as response:
        body: bytes = response.read()
    return body


def published_release(tool: Tool, version: str | None = None, fetch: Fetch = http_fetch) -> str:
    if version is not None:
        version = version.removeprefix("v")
        version_tuple(version)
    endpoint = f"tags/v{version}" if version else "latest"
    try:
        release = json.loads(
            fetch(f"https://api.github.com/repos/{tool.repository}/releases/{endpoint}")
        )
    except HTTPError as error:
        if error.code == NOT_FOUND:
            raise ValueError(f"no published {tool.name} release {version or 'latest'}") from error
        raise
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError(f"release has an unexpected tag: {tag!r}")
    if release.get("draft") or release.get("prerelease"):
        raise ValueError(f"{tag} is not a published stable release")
    found = tag.removeprefix("v")
    version_tuple(found)
    if version is not None and found != version:
        raise ValueError(f"asked for v{version}, got {tag}")
    assets = {asset.get("name") for asset in release.get("assets", [])}
    missing = sorted({*tool.artifacts, MANIFEST} - assets)
    if missing:
        raise ValueError(f"{tag} is missing release assets: {', '.join(missing)}")
    return found


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
            raise ValueError(f"{name} does not match the sha256 pinned in {tool.pin_path.name}")


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


def fetch_verified(tool: Tool, pin: Pin, fetch: Fetch = http_fetch, *, pinned: bool = True) -> Path:
    temporary = Path(tempfile.mkdtemp(prefix=f"ringleader-{tool.name}-"))
    try:
        download(tool, pin.version, temporary, fetch)
        verify_checksums(tool, temporary)
        if pinned:
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


def vendor(tool: Tool, dist: Path | None, pin_path: Path | None = None) -> None:
    if dist is None:
        pin = read_pin(tool, pin_path)
        dist = cached_release(tool, pin)
        print(f"{tool.name} {pin.version} (pinned) from {dist}", flush=True)
    stage(tool, dist)


def update(
    tool: Tool, version: str | None, pin_path: Path | None = None, fetch: Fetch = http_fetch
) -> None:
    pin_path = pin_path or tool.pin_path
    current = read_pin(tool, pin_path) if pin_path.exists() else None
    target = published_release(tool, version, fetch)
    if current is not None and version_tuple(target) < version_tuple(current.version):
        raise ValueError(f"refusing to downgrade {tool.name} from {current.version} to {target}")
    if current is not None and target == current.version:
        print(f"{tool.name} {target} is already pinned.")
        return
    fetched = fetch_verified(tool, Pin(target, tool.protocol), fetch, pinned=False)
    try:
        hashes = artifact_hashes(tool, fetched)
    finally:
        shutil.rmtree(fetched)
    write_pin(Pin(target, tool.protocol, hashes), pin_path)
    print(f"Pinned {tool.name} {current.version if current else '(none)'} -> {target}.")
    print("Next: just install, then commit the pin.")


def check(tool: Tool, pin_path: Path | None = None, fetch: Fetch = http_fetch) -> None:
    pin = read_pin(tool, pin_path)
    try:
        latest = published_release(tool, None, fetch)
    except (OSError, ValueError) as error:
        print(f"WARNING: could not check for {tool.name} updates: {error}", file=sys.stderr)
        return
    if version_tuple(latest) > version_tuple(pin.version):
        print(
            f"WARNING: {tool.name} {latest} is published; ringleader pins {pin.version}. "
            f"Run `just update-{tool.name}`.",
            file=sys.stderr,
        )
    else:
        print(f"{tool.name} pinned: {pin.version}; latest published: {latest}.")


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
    update_parser = commands.add_parser("update", help="pin a published release")
    update_parser.add_argument("tool", choices=list(TOOLS))
    update_parser.add_argument("version", nargs="?", help="defaults to the latest release")
    check_parser = commands.add_parser("check", help="warn when a newer release is published")
    check_parser.add_argument("tools", nargs="*", metavar="TOOL", help="default: all")
    args = parser.parse_args(argv)

    if args.command == "update":
        return _attempt(TOOLS[args.tool], lambda tool: update(tool, args.version))
    unknown = sorted(set(args.tools) - set(TOOLS))
    if unknown:
        parser.error(f"unknown tool {', '.join(unknown)}; choose from {', '.join(TOOLS)}")
    tools = _tools(args.tools)
    if args.command == "check":
        return max(_attempt(tool, check) for tool in tools)
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
