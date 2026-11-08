from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import shutil
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path

from ringleader.presence import VENDOR_PLATFORMS

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = ROOT / "tests" / "golden"
GOLDEN_TARBALL = "contract-golden.tar.gz"
CHECKSUMS = "SHA256SUMS"
REQUIRED_IN_WHEEL = (
    *(f"ringleader/_vendor/{platform}/hooklinesinker" for platform in VENDOR_PLATFORMS),
    "ringleader/_vendor/checksums.txt",
    "ringleader/_skills/ringleader-triage/SKILL.md",
    "ringleader/devtools/gui_world.py",
    "ringleader/devtools/tui_fixture.py",
)

type NamedBytes = tuple[str, bytes]


def project_version(pyproject: Path = ROOT / "pyproject.toml") -> str:
    with pyproject.open("rb") as handle:
        version: str = tomllib.load(handle)["project"]["version"]
    return version


def tag_problem(tag: str, version: str) -> str | None:
    expected = f"v{version}"
    return None if tag == expected else f"tag {tag!r} does not match pyproject version {expected!r}"


def wheel_name(version: str) -> str:
    return f"ringleader-{version}-py3-none-any.whl"


def missing_from_wheel(names: Iterable[str]) -> list[str]:
    present = set(names)
    return [name for name in REQUIRED_IN_WHEEL if name not in present]


def golden_tarball(files: Sequence[NamedBytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in sorted(files):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return packed.getvalue()


def sha256sums(files: Sequence[NamedBytes]) -> str:
    return "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files))


def _named(paths: Iterable[Path]) -> list[NamedBytes]:
    return [(path.name, path.read_bytes()) for path in paths]


def _check_tag(args: argparse.Namespace) -> int:
    problem = tag_problem(args.tag, project_version())
    if problem is not None:
        print(f"release: {problem}", file=sys.stderr)
        return 1
    return 0


def _assets(args: argparse.Namespace) -> int:
    version = project_version()
    wheel = Path(args.dist) / wheel_name(version)
    if not wheel.is_file():
        print(f"release: no wheel at {wheel}", file=sys.stderr)
        return 1
    with zipfile.ZipFile(wheel) as archive:
        missing = missing_from_wheel(archive.namelist())
    if missing:
        print(f"release: {wheel.name} is missing {', '.join(missing)}", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    shipped_wheel = out / wheel.name
    shutil.copyfile(wheel, shipped_wheel)
    tarball = out / GOLDEN_TARBALL
    tarball.write_bytes(golden_tarball(_named(GOLDEN_DIR.glob("contract-*.json"))))
    (out / CHECKSUMS).write_text(sha256sums(_named([shipped_wheel, tarball])), encoding="utf-8")
    print("\n".join(str(p) for p in (shipped_wheel, tarball, out / CHECKSUMS)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="release", description="Check and assemble release assets.")
    sub = ap.add_subparsers(dest="command", required=True)
    tag = sub.add_parser("check-tag", help="fail unless TAG is v<pyproject version>")
    tag.add_argument("tag")
    tag.set_defaults(run=_check_tag)
    assets = sub.add_parser("assets", help="verify the wheel and write the release assets")
    assets.add_argument("--dist", required=True, help="directory holding the built wheel")
    assets.add_argument("--out", required=True, help="directory to write the release assets into")
    assets.set_defaults(run=_assets)
    args = ap.parse_args(argv)
    code: int = args.run(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
