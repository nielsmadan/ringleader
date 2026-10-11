from __future__ import annotations

import json

import pytest
from ringleader import presence
from ringleader.cli import main
from ringleader.commands import presence as presence_cmd

from test_presence import FakeRunner, installed_hooks, ok, stage_candidate


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A temp home with one detected agent and a staged, checksum-valid candidate."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    vendor = tmp_path / "_vendor"
    candidate = stage_candidate(vendor)
    monkeypatch.setenv("RINGLEADER_VENDOR_DIR", str(vendor))
    return tmp_path, candidate


def install_client(world, runner, executable=None):
    tmp_path, _ = world
    return presence.PresenceClient(
        executable=executable or tmp_path / "data" / "hooklinesinker" / "bin" / "hooklinesinker",
        runner=runner,
        system="Darwin",
        machine="arm64",
        vendor_dir=tmp_path / "_vendor",
    )


def bind(monkeypatch, client):
    monkeypatch.setattr(presence_cmd, "_make_client", lambda: client)


def test_install_registers_the_consumer_and_installs_hooks(world, monkeypatch, capsys):
    _, candidate = world
    runner = FakeRunner(
        results={
            f"{candidate}|version": ok({"protocol": 1, "version": "0.1.0"}),
            **installed_hooks("claude"),
        }
    )
    bind(monkeypatch, install_client(world, runner))

    assert main(["data", "presence", "install", "--yes"]) == 0

    out = capsys.readouterr().out
    assert "ringleader" in out
    assert [c[1:] for c in runner.calls if c[1] != "version"] == [
        ["install", "--consumer", "ringleader"],
        ["hooks", "install", "--agent", "claude"],
        ["hooks", "status", "--agent", "claude", "--json"],
    ]


