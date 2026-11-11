import hashlib
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sandbox", REPO / "scripts" / "sandbox.py")
sandbox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sandbox)


def test_committed_descriptor_pins_local_fixture():
    descriptor = json.loads(sandbox.DESCRIPTOR.read_text(encoding="utf-8"))

    assert descriptor["fixture_version"] == "init-v1"
    assert sum(descriptor["session_counts"].values()) == 100
    assert re.fullmatch(r"[0-9a-f]{64}", descriptor["manifest_sha256"])
    assert not {"repository", "release_tag", "asset_name", "encrypted_sha256"} & descriptor.keys()
    assert not (REPO / "tests" / "fixtures" / "sandbox").exists()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "fixture"
    claude = root / "claude" / "-Users-dev-wrksp-oss-project" / "one.jsonl"
    codex = root / "codex" / "2026" / "01" / "01" / "rollout-two.jsonl"
    index = root / "session_index.jsonl"
    claude.parent.mkdir(parents=True)
    codex.parent.mkdir(parents=True)
    claude.write_bytes(b'{"type":"user","message":{"content":"exact claude"}}\n')
    codex.write_bytes(b'{"type":"session_meta","payload":{"id":"two"}}\n')
    index.write_bytes(b'{"id":"two","thread_name":"real title"}\n')
    files = [
        {
            "path": claude.relative_to(root).as_posix(),
            "kind": "session",
            "source": "claude-code",
            "sha256": _sha256(claude),
            "age_days": 2,
            "bucket": "tiny",
            "message_count": 4,
        },
        {
            "path": codex.relative_to(root).as_posix(),
            "kind": "session",
            "source": "codex",
            "sha256": _sha256(codex),
            "age_days": 14,
            "bucket": "medium",
            "message_count": 14,
        },
        {
            "path": index.relative_to(root).as_posix(),
            "kind": "codex-index",
            "sha256": _sha256(index),
        },
    ]
    manifest = {"schema_version": 1, "fixture_version": "test-v1", "files": files}
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    descriptor = {
        "schema_version": 1,
        "fixture_version": "test-v1",
        "manifest_sha256": _sha256(manifest_path),
        "session_counts": {"claude-code": 1, "codex": 1},
        "raw_bytes": sum(path.stat().st_size for path in (claude, codex, index)),
    }
    return root, descriptor


def test_build_restores_exact_fixture_state(tmp_path, capsys):
    fixture, descriptor = _fixture(tmp_path)
    target = tmp_path / ".sandbox"
    (target / "old-data").mkdir(parents=True)

    fingerprint = sandbox.build(
        fixture,
        target,
        descriptor,
        now=2_000_000_000,
        retention=7,
    )

    assert (target / "claude" / "-Users-dev-wrksp-oss-project" / "one.jsonl").read_bytes() == (
        fixture / "claude" / "-Users-dev-wrksp-oss-project" / "one.jsonl"
    ).read_bytes()
    assert (target / "codex" / "2026" / "01" / "01" / "rollout-two.jsonl").read_bytes() == (
        fixture / "codex" / "2026" / "01" / "01" / "rollout-two.jsonl"
    ).read_bytes()
    assert (target / "session_index.jsonl").read_bytes() == (
        fixture / "session_index.jsonl"
    ).read_bytes()
    assert json.loads((target / "claude-settings.json").read_text()) == {"cleanupPeriodDays": 7}
    assert (target / "droid" / ".factory" / "sessions").is_dir()
    assert list((target / "data").iterdir()) == []
    assert not (target / "old-data").exists()
    assert (
        target / "claude" / "-Users-dev-wrksp-oss-project" / "one.jsonl"
    ).stat().st_mtime == pytest.approx(2_000_000_000 - 2 * sandbox.DAY)
    assert (
        target / "codex" / "2026" / "01" / "01" / "rollout-two.jsonl"
    ).stat().st_mtime == pytest.approx(2_000_000_000 - 14 * sandbox.DAY)
    assert fingerprint in capsys.readouterr().out


