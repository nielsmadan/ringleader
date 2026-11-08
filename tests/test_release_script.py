import gzip
import hashlib
import importlib.util
import io
import tarfile
import zipfile
from importlib.metadata import version
from pathlib import Path

import pytest
from ringleader import cli

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_script", REPO / "scripts" / "release.py")
assert _spec is not None and _spec.loader is not None
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def test_the_tag_must_name_the_pyproject_version():
    assert release.tag_problem("v0.2.0", "0.2.0") is None
    assert release.tag_problem("v0.2.1", "0.2.0") == (
        "tag 'v0.2.1' does not match pyproject version 'v0.2.0'"
    )
    assert release.tag_problem("0.2.0", "0.2.0") == (
        "tag '0.2.0' does not match pyproject version 'v0.2.0'"
    )


def test_check_tag_exits_nonzero_and_names_the_mismatch(capsys):
    assert release.main(["check-tag", f"v{release.project_version()}"]) == 0
    assert release.main(["check-tag", "v0.0.0"]) == 1
    assert capsys.readouterr().err == (
        f"release: tag 'v0.0.0' does not match pyproject version 'v{release.project_version()}'\n"
    )


def test_rl_version_prints_the_installed_package_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == f"rl {version('ringleader')}\n"
    assert version("ringleader") == release.project_version()


def test_a_wheel_without_the_vendored_binaries_or_skills_is_incomplete():
    complete = list(release.REQUIRED_IN_WHEEL)
    assert release.missing_from_wheel([*complete, "ringleader/cli.py"]) == []
    without_linux = [n for n in complete if "linux-x86_64" not in n]
    assert release.missing_from_wheel(without_linux) == [
        "ringleader/_vendor/linux-x86_64/hooklinesinker"
    ]


def test_the_golden_tarball_is_flat_and_reproducible():
    files = [("contract-b.json", b"{}\n"), ("contract-a.json", b"[]\n")]
    packed = release.golden_tarball(files)
    assert packed == release.golden_tarball(list(reversed(files)))
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(packed))) as tar:
        assert tar.getnames() == ["contract-a.json", "contract-b.json"]
        member = tar.extractfile("contract-a.json")
        assert member is not None
        assert member.read() == b"[]\n"


def test_sha256sums_lists_each_asset_by_name():
    text = release.sha256sums([("b.whl", b"wheel"), ("a.tar.gz", b"")])
    assert text == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  a.tar.gz\n"
        "ba59926159d2aa256eb8739b8da7e2b574b960e1202c6d624cbe981cef996c91  b.whl\n"
    )


def _wheel(dist: Path, names) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    path = dist / release.wheel_name(release.project_version())
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, b"x")
    return path


def test_assets_needs_the_wheel_of_this_version(tmp_path, capsys):
    (tmp_path / "dist").mkdir()
    code = release.main(["assets", "--dist", str(tmp_path / "dist"), "--out", str(tmp_path / "o")])
    assert code == 1
    assert capsys.readouterr().err.startswith("release: no wheel at ")


def test_assets_refuses_an_incomplete_wheel_and_names_what_is_missing(tmp_path, capsys):
    _wheel(tmp_path / "dist", [n for n in release.REQUIRED_IN_WHEEL if "_skills" not in n])
    code = release.main(["assets", "--dist", str(tmp_path / "dist"), "--out", str(tmp_path / "o")])
    assert code == 1
    assert capsys.readouterr().err.endswith(
        "is missing ringleader/_skills/ringleader-triage/SKILL.md\n"
    )
    assert not (tmp_path / "o").exists()


def test_assets_writes_the_wheel_the_goldens_and_their_checksums(tmp_path):
    wheel = _wheel(tmp_path / "dist", release.REQUIRED_IN_WHEEL)
    out = tmp_path / "out"
    assert release.main(["assets", "--dist", str(tmp_path / "dist"), "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [wheel.name, release.GOLDEN_TARBALL, release.CHECKSUMS]
    )
    goldens = sorted(p.name for p in (REPO / "tests" / "golden").glob("contract-*.json"))
    tarball = (out / release.GOLDEN_TARBALL).read_bytes()
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(tarball))) as tar:
        assert tar.getnames() == goldens
    sums = (out / release.CHECKSUMS).read_text().splitlines()
    assert sums == [
        f"{hashlib.sha256((out / name).read_bytes()).hexdigest()}  {name}"
        for name in sorted((wheel.name, release.GOLDEN_TARBALL))
    ]


PYPROJECT = """[project]
name = "ringleader"
version = "0.2.0"

[tool.example]
target-version = "py313"
"""


def test_prepare_rewrites_only_the_project_version():
    assert release.with_version(PYPROJECT, "0.3.0") == PYPROJECT.replace(
        'version = "0.2.0"', 'version = "0.3.0"'
    )


def test_prepare_refuses_a_version_that_is_not_major_minor_patch():
    for bad in ("0.3", "v0.3.0", "0.03.0", "0.3.0rc1"):
        with pytest.raises(ValueError, match=r"is not a MAJOR\.MINOR\.PATCH version"):
            release.with_version(PYPROJECT, bad)


def test_prepare_refuses_a_second_version_line():
    doubled = PYPROJECT + '\n[tool.other]\nversion = "9.9.9"\n'
    with pytest.raises(ValueError, match="exactly one version line"):
        release.with_version(doubled, "0.3.0")


def test_prepare_command_reports_a_bad_version_and_leaves_pyproject_alone(capsys):
    before = (REPO / "pyproject.toml").read_bytes()
    assert release.main(["prepare", "1.0"]) == 1
    assert capsys.readouterr().err == "release: '1.0' is not a MAJOR.MINOR.PATCH version\n"
    assert (REPO / "pyproject.toml").read_bytes() == before
