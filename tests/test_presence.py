from __future__ import annotations

import hashlib
import json
import subprocess

import pytest
from ringleader import presence
from ringleader.presence import PresenceClient, PresenceUnavailable


def _key(cmd: list[str]) -> str:
    out = []
    for arg in cmd[1:]:
        if arg.startswith("-"):
            break
        out.append(arg)
    return " ".join(out)


class FakeRunner:
    """Stands in for the subprocess seam: records argv, replays canned results.

    ``results`` is keyed by the leading non-flag arguments ("sessions",
    "hooks install"), optionally prefixed with ``<executable>|`` when a test needs
    the active binary and the bundled candidate to answer differently."""

    def __init__(self, stdout="", stderr="", returncode=0, *, results=None, raises=None):
        self.default = presence.RunResult(returncode, stdout, stderr)
        self.results = dict(results or {})
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if self.raises is not None:
            raise self.raises
        for candidate in (f"{cmd[0]}|{_key(cmd)}", " ".join(cmd[1:]), _key(cmd)):
            if candidate in self.results:
                outcome = self.results[candidate]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        return self.default

    def commands(self) -> list[str]:
        return [_key(c) for c in self.calls]


def ok(payload) -> presence.RunResult:
    return presence.RunResult(0, json.dumps(payload), "")


def installed_hooks(*agents):
    return {
        f"hooks status --agent {agent} --json": ok(
            {"protocol": 1, "agent": agent, "state": "installed"}
        )
        for agent in agents
    }


def envelope(sessions, problems=()) -> str:
    return json.dumps({"protocol": 1, "sessions": list(sessions), "problems": list(problems)})


def session(agent="claude", sid="s1", running=True, **extra) -> dict:
    return {
        "protocol": 1,
        "agent": agent,
        "running": running,
        "session": {"id": sid, "cwd": "/tmp"},
        **extra,
    }


def test_running_ids_are_agent_qualified(tmp_path):
    runner = FakeRunner(
        stdout="""{
      "protocol": 1,
      "sessions": [
        {"agent":"claude","running":true,"session":{"id":"same"}},
        {"agent":"codex","running":true,"session":{"id":"same"}}
      ],
      "problems": []
    }"""
    )
    client = PresenceClient(executable=tmp_path / "hooklinesinker", runner=runner)
    assert client.running_session_ids() == frozenset({("claude", "same"), ("codex", "same")})


def test_only_running_sessions_become_ids(tmp_path):
    runner = FakeRunner(stdout=envelope([session(sid="live"), session(sid="dead", running=False)]))
    client = PresenceClient(executable=tmp_path / "hooklinesinker", runner=runner)
    assert client.running_session_ids() == frozenset({("claude", "live")})


def test_sessions_returns_problems_alongside_sessions(tmp_path):
    runner = FakeRunner(
        stdout=envelope(
            [session()],
            [{"observedAt": "2026-09-04T00:00:00Z", "message": "sink refused the event"}],
        )
    )
    result = PresenceClient(executable=tmp_path / "hls", runner=runner).sessions()
    assert result.available is True
    assert [s.session_id for s in result.sessions] == ["s1"]
    assert result.problems == ("sink refused the event",)


def test_an_absent_binary_is_not_an_empty_success(tmp_path):
    runner = FakeRunner(raises=FileNotFoundError(2, "No such file or directory"))
    result = PresenceClient(executable=tmp_path / "hls", runner=runner).sessions()
    assert result.available is False
    assert result.sessions == ()
    assert presence.SETUP_COMMAND in result.problems[0]


def test_a_timeout_is_not_an_empty_success(tmp_path):
    runner = FakeRunner(raises=subprocess.TimeoutExpired(cmd="hls", timeout=2))
    result = PresenceClient(executable=tmp_path / "hls", runner=runner).sessions()
    assert result.available is False
    assert "timed out" in result.problems[0]


def test_a_nonzero_exit_is_not_an_empty_success(tmp_path):
    runner = FakeRunner(returncode=3, stderr="state store is locked")
    result = PresenceClient(executable=tmp_path / "hls", runner=runner).sessions()
    assert result.available is False
    assert "state store is locked" in result.problems[0]