def test_ensure_sandbox_preserves_existing_test_progress(tmp_path):
    fixture, descriptor = _fixture(tmp_path)
    target = tmp_path / ".sandbox"
    sandbox.build(fixture, target, descriptor, now=2_000_000_000)
    progress = target / "data" / "ringleader" / "registry.json"
    progress.parent.mkdir(parents=True)
    progress.write_text('{"triaged": true}', encoding="utf-8")

    sandbox.ensure_sandbox(fixture, target, descriptor)

    assert progress.read_text(encoding="utf-8") == '{"triaged": true}'


def test_ensure_sandbox_rebuilds_incomplete_state(tmp_path):
    fixture, descriptor = _fixture(tmp_path)
    target = tmp_path / ".sandbox"
    target.mkdir()

    sandbox.ensure_sandbox(fixture, target, descriptor)

    assert (
        target / "claude" / "-Users-dev-wrksp-oss-project" / "one.jsonl"
    ).read_bytes() == b'{"type":"user","message":{"content":"exact claude"}}\n'
    assert json.loads((target / "claude-settings.json").read_text()) == {"cleanupPeriodDays": 7}


def test_shell_environment_isolates_every_ringleader_input(tmp_path):
    target = tmp_path / ".sandbox"
    base = {
        "HOME": "/Users/tester",
        "PATH": "/usr/bin",
        "SHELL": "/bin/zsh",
        "XDG_DATA_HOME": "/real/data",
        "SESSION_TRACKER_PROJECTS_DIR": "/real/claude",
        "CODEX_SESSIONS_DIR": "/real/codex",
        "PI_SESSIONS_DIR": "/real/pi",
        "FACTORY_HOME_OVERRIDE": "/real/factory",
        "CURSOR_CONFIG_DIR": "/real/cursor",
        "CURSOR_DATA_DIR": "/real/cursor",
        "RINGLEADER_CLAUDE_SETTINGS": "/real/settings.json",
    }

    environment = sandbox.shell_environment(target, base)

    assert environment == {
        "HOME": "/Users/tester",
        "PATH": f"{REPO / '.venv' / 'bin'}:/usr/bin",
        "SHELL": "/bin/zsh",
        "XDG_DATA_HOME": str(target / "data"),
        "SESSION_TRACKER_PROJECTS_DIR": str(target / "claude"),
        "CODEX_SESSIONS_DIR": str(target / "codex"),
        "PI_SESSIONS_DIR": str(target / "pi"),
        "FACTORY_HOME_OVERRIDE": str(target / "droid"),
        "CURSOR_CONFIG_DIR": str(target / "cursor"),
        "CURSOR_DATA_DIR": str(target / "cursor"),
        "RINGLEADER_CLAUDE_SETTINGS": str(target / "claude-settings.json"),
    }


def test_enter_shell_starts_the_user_shell_with_sandbox_environment(tmp_path):
    target = tmp_path / ".sandbox"
    invocation = {}

    def exec_process(executable, arguments, environment):
        invocation.update(
            executable=executable,
            arguments=arguments,
            environment=environment,
        )

    sandbox.enter_shell(
        target,
        base={"PATH": "/usr/bin", "SHELL": "/bin/zsh"},
        exec_process=exec_process,
    )

    assert invocation["executable"] == "/bin/zsh"
    assert invocation["arguments"] == ["/bin/zsh", "-i"]
    assert invocation["environment"]["PATH"] == f"{REPO / '.venv' / 'bin'}:/usr/bin"
    assert invocation["environment"]["XDG_DATA_HOME"] == str(target / "data")
    assert invocation["environment"]["SESSION_TRACKER_PROJECTS_DIR"] == str(target / "claude")
    assert invocation["environment"]["CODEX_SESSIONS_DIR"] == str(target / "codex")
    assert invocation["environment"]["FACTORY_HOME_OVERRIDE"] == str(target / "droid")
    assert invocation["environment"]["RINGLEADER_CLAUDE_SETTINGS"] == str(
        target / "claude-settings.json"
    )


def test_build_rejects_fixture_inside_sandbox(tmp_path):
    target = tmp_path / ".sandbox"
    fixture, descriptor = _fixture(target)

    with pytest.raises(sandbox.FixtureError, match="fixture cannot live inside"):
        sandbox.build(fixture, target, descriptor)


