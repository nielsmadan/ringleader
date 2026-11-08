from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
from ringleader import presence

REPO = Path(__file__).resolve().parent.parent
ARTIFACT_DIRS = ("macos-universal", "linux-aarch64", "linux-x86_64")


@pytest.fixture
def vendor_release(tmp_path):
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "vendor-hooklinesinker.sh"
    shutil.copy(REPO / "scripts" / script.name, script)
    (project / "ringleader").mkdir()
    stage_artifacts(project)
    dist = tmp_path / "release"
    dist.mkdir()
    for artifact in ARTIFACT_DIRS:
        (dist / f"hooklinesinker-{artifact}").write_bytes(f"release {artifact}\n".encode())
    return script, dist, project / "ringleader" / "_vendor"


def release_manifest(dist, artifacts=ARTIFACT_DIRS, separator="  ", prefix=""):
    lines = []
    for artifact in artifacts:
        name = f"hooklinesinker-{artifact}"
        digest = presence.sha256_file(dist / name)
        lines.append(f"{digest}{separator}{prefix}{name}\n")
    (dist / "SHA256SUMS").write_text("".join(lines))


def bundle_contents(vendor):
    return {
        str(path.relative_to(vendor)): (path.read_bytes(), path.stat().st_mode)
        for path in vendor.rglob("*")
        if path.is_file()
    }