def test_malformed_json_is_reported(tmp_path):
    runner = FakeRunner(stdout="{not json")
    result = PresenceClient(executable=tmp_path / "hls", runner=runner).sessions()
    assert result.available is False
    assert "could not parse" in result.problems[0]


def test_an_incompatible_envelope_protocol_is_refused(tmp_path):
    runner = FakeRunner(stdout=json.dumps({"protocol": 2, "sessions": [session()], "problems": []}))
    result = PresenceClient(executable=tmp_path / "hls", runner=runner).sessions()
    assert result.available is False
    assert result.sessions == ()
    assert "protocol 2" in result.problems[0]


def test_unknown_records_are_skipped_and_reported(tmp_path):
    runner = FakeRunner(
        stdout=envelope(
            [
                session(sid="good"),
                {"protocol": 2, "agent": "claude", "running": True, "session": {"id": "future"}},
                {"agent": "claude", "running": True},
            ]
        )
    )
    result = PresenceClient(executable=tmp_path / "hls", runner=runner).sessions()
    assert [s.session_id for s in result.sessions] == ["good"]
    assert len(result.problems) == 2
    assert result.available is True


def test_require_running_ids_fails_with_the_exact_setup_command(tmp_path):
    runner = FakeRunner(raises=FileNotFoundError(2, "No such file or directory"))
    client = PresenceClient(executable=tmp_path / "hls", runner=runner)
    with pytest.raises(PresenceUnavailable) as excinfo:
        client.require_running_session_ids()
    assert presence.SETUP_COMMAND in str(excinfo.value)
    assert runner.commands() == ["sessions"]


def test_require_running_ids_returns_the_ids_when_available(tmp_path):
    runner = FakeRunner(stdout=envelope([session(sid="live")]))
    client = PresenceClient(executable=tmp_path / "hls", runner=runner)
    assert client.require_running_session_ids() == frozenset({("claude", "live")})


def test_every_read_goes_through_the_cli_not_the_state_files(tmp_path):
    """The privacy boundary: ringleader asks the binary, it never opens the ledger."""
    exe = tmp_path / "hls"
    runner = FakeRunner(stdout=envelope([session()]))
    client = PresenceClient(executable=exe, runner=runner)
    client.sessions()
    client.doctor()
    client.consumers()
    assert {c[0] for c in runner.calls} == {str(exe)}
    assert runner.commands() == ["sessions", "doctor", "consumers"]


def test_doctor_keeps_its_checks_when_the_exit_code_is_nonzero(tmp_path):
    payload = {
        "protocol": 1,
        "version": "0.1.0",
        "checks": [
            {"name": "version", "ok": True, "detail": "hooklinesinker 0.1.0 (protocol 1)"},
            {"name": "hook_status", "ok": False, "detail": "Codex hooks are Drifted"},
        ],
        "exitCode": 1,
    }
    runner = FakeRunner(returncode=1, stdout=json.dumps(payload))
    report = PresenceClient(executable=tmp_path / "hls", runner=runner).doctor()
    assert report.installed is True
    assert report.available is True
    assert report.exit_code == 1
    assert report.problems == ("hook_status: Codex hooks are Drifted",)
    assert len(report.checks) == 2


def test_doctor_unreadable_output_is_a_problem_not_a_pass(tmp_path):
    runner = FakeRunner(returncode=1, stdout="", stderr="illegal instruction")
    report = PresenceClient(executable=tmp_path / "hls", runner=runner).doctor()
    assert report.available is False
    assert report.checks == ()
    assert "illegal instruction" in report.problems[0]


def test_doctor_reports_an_absent_binary_as_not_installed(tmp_path):
    runner = FakeRunner(raises=FileNotFoundError(2, "No such file or directory"))
    report = PresenceClient(executable=tmp_path / "hls", runner=runner).doctor()
    assert report.installed is False
    assert report.available is False


def test_consumers_lists_registered_names(tmp_path):
    payload = {
        "protocol": 1,
        "consumers": [
            {"name": "juggler", "protocol": 1, "capabilities": ["status"], "sink": "http://x"},
            {"name": "ringleader", "protocol": 1, "capabilities": ["status"], "sink": None},
        ],
        "problems": [],
    }
    result = PresenceClient(
        executable=tmp_path / "hls", runner=FakeRunner(stdout=json.dumps(payload))
    )
    consumers = result.consumers()
    assert consumers.names == ("juggler", "ringleader")
    assert consumers.available is True


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Darwin", "arm64", "macos-universal"),
        ("Darwin", "x86_64", "macos-universal"),
        ("Linux", "aarch64", "linux-aarch64"),
        ("Linux", "x86_64", "linux-x86_64"),
    ],
)
def test_artifact_dir_maps_the_supported_pairs(system, machine, expected):
    assert presence.artifact_dir(system, machine) == expected