def test_install_json_reports_the_outcome(world, monkeypatch, capsys):
    _, candidate = world
    runner = FakeRunner(
        results={
            f"{candidate}|version": ok({"protocol": 1, "version": "0.1.0"}),
            **installed_hooks("claude"),
        }
    )
    bind(monkeypatch, install_client(world, runner))

    assert main(["data", "presence", "install", "--yes", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["consumer"] == "ringleader"
    assert payload["agents"] == ["claude"]
    assert payload["hooks"] == [{"agent": "claude", "ok": True, "detail": "installed"}]


@pytest.mark.parametrize("as_json", [True, False])
@pytest.mark.parametrize(
    "status,diagnostic",
    [
        (
            ok({"protocol": 1, "agent": "claude", "state": "unsupported", "path": "settings.json"}),
            "unsupported at settings.json",
        ),
        (
            presence.RunResult(2, "", "cannot read settings.json"),
            "exited 2: cannot read settings.json",
        ),
        (presence.RunResult(0, "{invalid"), "could not parse"),
    ],
    ids=["unsupported", "failed-status", "malformed-status"],
)
def test_install_reports_hook_verification_failures(
    world, monkeypatch, capsys, as_json, status, diagnostic
):
    runner = FakeRunner(
        results={
            "version": ok({"protocol": 1, "version": "0.1.0"}),
            "hooks status": status,
        }
    )
    bind(monkeypatch, install_client(world, runner))
    args = ["data", "presence", "install", "--yes"]
    if as_json:
        args.append("--json")

    assert main(args) == 1

    out = capsys.readouterr().out
    if as_json:
        payload = json.loads(out)
        assert payload["ok"] is False
        assert payload["hooks"][0]["ok"] is False
        assert diagnostic in payload["hooks"][0]["detail"]
    else:
        assert "claude: failed" in out
        assert diagnostic in out


def test_install_json_refuses_to_run_unattended_without_yes(world, monkeypatch, capsys):
    runner = FakeRunner()
    bind(monkeypatch, install_client(world, runner))

    assert main(["data", "presence", "install", "--json"]) == 2

    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert runner.calls == []


def test_install_asks_before_changing_agent_configuration(world, monkeypatch, capsys):
    runner = FakeRunner()
    bind(monkeypatch, install_client(world, runner))
    monkeypatch.setattr("builtins.input", lambda _="": "n")

    assert main(["data", "presence", "install"]) == 0

    out = capsys.readouterr().out
    assert "aborted" in out
    assert "claude" in out
    assert runner.calls == []


def test_install_names_the_vendor_command_when_no_artifact_is_staged(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    empty = tmp_path / "empty"
    empty.mkdir()
    client = presence.PresenceClient(
        executable=tmp_path / "absent",
        runner=FakeRunner(),
        system="Darwin",
        machine="arm64",
        vendor_dir=empty,
    )
    bind(monkeypatch, client)

    assert main(["data", "presence", "install", "--yes"]) == 1

    assert presence.VENDOR_COMMAND in capsys.readouterr().err


def test_install_accepts_explicit_agents(world, monkeypatch, capsys):
    _, candidate = world
    runner = FakeRunner(
        results={
            f"{candidate}|version": ok({"protocol": 1, "version": "0.1.0"}),
            **installed_hooks("codex", "pi"),
        }
    )
    bind(monkeypatch, install_client(world, runner))

    assert main(["data", "presence", "install", "--yes", "--agent", "codex", "--agent", "pi"]) == 0

    agents = [c[-1] for c in runner.calls if c[1:3] == ["hooks", "install"]]
    assert agents == ["codex", "pi"]
    assert presence_cmd.CODEX_TRUST_HINT in capsys.readouterr().out


def test_install_without_codex_prints_no_codex_trust_hint(world, monkeypatch, capsys):
    _, candidate = world
    runner = FakeRunner(
        results={
            f"{candidate}|version": ok({"protocol": 1, "version": "0.1.0"}),
            **installed_hooks("pi"),
        }
    )
    bind(monkeypatch, install_client(world, runner))

    assert main(["data", "presence", "install", "--yes", "--agent", "pi"]) == 0

    out = capsys.readouterr().out
    assert "pi: ok" in out
    assert "codex" not in out


def test_install_accepts_the_new_agents(world, monkeypatch, capsys):
    _, candidate = world
    runner = FakeRunner(
        results={
            f"{candidate}|version": ok({"protocol": 1, "version": "0.1.0"}),
            **installed_hooks("droid", "qwen", "kimi"),
        }
    )
    bind(monkeypatch, install_client(world, runner))

    assert (
        main(
            [
                "data",
                "presence",
                "install",
                "--yes",
                "--agent",
                "droid",
                "--agent",
                "qwen",
                "--agent",
                "kimi",
            ]
        )
        == 0
    )

    agents = [c[-1] for c in runner.calls if c[1:3] == ["hooks", "install"]]
    assert agents == ["droid", "qwen", "kimi"]
    capsys.readouterr()


def test_status_reports_a_registered_consumer(world, monkeypatch, capsys):
    runner = FakeRunner(
        results={
            "version": ok({"protocol": 1, "version": "0.1.0"}),
            "consumers": ok({"protocol": 1, "consumers": [{"name": "ringleader"}], "problems": []}),
            "hooks status": ok({"protocol": 1, "agent": "claude", "state": "installed"}),
            "sessions": presence.RunResult(
                0, json.dumps({"protocol": 1, "sessions": [], "problems": []}), ""
            ),
        }
    )
    tmp_path, _ = world
    exe = tmp_path / "data" / "hooklinesinker" / "bin" / "hooklinesinker"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    bind(monkeypatch, install_client(world, runner, executable=exe))

    assert main(["data", "presence", "status"]) == 0

    out = capsys.readouterr().out
    assert "ringleader" in out
    assert "macos-universal" in out


def test_status_names_the_setup_command_for_drifted_hooks(world, monkeypatch, capsys):
    runner = FakeRunner(
        results={
            "version": ok({"protocol": 1, "version": "1.1.0"}),
            "consumers": ok({"protocol": 1, "consumers": [{"name": "ringleader"}], "problems": []}),
            "hooks status": ok({"protocol": 1, "agent": "claude", "state": "drifted"}),
            "sessions": ok({"protocol": 1, "sessions": [], "problems": []}),
        }
    )
    tmp_path, _ = world
    exe = tmp_path / "data" / "hooklinesinker" / "bin" / "hooklinesinker"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    bind(monkeypatch, install_client(world, runner, executable=exe))

    assert main(["data", "presence", "status"]) == 0

    out = capsys.readouterr().out
    assert "hooks claude: drifted" in out
    assert (
        f"drifted hooks predate the active hooklinesinker — run `{presence.SETUP_COMMAND}`" in out
    )


def test_status_is_nonzero_and_names_the_setup_command_when_absent(world, monkeypatch, capsys):
    runner = FakeRunner(raises=FileNotFoundError(2, "No such file or directory"))
    bind(monkeypatch, install_client(world, runner))

    assert main(["data", "presence", "status"]) == 1

    assert presence.SETUP_COMMAND in capsys.readouterr().out


def test_status_json_is_machine_readable(world, monkeypatch, capsys):
    runner = FakeRunner(raises=FileNotFoundError(2, "No such file or directory"))
    bind(monkeypatch, install_client(world, runner))

    assert main(["data", "presence", "status", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["installed"] is False
    assert payload["registered"] is False
    assert payload["artifact"] == "macos-universal"
    assert payload["setup_command"] == presence.SETUP_COMMAND


def test_uninstall_removes_only_the_ringleader_consumer(world, monkeypatch, capsys):
    tmp_path, _ = world
    exe = tmp_path / "data" / "hooklinesinker" / "bin" / "hooklinesinker"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    runner = FakeRunner(
        stdout="removed consumer ringleader",
        results={
            "consumers": ok(
                {
                    "protocol": 1,
                    "consumers": [{"name": "ringleader"}, {"name": "juggler"}],
                    "problems": [],
                }
            )
        },
    )
    bind(monkeypatch, install_client(world, runner, executable=exe))

    assert main(["data", "presence", "uninstall", "--yes"]) == 0

    assert runner.calls[-1] == [str(exe), "uninstall", "--consumer", "ringleader"]
    assert "ringleader" in capsys.readouterr().out


def test_uninstall_asks_first(world, monkeypatch, capsys):
    runner = FakeRunner()
    bind(monkeypatch, install_client(world, runner))
    monkeypatch.setattr("builtins.input", lambda _="": "")

    assert main(["data", "presence", "uninstall"]) == 0

    assert "aborted" in capsys.readouterr().out
    assert runner.calls == []