def run_vendor(script, dist):
    return subprocess.run(
        ["bash", str(script), str(dist)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("separator", ["  ", " *"])
@pytest.mark.parametrize("prefix", ["", "./"])
def test_vendor_stages_every_verified_release_artifact(vendor_release, separator, prefix):
    script, dist, vendor = vendor_release
    release_manifest(dist, separator=separator, prefix=prefix)

    result = run_vendor(script, dist)

    assert result.returncode == 0, result.stdout + result.stderr
    expected_manifest = {}
    for artifact in ARTIFACT_DIRS:
        source = dist / f"hooklinesinker-{artifact}"
        installed = vendor / artifact / "hooklinesinker"
        assert installed.read_bytes() == source.read_bytes()
        assert installed.stat().st_mode & 0o111 == 0o111
        expected_manifest[f"{artifact}/hooklinesinker"] = presence.sha256_file(source)
    assert presence.read_manifest(vendor) == expected_manifest


def test_vendor_keeps_the_bundle_when_manifest_omits_an_artifact(vendor_release):
    script, dist, vendor = vendor_release
    release_manifest(dist, artifacts=ARTIFACT_DIRS[:1])
    before = bundle_contents(vendor)

    result = run_vendor(script, dist)

    assert result.returncode == 1
    assert "no checksum for hooklinesinker-linux-aarch64" in result.stderr
    assert bundle_contents(vendor) == before


def test_vendor_keeps_the_bundle_when_a_checksum_mismatches(vendor_release):
    script, dist, vendor = vendor_release
    release_manifest(dist)
    (dist / "hooklinesinker-linux-x86_64").write_text("tampered release\n")
    before = bundle_contents(vendor)

    result = run_vendor(script, dist)

    assert result.returncode == 1
    assert "checksum mismatch for hooklinesinker-linux-x86_64" in result.stderr
    assert bundle_contents(vendor) == before


def test_vendor_keeps_the_bundle_when_a_release_artifact_is_missing(vendor_release):
    script, dist, vendor = vendor_release
    release_manifest(dist, artifacts=ARTIFACT_DIRS[:-1])
    (dist / "hooklinesinker-linux-x86_64").unlink()
    before = bundle_contents(vendor)

    result = run_vendor(script, dist)

    assert result.returncode == 1
    assert "hooklinesinker-linux-x86_64 is missing" in result.stderr
    assert bundle_contents(vendor) == before


def test_pyproject_ships_the_vendor_tree_in_the_wheel():
    """The staged binaries are gitignored, so hatchling drops them from the wheel unless
    they are declared as build artifacts."""
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert any("_vendor" in pattern for pattern in wheel["artifacts"])


def test_vendor_placeholder_is_tracked():
    assert (REPO / "ringleader" / "_vendor" / ".gitkeep").is_file()


def stage_project(root: Path) -> Path:
    project = root / "project"
    project.mkdir()
    for name in ("pyproject.toml", "README.md", ".gitignore"):
        shutil.copy(REPO / name, project / name)
    shutil.copytree(
        REPO / "ringleader",
        project / "ringleader",
        ignore=shutil.ignore_patterns("__pycache__", "_vendor"),
    )
    (project / ".agents").mkdir()
    shutil.copytree(REPO / ".agents" / "skills", project / ".agents" / "skills")
    return project


def stage_artifacts(project: Path) -> dict[str, str]:
    """Synthetic binaries plus the manifest the vendor script writes."""
    vendor = project / "ringleader" / "_vendor"
    vendor.mkdir()
    (vendor / ".gitkeep").touch()
    digests = {}
    lines = []
    for artifact in ARTIFACT_DIRS:
        body = f"#!/bin/sh\necho {artifact}\n".encode()
        binary = vendor / artifact / "hooklinesinker"
        binary.parent.mkdir()
        binary.write_bytes(body)
        binary.chmod(0o755)
        digest = hashlib.sha256(body).hexdigest()
        digests[f"{artifact}/hooklinesinker"] = digest
        lines.append(f"{digest}  {artifact}/hooklinesinker")
    (vendor / presence.CHECKSUM_MANIFEST).write_text("\n".join(lines) + "\n")
    return digests


def build_wheel(project: Path) -> zipfile.ZipFile:
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(project / "dist")],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    built = sorted((project / "dist").glob("*.whl"))
    assert len(built) == 1
    return zipfile.ZipFile(built[0])


needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not on PATH")


@needs_uv
def test_wheel_carries_every_staged_artifact_and_the_manifest(tmp_path):
    project = stage_project(tmp_path)
    digests = stage_artifacts(project)
    wheel = build_wheel(project)

    names = set(wheel.namelist())
    for relative, digest in digests.items():
        member = f"ringleader/_vendor/{relative}"
        assert member in names
        assert hashlib.sha256(wheel.read(member)).hexdigest() == digest
    manifest = wheel.read(f"ringleader/_vendor/{presence.CHECKSUM_MANIFEST}").decode()
    for relative, digest in digests.items():
        assert f"{digest}  {relative}" in manifest


@needs_uv
def test_a_built_wheel_has_everything_the_release_requires(tmp_path):
    spec = importlib.util.spec_from_file_location("release_check", REPO / "scripts" / "release.py")
    assert spec is not None and spec.loader is not None
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    project = stage_project(tmp_path)
    stage_artifacts(project)

    assert release.missing_from_wheel(build_wheel(project).namelist()) == []


@needs_uv
def test_wheel_keeps_the_artifacts_executable(tmp_path):
    """A binary that arrives without its exec bit cannot be promoted by the installer."""
    project = stage_project(tmp_path)
    stage_artifacts(project)
    wheel = build_wheel(project)

    for artifact in ARTIFACT_DIRS:
        info = wheel.getinfo(f"ringleader/_vendor/{artifact}/hooklinesinker")
        assert (info.external_attr >> 16) & 0o111 == 0o111


@needs_uv
def test_wheel_builds_without_staged_artifacts(tmp_path):
    """`uv tool install .` on a bare checkout still ships everything but presence."""
    project = stage_project(tmp_path)
    (project / "ringleader" / "_vendor").mkdir()
    (project / "ringleader" / "_vendor" / ".gitkeep").touch()
    wheel = build_wheel(project)

    names = wheel.namelist()
    assert "ringleader/presence.py" in names
    assert not [n for n in names if n.endswith("_vendor/checksums.txt")]


@pytest.mark.parametrize(
    "system,machine,artifact",
    [
        ("Darwin", "arm64", "macos-universal"),
        ("Darwin", "x86_64", "macos-universal"),
        ("Linux", "aarch64", "linux-aarch64"),
        ("Linux", "x86_64", "linux-x86_64"),
    ],
)
def test_runtime_selection_finds_the_staged_binary(tmp_path, system, machine, artifact):
    vendor = tmp_path / "_vendor"
    body = b"#!/bin/sh\nexit 0\n"
    binary = vendor / artifact / "hooklinesinker"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(body)
    (vendor / presence.CHECKSUM_MANIFEST).write_text(
        f"{hashlib.sha256(body).hexdigest()}  {artifact}/hooklinesinker\n"
    )
    client = presence.PresenceClient(
        executable=tmp_path / "absent",
        runner=lambda cmd: presence.RunResult(0, "", ""),
        system=system,
        machine=machine,
        vendor_dir=vendor,
    )
    assert client.candidate_path() == binary


STAGED = presence.vendor_root() / presence.CHECKSUM_MANIFEST


@pytest.mark.skipif(not STAGED.is_file(), reason="no vendored artifacts staged in this checkout")
def test_staged_artifacts_match_their_manifest():
    root = presence.vendor_root()
    entries = presence.read_manifest(root)
    assert entries
    for relative, digest in entries.items():
        path = root / relative
        assert path.is_file(), f"{relative} is listed in the manifest but not staged"
        assert presence.sha256_file(path) == digest
