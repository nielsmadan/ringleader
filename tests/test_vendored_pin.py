from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("vendored_script", REPO / "scripts" / "vendored.py")
assert _spec is not None and _spec.loader is not None
vendored = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vendored
_spec.loader.exec_module(vendored)


@pytest.fixture(params=sorted(vendored.TOOLS))
def tool(request):
    return vendored.TOOLS[request.param]


def binary(tool, version: str, protocol: int = 1) -> bytes:
    if tool.name == "hooklinesinker":
        report = json.dumps({"version": version, "protocol": protocol})
        return f"#!/bin/sh\necho '{report}'\n".encode()
    return f'#!/bin/sh\n[ "$1" = --version ] || exit 2\necho "slimetrail {version}"\n'.encode()


class FakeGitHub:
    def __init__(self, tool) -> None:
        self.tool = tool
        self.download = f"https://github.com/nielsmadan/{tool.name}/releases/download/"
        self.requests: list[str] = []

    def files(self, version: str) -> dict[str, bytes]:
        artifacts = {name: binary(self.tool, version) for name in self.tool.artifacts}
        manifest = "".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in artifacts.items()
        )
        return {**artifacts, vendored.MANIFEST: manifest.encode()}

    def __call__(self, url: str) -> bytes:
        self.requests.append(url)
        assert url.startswith(self.download), url
        tag, name = url.removeprefix(self.download).split("/")
        return self.files(tag.removeprefix("v"))[name]


def pinned(tool, version: str) -> object:
    hashes = {name: hashlib.sha256(binary(tool, version)).hexdigest() for name in tool.artifacts}
    return vendored.Pin(version, 1, hashes)


def pins_entry(pin) -> dict[str, object]:
    return {
        "repository": "nielsmadan/tool",
        "version": pin.version,
        "commit": "a" * 40,
        "assets": {name: f"sha256:{digest}" for name, digest in pin.sha256.items()},
    }


@pytest.fixture
def pin_path(tmp_path: Path, tool) -> Path:
    path = tmp_path / "pins.json"
    path.write_text(json.dumps({tool.name: pins_entry(pinned(tool, "1.0.3"))}))
    return path


def test_every_tool_vendors_the_same_platforms_from_its_own_repository():
    assert {tool.repository for tool in vendored.TOOLS.values()} == {
        "nielsmadan/hooklinesinker",
        "nielsmadan/slimetrail",
    }
    assert vendored.TOOLS["slimetrail"].artifacts == (
        "slimetrail-macos-universal",
        "slimetrail-linux-aarch64",
        "slimetrail-linux-x86_64",
    )


def test_read_pin_returns_the_pinned_version(tool, pin_path):
    assert vendored.read_pin(tool, pin_path) == pinned(tool, "1.0.3")


def test_read_pin_refuses_a_pin_without_every_platform_digest(tool, tmp_path):
    entry = pins_entry(pinned(tool, "1.0.3"))
    entry["assets"] = {tool.artifacts[0]: entry["assets"][tool.artifacts[0]]}
    path = tmp_path / "pins.json"
    path.write_text(json.dumps({tool.name: entry}))

    with pytest.raises(ValueError, match=f"needs a sha256 digest for {tool.artifacts[1]}"):
        vendored.read_pin(tool, path)


def test_a_missing_pin_names_the_command_that_adds_it(tool, tmp_path):
    with pytest.raises(ValueError, match="add one with `curtains pins add`"):
        vendored.read_pin(tool, tmp_path / "absent.json")


def test_the_repository_pins_every_vendored_platform(tool):
    assert vendored.read_pin(tool).sha256.keys() == set(tool.artifacts)


def test_cached_release_downloads_once_then_reuses_the_cache(tool, tmp_path):
    github = FakeGitHub(tool)
    pin = pinned(tool, "1.0.3")

    first = vendored.cached_release(tool, pin, tmp_path / "cache", github)
    downloads = len(github.requests)
    second = vendored.cached_release(tool, pin, tmp_path / "cache", github)

    assert first == second == tmp_path / "cache" / "1.0.3"
    assert len(github.requests) == downloads
    vendored.verify_checksums(tool, second)


def test_cached_release_replaces_a_damaged_cache(tool, tmp_path):
    github = FakeGitHub(tool)
    pin = pinned(tool, "1.0.3")
    cached = vendored.cached_release(tool, pin, tmp_path / "cache", github)
    (cached / tool.artifacts[0]).write_bytes(b"tampered")

    repaired = vendored.cached_release(tool, pin, tmp_path / "cache", github)

    assert (repaired / tool.artifacts[0]).read_bytes() == binary(tool, "1.0.3")


def test_verify_checksums_rejects_a_mismatched_artifact(tool, tmp_path):
    github = FakeGitHub(tool)
    vendored.download(tool, "1.0.3", tmp_path, github)
    (tmp_path / tool.artifacts[1]).write_bytes(b"other")

    with pytest.raises(ValueError, match=f"checksum mismatch for {tool.artifacts[1]}"):
        vendored.verify_checksums(tool, tmp_path)


def test_cached_release_refuses_a_release_that_no_longer_matches_the_pin(tool, tmp_path):
    github = FakeGitHub(tool)
    swapped = {name: hashlib.sha256(b"earlier upload").hexdigest() for name in tool.artifacts}

    with pytest.raises(ValueError, match="does not match the sha256 pinned"):
        vendored.cached_release(tool, vendored.Pin("1.0.3", 1, swapped), tmp_path / "cache", github)

    assert not (tmp_path / "cache" / "1.0.3").exists()


def test_vendor_keeps_staging_the_other_tools_when_one_fails(monkeypatch, capsys):
    staged = []

    def fake_vendor(tool, dist, pin_path=None):
        if tool.name == "slimetrail":
            raise ValueError("no published slimetrail release latest")
        staged.append(tool.name)

    monkeypatch.setattr(vendored, "vendor", fake_vendor)

    assert vendored.main(["vendor"]) == 1
    assert staged == ["hooklinesinker"]
    assert "slimetrail: no published slimetrail release latest" in capsys.readouterr().err


def test_vendor_takes_a_local_dist_for_one_named_tool(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(vendored, "vendor", lambda tool, dist: calls.append((tool.name, dist)))

    assert vendored.main(["vendor", "slimetrail", "--dist", str(tmp_path)]) == 0
    assert calls == [("slimetrail", tmp_path)]


def test_vendor_reads_each_tools_dist_from_its_own_variable(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(vendored, "vendor", lambda tool, dist: calls.append((tool.name, dist)))
    monkeypatch.setenv("SLIMETRAIL_DIST", str(tmp_path))
    monkeypatch.delenv("HOOKLINESINKER_DIST", raising=False)

    assert vendored.main(["vendor"]) == 0
    assert calls == [("hooklinesinker", None), ("slimetrail", tmp_path)]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["vendor", "--dist", "/tmp/x"], "--dist stages one tool"),
        (["vendor", "nonesuch"], "unknown tool nonesuch"),
    ],
)
def test_vendor_rejects_an_ambiguous_or_unknown_request(argv, message, capsys):
    with pytest.raises(SystemExit) as exited:
        vendored.main(argv)

    assert exited.value.code == 2
    assert message in capsys.readouterr().err