def test_fixture_rejects_unmanifested_files(tmp_path):
    fixture, descriptor = _fixture(tmp_path)
    extra = fixture / "claude" / "-Users-dev-wrksp-oss-project" / "extra.jsonl"
    extra.write_text('{"type":"user"}\n', encoding="utf-8")

    with pytest.raises(sandbox.FixtureError, match="unexpected file"):
        sandbox.validate_fixture(fixture, descriptor)


def test_fixture_rejects_paths_outside_root(tmp_path):
    fixture, descriptor = _fixture(tmp_path)
    manifest_path = fixture / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][0]["path"] = "../outside.jsonl"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    descriptor["manifest_sha256"] = _sha256(manifest_path)

    with pytest.raises(sandbox.FixtureError, match="unsafe fixture path"):
        sandbox.validate_fixture(fixture, descriptor)


def test_fixture_rejects_manifest_fingerprint_mismatch(tmp_path):
    fixture, descriptor = _fixture(tmp_path)
    descriptor["manifest_sha256"] = "0" * 64

    with pytest.raises(sandbox.FixtureError, match="manifest fingerprint"):
        sandbox.validate_fixture(fixture, descriptor)


def test_fixture_rejects_wrong_message_bucket(tmp_path):
    fixture, descriptor = _fixture(tmp_path)
    manifest_path = fixture / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][0]["message_count"] = 12
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    descriptor["manifest_sha256"] = _sha256(manifest_path)

    with pytest.raises(sandbox.FixtureError, match="message bucket"):
        sandbox.validate_fixture(fixture, descriptor)


def test_build_command_ignores_sandbox_xdg_data_home(tmp_path, monkeypatch):
    _fixture_root, descriptor = _fixture(tmp_path)
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    seen = {}

    def build(fixture_root, sandbox_root, loaded, *, retention):
        seen.update(
            fixture_root=fixture_root,
            sandbox_root=sandbox_root,
            descriptor=loaded,
            retention=retention,
        )

    monkeypatch.setattr(sandbox, "DESCRIPTOR", descriptor_path)
    monkeypatch.setattr(sandbox, "build", build)
    monkeypatch.delenv("RINGLEADER_SANDBOX_FIXTURE_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".sandbox" / "data"))

    assert sandbox.main([]) == 0
    assert seen["fixture_root"] == (
        Path.home() / ".local" / "share" / "ringleader" / "fixtures" / "test-v1"
    )


def test_shell_command_reuses_ready_sandbox_and_enters_it(tmp_path, monkeypatch):
    fixture, descriptor = _fixture(tmp_path)
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    target = tmp_path / ".sandbox"
    sandbox.build(fixture, target, descriptor)
    progress = target / "data" / "progress"
    progress.write_text("kept", encoding="utf-8")
    invocation = {}

    def exec_process(executable, arguments, environment):
        invocation.update(
            executable=executable,
            arguments=arguments,
            environment=environment,
        )

    monkeypatch.setattr(sandbox, "DESCRIPTOR", descriptor_path)
    monkeypatch.setattr(sandbox, "SANDBOX", target)
    monkeypatch.setenv("RINGLEADER_SANDBOX_FIXTURE_DIR", str(fixture))
    monkeypatch.setenv("SHELL", "/bin/zsh")

    assert sandbox.main(["--shell"], exec_process=exec_process) == 0
    assert progress.read_text(encoding="utf-8") == "kept"
    assert invocation["arguments"] == ["/bin/zsh", "-i"]
    assert invocation["environment"]["XDG_DATA_HOME"] == str(target / "data")


def test_sandbox_recipe_wires_skills_then_enters_shell():
    result = subprocess.run(
        ["just", "--dry-run", "sandbox"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stderr.splitlines() == [
        "uv run rl install skills --yes",
        "uv run python scripts/sandbox.py --shell",
    ]


def test_sandbox_reset_recipe_rebuilds_without_entering_shell():
    result = subprocess.run(
        ["just", "--dry-run", "sandbox-reset"],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr.strip() == "uv run python scripts/sandbox.py"