@pytest.mark.parametrize(
    "system,machine",
    [("Windows", "AMD64"), ("Linux", "armv7l"), ("Darwin", "ppc")],
)
def test_artifact_dir_refuses_unsupported_pairs(system, machine):
    with pytest.raises(PresenceUnavailable) as excinfo:
        presence.artifact_dir(system, machine)
    assert machine in str(excinfo.value)


def stage_candidate(root, artifact="macos-universal", body=b"#!/bin/sh\nexit 0\n", digest=None):
    binary = root / artifact / "hooklinesinker"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(body)
    binary.chmod(0o755)
    checksum = digest or hashlib.sha256(body).hexdigest()
    (root / presence.CHECKSUM_MANIFEST).write_text(f"{checksum}  {artifact}/hooklinesinker\n")
    return binary


def client_for(tmp_path, vendor, runner, executable=None):
    return PresenceClient(
        executable=executable or tmp_path / "active" / "hooklinesinker",
        runner=runner,
        system="Darwin",
        machine="arm64",
        vendor_dir=vendor,
    )


def test_candidate_verifies_against_the_manifest(tmp_path):
    vendor = tmp_path / "_vendor"
    binary = stage_candidate(vendor)
    client = client_for(tmp_path, vendor, FakeRunner())
    assert client.candidate_path() == binary


def test_candidate_checksum_mismatch_is_refused(tmp_path):
    vendor = tmp_path / "_vendor"
    stage_candidate(vendor, digest="0" * 64)
    client = client_for(tmp_path, vendor, FakeRunner())
    with pytest.raises(PresenceUnavailable) as excinfo:
        client.candidate_path()
    assert "checksum mismatch" in str(excinfo.value)


def test_a_missing_candidate_names_the_vendor_command(tmp_path):
    vendor = tmp_path / "_vendor"
    vendor.mkdir()
    client = client_for(tmp_path, vendor, FakeRunner())
    with pytest.raises(PresenceUnavailable) as excinfo:
        client.candidate_path()
    assert presence.VENDOR_COMMAND in str(excinfo.value)


def active_binary(tmp_path, version="0.1.0"):
    exe = tmp_path / "active" / "hooklinesinker"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def test_install_reuses_a_compatible_active_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vendor = tmp_path / "_vendor"
    candidate = stage_candidate(vendor)
    exe = active_binary(tmp_path)
    runner = FakeRunner(
        results={
            f"{exe}|version": ok({"protocol": 1, "version": "0.9.0"}),
            f"{candidate}|version": ok({"protocol": 1, "version": "0.1.0"}),
            f"{exe}|hooks status": ok({"protocol": 1, "agent": "claude", "state": "installed"}),
        }
    )
    outcome = client_for(tmp_path, vendor, runner, executable=exe).install_consumer(["claude"])
    assert outcome.ok is True
    assert outcome.reused_active is True
    assert runner.calls == [
        [str(exe), "version", "--json"],
        [str(candidate), "version", "--json"],
        [str(exe), "install", "--consumer", "ringleader"],
        [str(exe), "hooks", "install", "--agent", "claude"],
        [str(exe), "hooks", "status", "--agent", "claude", "--json"],
    ]


