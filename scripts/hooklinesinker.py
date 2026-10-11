"""Pin, fetch and vendor the hooklinesinker release that ringleader bundles.

uv run python scripts/hooklinesinker.py vendor [--dist DIR]   # stage the pinned release
uv run python scripts/hooklinesinker.py update [VERSION]      # move the pin to a release
uv run python scripts/hooklinesinker.py check                 # warn about a newer release
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
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ringleader import presence

REPOSITORY = "nielsmadan/hooklinesinker"
ROOT = Path(__file__).resolve().parents[1]
PIN_PATH = ROOT / "scripts" / "hooklinesinker.json"
CACHE = ROOT / "build" / "hooklinesinker" / "downloads"
STAGER = ROOT / "scripts" / "vendor-hooklinesinker.sh"
ARTIFACTS = tuple(
    f"hooklinesinker-{name}" for name in ("macos-universal", "linux-aarch64", "linux-x86_64")
)
MANIFEST = "SHA256SUMS"
NOT_FOUND = 404
CHECKSUM_FIELDS = 2

Fetch = Callable[[str], bytes]


@dataclass(frozen=True)
class Pin:
    version: str
    protocol: int


def version_tuple(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version)
    if match is None:
        raise ValueError(f"expected a stable version such as 1.0.3, got {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def read_pin(path: Path = PIN_PATH) -> Pin:
    raw = json.loads(path.read_text())
    version, protocol = raw.get("version"), raw.get("protocol")
    if not isinstance(version, str) or type(protocol) is not int:
        raise ValueError(f"{path} needs a string version and an integer protocol")
    version_tuple(version)
    if protocol != presence.PROTOCOL:
        raise ValueError(
            f"{path} pins protocol {protocol}, but ringleader speaks {presence.PROTOCOL}"
        )
    return Pin(version, protocol)


def write_pin(pin: Pin, path: Path = PIN_PATH) -> None:
    text = json.dumps({"version": pin.version, "protocol": pin.protocol}, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        handle.write(text)
    os.replace(handle.name, path)


def http_fetch(url: str) -> bytes:
    request = Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ringleader-hls-update"},
    )
    with urlopen(request, timeout=30) as response:
        body: bytes = response.read()
    return body


def published_release(version: str | None = None, fetch: Fetch = http_fetch) -> str:
    if version is not None:
        version = version.removeprefix("v")
        version_tuple(version)
    endpoint = f"tags/v{version}" if version else "latest"
    try:
        release = json.loads(
            fetch(f"https://api.github.com/repos/{REPOSITORY}/releases/{endpoint}")
        )
    except HTTPError as error:
        if error.code == NOT_FOUND:
            raise ValueError(
                f"no published hooklinesinker release {version or 'latest'}"
            ) from error
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
    missing = sorted({*ARTIFACTS, MANIFEST} - assets)
    if missing:
        raise ValueError(f"{tag} is missing release assets: {', '.join(missing)}")
    return found


def download(version: str, destination: Path, fetch: Fetch = http_fetch) -> None:
    base = f"https://github.com/{REPOSITORY}/releases/download/v{version}"
    destination.mkdir(parents=True, exist_ok=True)
    for name in (*ARTIFACTS, MANIFEST):
        (destination / name).write_bytes(fetch(f"{base}/{name}"))


def manifest_entries(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == CHECKSUM_FIELDS:
            entries[parts[1].lstrip("*").removeprefix("./")] = parts[0].lower()
    return entries


def verify_checksums(dist: Path) -> None:
    entries = manifest_entries((dist / MANIFEST).read_text())
    for name in ARTIFACTS:
        expected = entries.get(name)
        if expected is None or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(f"{MANIFEST} has no valid checksum for {name}")
        if hashlib.sha256((dist / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"checksum mismatch for {name}")


def verify_binary(dist: Path, pin: Pin) -> str:
    binary = dist / f"hooklinesinker-{presence.artifact_dir()}"
    binary.chmod(0o755)
    result = subprocess.run(
        [str(binary), "version", "--json"], capture_output=True, text=True, timeout=15, check=False
    )
    if result.returncode:
        raise ValueError(f"{binary.name} version failed: {result.stderr.strip()}")
    reported = json.loads(result.stdout)
    if reported != {"version": pin.version, "protocol": pin.protocol}:
        raise ValueError(f"{binary.name} reports {reported}, pinned {pin}")
    return binary.name


def fetch_verified(pin: Pin, fetch: Fetch = http_fetch) -> Path:
    temporary = Path(tempfile.mkdtemp(prefix="ringleader-hls-"))
    try:
        download(pin.version, temporary, fetch)
        verify_checksums(temporary)
        verify_binary(temporary, pin)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return temporary


def cached_release(pin: Pin, cache: Path = CACHE, fetch: Fetch = http_fetch) -> Path:
    target = cache / pin.version
    if target.is_dir():
        try:
            verify_checksums(target)
            verify_binary(target, pin)
        except (OSError, ValueError):
            shutil.rmtree(target)
        else:
            return target
    fetched = fetch_verified(pin, fetch)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(fetched, target)
    return target


def stage(dist: Path, stager: Path = STAGER) -> None:
    subprocess.run(["bash", str(stager), str(dist)], check=True, timeout=120)


def vendor(dist: Path | None, pin_path: Path = PIN_PATH, cache: Path = CACHE) -> None:
    if dist is None:
        pin = read_pin(pin_path)
        dist = cached_release(pin, cache)
        print(f"hooklinesinker {pin.version} (pinned) from {dist}", flush=True)
    stage(dist)


def update(version: str | None, pin_path: Path = PIN_PATH, fetch: Fetch = http_fetch) -> None:
    current = read_pin(pin_path)
    target = published_release(version, fetch)
    if version_tuple(target) < version_tuple(current.version):
        raise ValueError(f"refusing to downgrade hooklinesinker from {current.version} to {target}")
    if target == current.version:
        print(f"hooklinesinker {target} is already pinned.")
        return
    candidate = Pin(target, current.protocol)
    shutil.rmtree(fetch_verified(candidate, fetch))
    write_pin(candidate, pin_path)
    print(f"Pinned hooklinesinker {current.version} -> {target}.")
    print("Next: just install, check `rl data presence status`, then commit the pin.")


def check(pin_path: Path = PIN_PATH, fetch: Fetch = http_fetch) -> None:
    pin = read_pin(pin_path)
    try:
        latest = published_release(None, fetch)
    except (OSError, ValueError) as error:
        print(f"WARNING: could not check for hooklinesinker updates: {error}", file=sys.stderr)
        return
    if version_tuple(latest) > version_tuple(pin.version):
        print(
            f"WARNING: hooklinesinker {latest} is published; ringleader pins {pin.version}. "
            "Run `just update-hooklinesinker`.",
            file=sys.stderr,
        )
    else:
        print(f"hooklinesinker pinned: {pin.version}; latest published: {latest}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    vendor_parser = commands.add_parser("vendor", help="stage the pinned release into _vendor")
    vendor_parser.add_argument(
        "--dist",
        type=Path,
        default=os.environ.get("HOOKLINESINKER_DIST") or None,
        help="stage a local release build instead of the pinned download",
    )
    update_parser = commands.add_parser("update", help="pin a published release")
    update_parser.add_argument("version", nargs="?", help="defaults to the latest release")
    commands.add_parser("check", help="warn when a newer release is published")
    args = parser.parse_args(argv)
    try:
        if args.command == "vendor":
            vendor(args.dist)
        elif args.command == "update":
            update(args.version)
        else:
            check()
    except (OSError, ValueError, subprocess.SubprocessError, presence.PresenceUnavailable) as error:
        print(f"hooklinesinker: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
