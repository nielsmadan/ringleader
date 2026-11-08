from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hls_script", REPO / "scripts" / "hooklinesinker.py")
assert _spec is not None and _spec.loader is not None
hls = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = hls
_spec.loader.exec_module(hls)

API = "https://api.github.com/repos/nielsmadan/hooklinesinker/releases/"
DOWNLOAD = "https://github.com/nielsmadan/hooklinesinker/releases/download/"


def binary(version: str, protocol: int = 1) -> bytes:
    report = json.dumps({"version": version, "protocol": protocol})
    return f"#!/bin/sh\necho '{report}'\n".encode()


def release(version: str, *, assets=None, prerelease=False) -> dict[str, object]:
    names = assets if assets is not None else [*hls.ARTIFACTS, hls.MANIFEST]
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": prerelease,
        "assets": [{"name": name} for name in names],
    }


class FakeGitHub:
    def __init__(self, releases: dict[str, dict[str, object]], latest: str) -> None:
        self.releases = releases
        self.latest = latest
        self.requests: list[str] = []

    def files(self, version: str) -> dict[str, bytes]:
        artifacts = {name: binary(version) for name in hls.ARTIFACTS}
        manifest = "".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in artifacts.items()
        )
        return {**artifacts, hls.MANIFEST: manifest.encode()}

    def __call__(self, url: str) -> bytes:
        self.requests.append(url)
        if url.startswith(API):
            endpoint = url.removeprefix(API)
            version = self.latest if endpoint == "latest" else endpoint.removeprefix("tags/v")
            if version not in self.releases:
                raise HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
            return json.dumps(self.releases[version]).encode()
        tag, name = url.removeprefix(DOWNLOAD).split("/")
        return self.files(tag.removeprefix("v"))[name]


def pinned(version: str) -> object:
    hashes = {name: hashlib.sha256(binary(version)).hexdigest() for name in hls.ARTIFACTS}
    return hls.Pin(version, 1, hashes)


@pytest.fixture
def pin_path(tmp_path: Path) -> Path:
    path = tmp_path / "hooklinesinker.json"
    hls.write_pin(pinned("1.0.3"), path)
    return path


def test_read_pin_returns_the_pinned_version(pin_path):
    assert hls.read_pin(pin_path) == pinned("1.0.3")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ({"version": "1.0", "protocol": 1}, "stable version"),
        ({"version": "1.0.3-rc1", "protocol": 1}, "stable version"),
        ({"version": "1.0.3", "protocol": 2}, "speaks 1"),
        ({"version": "1.0.3"}, "integer protocol"),
        ({"version": "1.0.3", "protocol": 1}, "needs a sha256"),
        ({"version": "1.0.3", "protocol": 1, "sha256": {hls.ARTIFACTS[0]: "ab"}}, "needs a sha256"),
    ],
)
def test_read_pin_refuses_an_invalid_pin(tmp_path, content, message):
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(content))

    with pytest.raises(ValueError, match=message):
        hls.read_pin(path)


def test_update_pins_the_latest_release_after_verifying_it(pin_path):
    github = FakeGitHub({"1.0.4": release("1.0.4")}, latest="1.0.4")

    hls.update(None, pin_path, github)

    assert hls.read_pin(pin_path) == pinned("1.0.4")
    assert f"{DOWNLOAD}v1.0.4/{hls.MANIFEST}" in github.requests


def test_update_pins_a_requested_version(pin_path):
    github = FakeGitHub({"1.0.5": release("1.0.5"), "1.0.6": release("1.0.6")}, latest="1.0.6")

    hls.update("v1.0.5", pin_path, github)

    assert hls.read_pin(pin_path) == pinned("1.0.5")


def test_update_refuses_to_downgrade(pin_path):
    github = FakeGitHub({"1.0.2": release("1.0.2")}, latest="1.0.2")

    with pytest.raises(ValueError, match="downgrade"):
        hls.update("1.0.2", pin_path, github)

    assert hls.read_pin(pin_path) == pinned("1.0.3")


@pytest.mark.parametrize(
    ("published", "message"),
    [
        (release("1.0.4", assets=[hls.ARTIFACTS[0], hls.MANIFEST]), "missing release assets"),
        (release("1.0.4", prerelease=True), "not a published stable release"),
    ],
)
def test_update_refuses_an_incomplete_or_unstable_release(pin_path, published, message):
    github = FakeGitHub({"1.0.4": published}, latest="1.0.4")

    with pytest.raises(ValueError, match=message):
        hls.update(None, pin_path, github)

    assert hls.read_pin(pin_path) == pinned("1.0.3")


def test_update_refuses_a_binary_reporting_another_version(pin_path, monkeypatch):
    github = FakeGitHub({"1.0.4": release("1.0.4")}, latest="1.0.4")
    monkeypatch.setattr(github, "files", lambda version: FakeGitHub.files(github, "9.9.9"))

    with pytest.raises(ValueError, match="reports"):
        hls.update(None, pin_path, github)

    assert hls.read_pin(pin_path) == pinned("1.0.3")


def test_cached_release_downloads_once_then_reuses_the_cache(tmp_path):
    github = FakeGitHub({"1.0.3": release("1.0.3")}, latest="1.0.3")
    pin = pinned("1.0.3")

    first = hls.cached_release(pin, tmp_path / "cache", github)
    downloads = len(github.requests)
    second = hls.cached_release(pin, tmp_path / "cache", github)

    assert first == second == tmp_path / "cache" / "1.0.3"
    assert len(github.requests) == downloads
    hls.verify_checksums(second)


def test_cached_release_replaces_a_damaged_cache(tmp_path):
    github = FakeGitHub({"1.0.3": release("1.0.3")}, latest="1.0.3")
    pin = pinned("1.0.3")
    cached = hls.cached_release(pin, tmp_path / "cache", github)
    (cached / hls.ARTIFACTS[0]).write_bytes(b"tampered")

    repaired = hls.cached_release(pin, tmp_path / "cache", github)

    assert (repaired / hls.ARTIFACTS[0]).read_bytes() == binary("1.0.3")


def test_verify_checksums_rejects_a_mismatched_artifact(tmp_path):
    github = FakeGitHub({"1.0.3": release("1.0.3")}, latest="1.0.3")
    hls.download("1.0.3", tmp_path, github)
    (tmp_path / hls.ARTIFACTS[1]).write_bytes(b"other")

    with pytest.raises(ValueError, match=f"checksum mismatch for {hls.ARTIFACTS[1]}"):
        hls.verify_checksums(tmp_path)


def test_cached_release_refuses_a_release_that_no_longer_matches_the_pin(tmp_path):
    github = FakeGitHub({"1.0.3": release("1.0.3")}, latest="1.0.3")
    swapped = {name: hashlib.sha256(b"earlier upload").hexdigest() for name in hls.ARTIFACTS}

    with pytest.raises(ValueError, match="does not match the sha256 pinned"):
        hls.cached_release(hls.Pin("1.0.3", 1, swapped), tmp_path / "cache", github)

    assert not (tmp_path / "cache" / "1.0.3").exists()


def test_update_records_the_sha256_of_every_artifact(pin_path):
    github = FakeGitHub({"1.0.4": release("1.0.4")}, latest="1.0.4")

    hls.update(None, pin_path, github)

    recorded = json.loads(pin_path.read_text())["sha256"]
    assert recorded == {name: hashlib.sha256(binary("1.0.4")).hexdigest() for name in hls.ARTIFACTS}