def test_install_activates_the_bundled_candidate_when_nothing_is_active(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vendor = tmp_path / "_vendor"
    candidate = stage_candidate(vendor)
    runner = FakeRunner(
        results={
            f"{candidate}|version": ok({"protocol": 1, "version": "0.1.0"}),
            **installed_hooks("claude"),
        }
    )
    outcome = client_for(tmp_path, vendor, runner).install_consumer(["claude"])
    assert outcome.ok is True
    assert outcome.reused_active is False
    assert outcome.binary == str(candidate)
    assert runner.calls == [
        [str(candidate), "version", "--json"],
        [str(candidate), "install", "--consumer", "ringleader"],
        [str(candidate), "hooks", "install", "--agent", "claude"],
        [str(candidate), "hooks", "status", "--agent", "claude", "--json"],
    ]


def test_install_upgrades_to_a_newer_compatible_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vendor = tmp_path / "_vendor"
    candidate = stage_candidate(vendor)
    exe = active_binary(tmp_path)
    runner = FakeRunner(
        results={
            f"{exe}|version": ok({"protocol": 1, "version": "0.1.0"}),
            f"{candidate}|version": ok({"protocol": 1, "version": "0.2.0"}),
            f"{candidate}|hooks status": ok(
                {"protocol": 1, "agent": "claude", "state": "installed"}
            ),
        }
    )
    outcome = client_for(tmp_path, vendor, runner, executable=exe).install_consumer(["claude"])
    assert outcome.ok is True
    assert outcome.reused_active is False
    assert outcome.binary == str(candidate)


def test_install_refuses_an_incompatible_active_protocol(tmp_path):
    vendor = tmp_path / "_vendor"
    stage_candidate(vendor)
    exe = active_binary(tmp_path)
    runner = FakeRunner(results={f"{exe}|version": ok({"protocol": 2, "version": "9.0.0"})})
    with pytest.raises(PresenceUnavailable) as excinfo:
        client_for(tmp_path, vendor, runner, executable=exe).install_consumer(["claude"])
    assert "protocol 2" in str(excinfo.value)
    assert "install" not in runner.commands()


def test_install_verifies_the_checksum_before_invoking_anything(tmp_path):
    vendor = tmp_path / "_vendor"
    stage_candidate(vendor, digest="0" * 64)
    runner = FakeRunner()
    with pytest.raises(PresenceUnavailable) as excinfo:
        client_for(tmp_path, vendor, runner).install_consumer(["claude"])
    assert "checksum mismatch" in str(excinfo.value)
    assert runner.calls == []


def test_install_registers_the_consumer_with_no_sink(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vendor = tmp_path / "_vendor"
    candidate = stage_candidate(vendor)
    runner = FakeRunner(
        results={
            f"{candidate}|version": ok({"protocol": 1, "version": "0.1.0"}),
            **installed_hooks("claude"),
        }
    )
    client_for(tmp_path, vendor, runner).install_consumer(["claude"])
    install_call = next(c for c in runner.calls if c[1] == "install")
    assert "--sink" not in install_call


def test_install_hooks_for_explicit_agents_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    vendor = tmp_path / "_vendor"
    candidate = stage_candidate(vendor)
    runner = FakeRunner(
        results={
            f"{candidate}|version": ok({"protocol": 1, "version": "0.1.0"}),
            **installed_hooks("pi"),
        }
    )
    outcome = client_for(tmp_path, vendor, runner).install_consumer(["pi"])
    assert outcome.ok is True
    assert [h.agent for h in outcome.hooks] == ["pi"]


def test_install_hooks_for_detected_agents_when_none_are_named(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".config" / "opencode").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    vendor = tmp_path / "_vendor"
    candidate = stage_candidate(vendor)
    runner = FakeRunner(
        results={
            f"{candidate}|version": ok({"protocol": 1, "version": "0.1.0"}),
            **installed_hooks("claude", "opencode"),
        }
    )
    outcome = client_for(tmp_path, vendor, runner).install_consumer()
    assert outcome.ok is True
    assert [h.agent for h in outcome.hooks] == ["claude", "opencode"]


def test_install_reports_a_failing_hook_install(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vendor = tmp_path / "_vendor"
    candidate = stage_candidate(vendor)
    runner = FakeRunner(
        results={
            f"{candidate}|version": ok({"protocol": 1, "version": "0.1.0"}),
            "hooks install": presence.RunResult(1, "", "settings.json is not writable"),
        }
    )
    outcome = client_for(tmp_path, vendor, runner).install_consumer(["claude"])
    assert outcome.ok is False
    assert outcome.hooks[0].detail == "settings.json is not writable"
    assert runner.commands() == ["version", "install", "hooks install"]


@pytest.mark.parametrize("state", ["unsupported", "missing", "drifted"])
def test_install_requires_installed_hooks_after_a_successful_process(tmp_path, state):
    vendor = tmp_path / "_vendor"
    stage_candidate(vendor)
    config = tmp_path / "home" / ".claude" / "settings.json"
    runner = FakeRunner(
        results={
            "version": ok({"protocol": 1, "version": "0.1.0"}),
            "hooks install": presence.RunResult(0, f"[Claude] {state}: {config}"),
            "hooks status": ok(
                {"protocol": 1, "agent": "claude", "state": state, "path": str(config)}
            ),
        }
    )

    outcome = client_for(tmp_path, vendor, runner).install_consumer(["claude"])

    assert outcome.ok is False
    assert outcome.hooks == (presence.HookOutcome("claude", False, f"{state} at {config}"),)
    assert runner.commands() == ["version", "install", "hooks install", "hooks status"]


@pytest.fixture(
    params=[
        (
            presence.RunResult(
                3,
                json.dumps({"protocol": 1, "agent": "claude", "state": "installed"}),
                "settings.json could not be read",
            ),
            "exited 3: settings.json could not be read",
        ),
        (presence.RunResult(0, "{not json"), "could not parse"),
        (ok([]), "could not parse"),
        (ok({"protocol": 2, "agent": "claude", "state": "installed"}), "protocol 2"),
        (ok({"protocol": 1, "agent": "codex", "state": "installed"}), "expected 'claude'"),
        (ok({"protocol": 1, "state": "installed"}), "reported agent None"),
        (ok({"protocol": 1, "agent": "claude"}), "unrecognized state None"),
        (ok({"protocol": 1, "agent": "claude", "state": "future"}), "unrecognized state 'future'"),
        (subprocess.TimeoutExpired(cmd="hls", timeout=2), "timed out"),
        (FileNotFoundError(2, "No such file or directory"), presence.SETUP_COMMAND),
    ],
    ids=[
        "nonzero-installed",
        "malformed-json",
        "json-array",
        "wrong-protocol",
        "wrong-agent",
        "missing-agent",
        "missing-state",
        "unknown-state",
        "timeout",
        "absent-binary",
    ],
)
def invalid_hook_status(request):
    return request.param


def test_install_reports_failed_hook_verification(tmp_path, invalid_hook_status):
    status, diagnostic = invalid_hook_status
    vendor = tmp_path / "_vendor"
    stage_candidate(vendor)
    runner = FakeRunner(
        results={
            "version": ok({"protocol": 1, "version": "0.1.0"}),
            "hooks status": status,
        }
    )

    outcome = client_for(tmp_path, vendor, runner).install_consumer(["claude"])

    assert outcome.ok is False
    assert outcome.hooks[0].ok is False
    assert diagnostic in outcome.hooks[0].detail


def test_hook_state_reports_unknown_when_verification_fails(tmp_path, invalid_hook_status):
    status, _ = invalid_hook_status
    runner = FakeRunner(results={"hooks status": status})
    client = PresenceClient(executable=tmp_path / "hls", runner=runner)

    assert client.hook_state("claude") == "unknown"


@pytest.mark.parametrize("state", ["installed", "unsupported", "missing", "drifted"])
def test_hook_state_reads_verified_states(tmp_path, state):
    runner = FakeRunner(
        results={"hooks status": ok({"protocol": 1, "agent": "claude", "state": state})}
    )
    client = PresenceClient(executable=tmp_path / "hls", runner=runner)

    assert client.hook_state("claude") == state


def test_uninstall_removes_only_the_ringleader_consumer(tmp_path):
    exe = active_binary(tmp_path)
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
    outcome = PresenceClient(executable=exe, runner=runner).uninstall_consumer()
    assert outcome.ok is True
    assert runner.calls == [
        [str(exe), "consumers", "--json"],
        [str(exe), "uninstall", "--consumer", "ringleader"],
    ]


def test_uninstall_when_ringleader_is_not_registered_succeeds_without_running_it(tmp_path):
    exe = active_binary(tmp_path)
    runner = FakeRunner(
        results={
            "consumers": ok({"protocol": 1, "consumers": [{"name": "juggler"}], "problems": []}),
            "uninstall": presence.RunResult(1, "", "consumer ringleader is not registered"),
        }
    )
    outcome = PresenceClient(executable=exe, runner=runner).uninstall_consumer()
    assert (outcome.ok, outcome.ran) == (True, False)
    assert outcome.detail == "ringleader is not registered with hooklinesinker"
    assert runner.commands() == ["consumers"]


def test_uninstall_keeps_the_warning_hooklinesinker_prints_on_success(tmp_path):
    exe = active_binary(tmp_path)
    runner = FakeRunner(
        results={
            "consumers": ok(
                {
                    "protocol": 1,
                    "consumers": [{"name": "ringleader"}, {"name": "juggler"}],
                    "problems": [],
                }
            ),
            "uninstall": presence.RunResult(
                0, "removed consumer ringleader\n", "hooks remain for codex: config changed\n"
            ),
        }
    )
    outcome = PresenceClient(executable=exe, runner=runner).uninstall_consumer()
    assert outcome.ok is True
    assert outcome.detail == "removed consumer ringleader\nhooks remain for codex: config changed"


def test_uninstall_without_an_installed_binary_is_a_no_op(tmp_path):
    exe = tmp_path / "absent" / "hooklinesinker"
    runner = FakeRunner()
    outcome = PresenceClient(executable=exe, runner=runner).uninstall_consumer()
    assert outcome.ok is True
    assert runner.calls == []


def test_detected_agents_reads_the_agent_homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for probe in (".claude", ".codex", ".config/opencode", ".pi/agent"):
        (home / probe).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert presence.detected_agents() == ("claude", "codex", "opencode", "pi")


@pytest.mark.parametrize(
    "agent,env_var,relative",
    [
        ("opencode", "OPENCODE_CONFIG_DIR", "relocated"),
        ("opencode", "XDG_CONFIG_HOME", "relocated/opencode"),
        ("pi", "PI_CODING_AGENT_DIR", "relocated"),
    ],
)
def test_detected_agents_reads_relocated_config(tmp_path, monkeypatch, agent, env_var, relative):
    (tmp_path / relative).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(env_var, str(tmp_path / "relocated"))

    assert presence.detected_agents() == (agent,)


@pytest.mark.parametrize(
    "env_var,default_relative",
    [
        ("OPENCODE_CONFIG_DIR", ".config/opencode"),
        ("XDG_CONFIG_HOME", ".config/opencode"),
        ("PI_CODING_AGENT_DIR", ".pi/agent"),
    ],
)
def test_detected_agents_uses_only_the_configured_root(
    tmp_path, monkeypatch, env_var, default_relative
):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / default_relative).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(env_var, str(tmp_path / "absent"))

    assert presence.detected_agents() == ("claude",)


@pytest.mark.parametrize("opencode_exists", [True, False])
def test_detected_agents_prioritizes_opencode_config_over_xdg(
    tmp_path, monkeypatch, opencode_exists
):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (tmp_path / "xdg" / "opencode").mkdir(parents=True)
    opencode = tmp_path / "opencode"
    if opencode_exists:
        opencode.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(opencode))

    expected = ("claude", "opencode") if opencode_exists else ("claude",)
    assert presence.detected_agents() == expected


def test_detected_agents_falls_back_to_xdg_for_empty_opencode_override(tmp_path, monkeypatch):
    (tmp_path / "xdg" / "opencode").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "")

    assert presence.detected_agents() == ("opencode",)


def test_detected_agents_falls_back_home_for_empty_config_overrides(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".pi" / "agent").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for name in ("OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME", "PI_CODING_AGENT_DIR"):
        monkeypatch.setenv(name, "")

    assert presence.detected_agents() == ("opencode", "pi")


def test_detected_agents_requires_pis_agent_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".pi").mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert presence.detected_agents() == ("claude",)


def test_detected_agents_reads_droids_factory_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".factory").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FACTORY_HOME_OVERRIDE", raising=False)
    assert presence.detected_agents() == ("droid",)


def test_detected_agents_honors_the_factory_home_override(tmp_path, monkeypatch):
    """FACTORY_HOME_OVERRIDE stands in for HOME, so droid's config still sits under a
    `.factory` directory beneath it — the same reading `paths.factory_dir()` takes."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    relocated = tmp_path / "elsewhere"
    (relocated / ".factory").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FACTORY_HOME_OVERRIDE", str(relocated))
    assert presence.detected_agents() == ("droid",)


def test_detected_agents_treats_an_empty_factory_home_override_as_unset(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".factory").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FACTORY_HOME_OVERRIDE", "")
    assert presence.detected_agents() == ("droid",)


def test_detected_agents_reads_qwens_default_home_without_an_override(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".qwen").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert presence.detected_agents() == ("qwen",)


def test_detected_agents_honors_qwen_home_override(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    relocated = tmp_path / "elsewhere" / "qwen"
    relocated.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("QWEN_HOME", str(relocated))
    assert presence.detected_agents() == ("qwen",)


def test_detected_agents_treats_an_empty_qwen_home_as_unset(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".qwen").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("QWEN_HOME", "")
    assert presence.detected_agents() == ("qwen",)


def test_detected_agents_reads_kimis_default_home_without_an_override(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".kimi-code").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert presence.detected_agents() == ("kimi",)


def test_detected_agents_honors_kimi_code_home_override(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    relocated = tmp_path / "elsewhere" / "kimi-code"
    relocated.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIMI_CODE_HOME", str(relocated))
    assert presence.detected_agents() == ("kimi",)


def test_detected_agents_orders_new_agents_after_the_original_four(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for probe in (
        ".claude",
        ".codex",
        ".config/opencode",
        ".pi/agent",
        ".factory",
        ".qwen",
        ".kimi-code",
    ):
        (home / probe).mkdir(parents=True)
    (home / ".cursor").mkdir()
    (home / ".cursor" / "cli-config.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    for name in ("FACTORY_HOME_OVERRIDE", "CURSOR_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    assert presence.detected_agents() == presence.AGENTS


def test_active_binary_path_follows_xdg_data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert (
        presence.active_binary_path()
        == tmp_path / "data" / "hooklinesinker" / "bin" / "hooklinesinker"
    )


def test_presence_rows_warn_when_hooklinesinker_is_absent(tmp_path):
    runner = FakeRunner(raises=FileNotFoundError(2, "No such file or directory"))
    rows = presence.presence_rows(PresenceClient(executable=tmp_path / "hls", runner=runner))
    assert [r["state"] for r in rows] == ["warn"]
    assert presence.SETUP_COMMAND in rows[0]["detail"]


def test_presence_rows_error_on_malformed_installed_state(tmp_path):
    payload = {
        "protocol": 1,
        "version": "0.1.0",
        "checks": [
            {"name": "version", "ok": True, "detail": "hooklinesinker 0.1.0 (protocol 1)"},
            {"name": "status_parse_problems", "ok": False, "detail": "failed to parse a record"},
        ],
        "exitCode": 1,
    }
    runner = FakeRunner(
        results={
            "doctor": presence.RunResult(1, json.dumps(payload), ""),
            "consumers": ok({"protocol": 1, "consumers": [], "problems": []}),
        }
    )
    rows = presence.presence_rows(PresenceClient(executable=tmp_path / "hls", runner=runner))
    row = next(r for r in rows if r["label"] == "status_parse_problems")
    assert row["state"] == "error"
    assert row["detail"] == "failed to parse a record"


def test_presence_rows_report_an_unregistered_consumer(tmp_path):
    payload = {"protocol": 1, "version": "0.1.0", "checks": [], "exitCode": 0}
    runner = FakeRunner(
        results={
            "doctor": ok(payload),
            "consumers": ok({"protocol": 1, "consumers": [{"name": "juggler"}], "problems": []}),
        }
    )
    rows = presence.presence_rows(PresenceClient(executable=tmp_path / "hls", runner=runner))
    consumer_row = next(r for r in rows if r["label"] == "consumer")
    assert consumer_row["state"] == "warn"
    assert presence.SETUP_COMMAND in consumer_row["detail"]


def test_detected_agents_finds_cursor_by_its_cli_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    (home / ".cursor" / "cli-config.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CURSOR_CONFIG_DIR", raising=False)
    assert presence.detected_agents() == ("cursor",)


def test_detected_agents_skips_a_cursor_dir_the_ide_made(tmp_path, monkeypatch):
    """The Cursor IDE creates ~/.cursor without the CLI, which alone writes cli-config.json."""
    home = tmp_path / "home"
    (home / ".cursor" / "extensions").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CURSOR_CONFIG_DIR", raising=False)
    assert presence.detected_agents() == ()


def test_detected_agents_honors_cursor_config_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    relocated = tmp_path / "elsewhere"
    relocated.mkdir()
    (relocated / "cli-config.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(relocated))
    assert presence.detected_agents() == ("cursor",)
