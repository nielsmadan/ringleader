from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ringleader import cli, commands, registry
from ringleader.commands import conversion, doctor
from ringleader.portability.adapters import builtin_adapters
from ringleader.portability.capabilities import RunRequest, RunResult
from ringleader.portability.launch import ExecLauncher
from ringleader.portability.model import PreservationError
from ringleader.portability.operations import StoredRoots
from ringleader.presence import PresenceResult, PresenceSession
from tests.portability.test_service import Crash, OfflineRunner, source


@pytest.fixture
def environment(tmp_path, monkeypatch):
    for key in (
        "SESSION_TRACKER_PROJECTS_DIR",
        "CODEX_SESSIONS_DIR",
        "PI_SESSIONS_DIR",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "PI_CODING_AGENT_DIR",
        "OPENCODE_DB",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, directory in (
        ("HOME", "home"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
        ("TMPDIR", "tmp"),
    ):
        monkeypatch.setenv(key, str(tmp_path / directory))
    monkeypatch.setattr(conversion, "ProbeRunner", OfflineRunner)
    return tmp_path


def seed(base, count=1):
    reg = registry.empty_registry()
    for index in range(count):
        item = source(base / str(index))
        reg["threads"].append(
            {
                "sessionId": item.identity.native_id,
                "source": item.identity.agent,
                "file": str(item.roots.store / item.source_ref),
                "project": str(item.roots.cwd),
                "status": "active",
                "mtime": datetime.now(UTC).isoformat(),
            }
        )
    path = base / "registry.json"
    path.write_text(json.dumps(reg))
    return path, reg


def invoke(path, *args):
    return cli.main([*args, "--registry", str(path)])


def tree(root):
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def output(capsys):
    return json.loads(capsys.readouterr().out)


def test_public_surface():
    assert {
        "session convert",
        "session fork",
        "session handoff",
        "session resume",
        "session lineage",
        "data transaction list",
        "data transaction resume",
        "data transaction rollback",
    } <= commands.command_paths()


def test_convert_preview_publish_reuse_fork_lineage(environment, capsys):
    path, reg = seed(environment)
    identifier = reg["threads"][0]["sessionId"]
    before = tree(environment)
    assert (
        invoke(path, "session", "convert", identifier[:8], "--to", "claude", "--dry-run", "--json")
        == 0
    )
    assert output(capsys)["results"][0]["lifecycle"] == "preview"
    assert tree(environment) == before
    assert invoke(path, "session", "convert", identifier, "--to", "pi", "--json") == 0
    created = output(capsys)["results"][0]
    assert created["lifecycle"] == "active"
    assert invoke(path, "session", "convert", "pi:" + identifier, "--to", "pi", "--json") == 0
    reused = output(capsys)["results"][0]
    assert reused["target"] == created["target"] and reused["reused"]
    assert invoke(path, "session", "fork", identifier, "--json") == 0
    forked = output(capsys)
    assert forked["target"] != created["target"]
    assert invoke(path, "session", "lineage", identifier, "--json") == 0
    assert {item["kind"] for item in output(capsys)} == {"convert", "fork"}
    assert invoke(path, "data", "transaction", "list", "--json") == 0
    assert {item["state"] for item in output(capsys)} == {"complete"}


def test_dry_run_convert_after_a_real_one_reports_the_earlier_target_as_reused(environment, capsys):
    path, reg = seed(environment)
    identifier = reg["threads"][0]["sessionId"]
    assert invoke(path, "session", "convert", identifier, "--to", "claude", "--json") == 0
    created = output(capsys)["results"][0]

    assert (
        invoke(path, "session", "convert", identifier, "--to", "claude", "--dry-run", "--json") == 0
    )
    preview = output(capsys)["results"][0]
    assert preview["reused"] is True
    assert preview["dry_run"] is True
    assert preview["target"] == created["target"]
    assert preview["operation_id"] == created["operation_id"]
    assert preview["lifecycle"] == created["lifecycle"]


def test_corrupt_verified_state_fails_gracefully_instead_of_crashing(
    environment, capsys, monkeypatch
):
    path, reg = seed(environment)
    identifier = reg["threads"][0]["sessionId"]
    verified = environment / "verified-targets.json"
    verified.write_text('{"version": 1, "records": [{"agent": "pi"}]}')

    assert (
        invoke(
            path,
            "session",
            "convert",
            identifier,
            "--to",
            "pi",
            "--verified",
            str(verified),
            "--json",
        )
        == 1
    )
    error = output(capsys)["error"]
    assert "Invalid self-verification record" in error and str(verified) in error

    # lineage is read-only and needs no self-verification state, so it takes no --verified.
    assert invoke(path, "session", "lineage", identifier, "--json") == 0
    with pytest.raises(SystemExit) as rejected:
        invoke(path, "session", "lineage", identifier, "--verified", str(verified))
    assert rejected.value.code == 2

    monkeypatch.setattr(doctor, "presence_rows", lambda _: [])
    for module in (doctor.skills, doctor.hooks, doctor.statusline):
        monkeypatch.setattr(module, "cmd_status", lambda _: 0)
    assert (
        invoke(
            path,
            "doctor",
            "--settings",
            str(environment / "settings.json"),
            "--verified",
            str(verified),
        )
        == 2
    )
    assert "Invalid self-verification record" in capsys.readouterr().out


def test_dry_run_reports_would_self_verify_for_an_unverified_version(
    environment, capsys, monkeypatch
):
    path, reg = seed(environment)
    identifier = reg["threads"][0]["sessionId"]
    source_agent = reg["threads"][0]["source"]

    class UnknownVersionRunner:
        def run(self, request):
            assert request.argv[-1] == "--version"
            return RunResult(0, b"9.9.9", b"")

    monkeypatch.setattr(conversion, "ProbeRunner", UnknownVersionRunner)
    assert invoke(path, "session", "convert", identifier, "--to", "pi", "--dry-run", "--json") == 0
    result = output(capsys)
    assert result == {
        "results": [],
        "wouldSelfVerify": [
            {
                "selector": f"{source_agent}:{identifier}",
                "wouldSelfVerify": result["wouldSelfVerify"][0]["wouldSelfVerify"],
            }
        ],
        "errors": [],
    }
    assert "needs self-verification" in result["wouldSelfVerify"][0]["wouldSelfVerify"]

    assert invoke(path, "session", "convert", identifier, "--to", "pi", "--dry-run") == 0
    assert "would self-verify; " in capsys.readouterr().out


def test_exact_qualified_prefix_and_ambiguities(environment, capsys):
    path, reg = seed(environment)
    original = reg["threads"][0]
    identifier = original["sessionId"]
    reg["threads"].extend(
        [{**original, "sessionId": identifier + "-long"}, {**original, "source": "claude-code"}]
    )
    path.write_text(json.dumps(reg))
    assert invoke(path, "session", "convert", identifier, "--to", "pi", "--dry-run", "--json") == 1
    assert "Ambiguous" in output(capsys)["errors"][0]["error"]
    assert (
        invoke(
            path, "session", "convert", "pi:" + identifier[:8], "--to", "pi", "--dry-run", "--json"
        )
        == 1
    )
    assert "Ambiguous" in output(capsys)["errors"][0]["error"]
    assert (
        invoke(path, "session", "convert", "pi:" + identifier, "--to", "pi", "--dry-run", "--json")
        == 0
    )
    assert output(capsys)["results"][0]["source"]["agent"] == "pi"
    assert invoke(path, "session", "lineage", "claude:" + identifier, "--json") == 0
    assert output(capsys) == []


def test_open_filters_effective_status_kept_and_workspace(environment, capsys):
    path, reg = seed(environment, 7)
    reg["threads"][1]["snoozedAt"] = datetime.now(UTC).isoformat()
    reg["threads"][2]["filteredBy"] = "hidden*"
    reg["threads"][3].update(status="unfinished", mtime="2000-01-01T00:00:00+00:00")
    reg["threads"][4].update(
        status="unfinished", mtime="2000-01-01T00:00:00+00:00", decision="keep"
    )
    reg["threads"][5]["decision"] = "harvested"
    reg["threads"][6]["status"] = "blocked"
    path.write_text(json.dumps(reg))
    assert (
        invoke(
            path,
            "session",
            "convert",
            "--open",
            "--to",
            "pi",
            "--from",
            "pi",
            "--workspace",
            str(environment),
            "--dry-run",
            "--json",
        )
        == 0
    )
    results = output(capsys)["results"]
    assert {item["source"]["native_id"] for item in results} == {
        reg["threads"][index]["sessionId"] for index in (0, 4, 6)
    }
    assert (
        invoke(
            path,
            "session",
            "convert",
            "--open",
            "--to",
            "pi",
            "--workspace",
            reg["threads"][0]["project"],
            "--dry-run",
            "--json",
        )
        == 0
    )
    assert len(output(capsys)["results"]) == 1
    assert (
        invoke(
            path,
            "session",
            "convert",
            reg["threads"][1]["sessionId"],
            "--to",
            "pi",
            "--dry-run",
            "--json",
        )
        == 0
    )
    assert len(output(capsys)["results"]) == 1


def test_running_requires_complete_presence_and_ignores_visibility(
    environment, monkeypatch, capsys
):
    path, reg = seed(environment, 2)
    reg["threads"][0].update(status="finished", filteredBy="*", snoozedAt="2026-01-01")
    path.write_text(json.dumps(reg))
    presence = PresenceResult(
        (
            PresenceSession("pi", reg["threads"][0]["sessionId"], True),
            PresenceSession("pi", reg["threads"][1]["sessionId"], False),
        )
    )
    monkeypatch.setattr(conversion.PresenceClient, "sessions", lambda _: presence)
    assert invoke(path, "session", "convert", "--running", "--to", "pi", "--dry-run", "--json") == 0
    assert [item["source"]["native_id"] for item in output(capsys)["results"]] == [
        reg["threads"][0]["sessionId"]
    ]
    before = tree(environment)
    for broken in (
        replace(presence, problems=("unrecognized record",)),
        PresenceResult(available=False, problems=("not installed",)),
    ):
        monkeypatch.setattr(conversion.PresenceClient, "sessions", lambda _, answer=broken: answer)
        assert invoke(path, "session", "convert", "--running", "--to", "pi", "--json") == 1
        assert "rl data presence install" in output(capsys)["error"]
        assert tree(environment) == before


def test_selector_conflicts(environment):
    path, reg = seed(environment)
    for selectors in ((reg["threads"][0]["sessionId"], "--open"), ("--open", "--running"), ()):
        with pytest.raises(SystemExit) as error:
            invoke(path, "session", "convert", *selectors, "--to", "pi")
        assert error.value.code == 2


def test_archived_source_reads_archive_without_unarchive(environment, capsys):
    path, reg = seed(environment)
    row = reg["threads"].pop()
    archived = environment / "archive.jsonl"
    Path(row["file"]).rename(archived)
    reg["archived"].append(
        {
            "sessionId": row["sessionId"],
            "source": "pi",
            "archivePath": str(archived),
            "project": "git-label",
            "originalPath": row["file"],
            "thread": row,
        }
    )
    path.write_text(json.dumps(reg))
    assert invoke(path, "session", "convert", row["sessionId"], "--to", "pi", "--json") == 0
    assert output(capsys)["results"][0]["lifecycle"] == "active"
    assert archived.exists()
    assert registry.load_registry(path)["archived"][0]["archivePath"] == str(archived)
    assert invoke(path, "session", "resume", row["sessionId"], "--json") == 1
    assert "archived" in output(capsys)["error"]


def test_partial_batch_retry_preserves_success(environment, capsys):
    path, reg = seed(environment, 2)
    damaged = Path(reg["threads"][1]["file"])
    saved = damaged.read_bytes()
    damaged.write_text('{"unknown":"conversation"}\n')
    identifiers = [row["sessionId"] for row in reg["threads"]]
    assert (
        invoke(path, "session", "convert", *identifiers, "nonexistent", "--to", "pi", "--json") == 1
    )
    first = output(capsys)
    assert len(first["results"]) == 1 and len(first["errors"]) == 2
    damaged.write_bytes(saved)
    assert invoke(path, "session", "convert", *identifiers, "--to", "pi", "--json") == 0
    second = output(capsys)
    assert second["results"][0]["target"] == first["results"][0]["target"]
    assert [row["reused"] for row in second["results"]] == [True, False]


class RecordingLauncher:
    def __init__(self):
        self.calls = []
        self.validation_error = None
        self.launch_error = None

    def validate(self, request):
        if self.validation_error:
            raise PreservationError(self.validation_error)

    def launch(self, request):
        self.calls.append(request)
        if self.launch_error:
            raise OSError(self.launch_error)


@pytest.fixture
def launches(monkeypatch):
    launcher = RecordingLauncher()
    launcher.calls = []
    monkeypatch.setattr(conversion, "ExecLauncher", lambda: launcher)
    return launcher


def test_fork_resume_records_effective_cwd_argv_roots_and_failure_retains_target(
    environment, launches, capsys, monkeypatch
):
    path, reg = seed(environment)
    pinned = environment / "pinned workspace"
    pinned.mkdir()
    reg["threads"][0].update(project=str(pinned), locationPinned=True)
    path.write_text(json.dumps(reg))
    custom = environment / "native pi"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(custom))
    identifier = reg["threads"][0]["sessionId"]
    assert invoke(path, "session", "fork", identifier, "--resume", "--json") == 0
    created = output(capsys)
    request = launches.calls[0]
    assert request.argv == ("pi", "--session", created["target"]["native_id"])
    assert request.cwd == pinned
    assert request.env == ()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(environment / "different"))
    assert invoke(path, "session", "resume", created["target"]["native_id"]) == 0
    assert launches.calls[-1].env == (("PI_CODING_AGENT_DIR", str(custom)),)
    launches.launch_error = "exec failed"
    assert invoke(path, "session", "handoff", identifier, "--to", "pi", "--json") == 1
    captured = capsys.readouterr()
    retained = json.loads(captured.out)
    assert f"rl session resume pi:{retained['target']['native_id']}" in captured.err
    assert registry.find_session(registry.load_registry(path), retained["target"]["native_id"])


def test_launch_validation_precedes_publication(environment, launches, capsys):
    path, reg = seed(environment)
    before = tree(environment)
    launches.validation_error = "missing executable"
    assert (
        invoke(path, "session", "fork", reg["threads"][0]["sessionId"], "--resume", "--json") == 1
    )
    assert output(capsys)["error"] == "missing executable"
    assert tree(environment) == before


def test_exec_launcher_missing_executable_and_cwd(environment, monkeypatch):
    launcher = ExecLauncher()
    monkeypatch.setenv("PATH", str(environment / "bin"))
    request = RunRequest(("missing-binary", "resume", "abc"), environment, ())
    with pytest.raises(PreservationError, match="executable"):
        launcher.validate(request)
    binary = environment / "bin" / "fake"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    with pytest.raises(PreservationError, match="workspace"):
        launcher.validate(replace(request, argv=("fake",), cwd=environment / "gone"))


def test_exec_launcher_inherits_terminal_environment_and_restores_cwd_on_error(
    environment, monkeypatch
):
    binary = environment / "native"
    binary.write_text("fake")
    binary.chmod(0o700)
    monkeypatch.setenv("SAMPLE_TERMINAL", "inherited")
    called = []

    def execute(file, argv, env):
        called.append((file, argv, env, Path.cwd()))
        raise OSError("exec failure")

    monkeypatch.setattr(os, "execvpe", execute)
    previous = Path.cwd()
    request = RunRequest(
        (str(binary), "--session", "id with ; literal"), environment, (("NATIVE_ROOT", "root"),)
    )
    with pytest.raises(OSError, match="exec failure"):
        ExecLauncher().launch(request)
    assert called[0][1] == request.argv
    assert called[0][2]["SAMPLE_TERMINAL"] == "inherited"
    assert called[0][2]["NATIVE_ROOT"] == "root"
    assert called[0][3] == environment and Path.cwd() == previous


def test_transactions_restart_resume_rollback_and_no_write_list(environment, monkeypatch, capsys):
    path, reg = seed(environment)
    identifier = reg["threads"][0]["sessionId"]
    before = tree(environment)
    assert invoke(path, "data", "transaction", "list", "--json") == 0
    assert output(capsys) == [] and tree(environment) == before
    original_service = conversion.service

    def crashing(args):
        worker = original_service(args)

        def fault(name, operation):
            if name == "after:create":
                raise Crash(operation.id)

        worker.fault = fault
        return worker

    monkeypatch.setattr(conversion, "service", crashing)
    with pytest.raises(Crash):
        invoke(path, "session", "convert", identifier, "--to", "pi")
    monkeypatch.setattr(conversion, "service", original_service)
    assert invoke(path, "data", "transaction", "list", "--json") == 0
    operation = output(capsys)[0]
    assert operation["state"] == "prepared"
    assert invoke(path, "data", "transaction", "resume", operation["id"], "--json") == 0
    assert output(capsys)["lifecycle"] == "active"
    assert invoke(path, "data", "transaction", "rollback", operation["id"], "--json") == 1
    assert "forward" in output(capsys)["error"].lower()
    monkeypatch.setattr(conversion, "service", crashing)
    with pytest.raises(Crash):
        invoke(path, "session", "fork", identifier)
    monkeypatch.setattr(conversion, "service", original_service)
    assert invoke(path, "data", "transaction", "list", "--json") == 0
    private = next(row for row in output(capsys) if row["state"] == "prepared")
    assert invoke(path, "data", "transaction", "rollback", private["id"], "--json") == 0
    assert output(capsys)["state"] == "rolled_back"


def test_doctor_conversion_sections_are_read_only(environment, monkeypatch, capsys):
    path = environment / "absent-registry.json"
    monkeypatch.setattr(doctor, "presence_rows", lambda _: [])
    for module in (doctor.skills, doctor.hooks, doctor.statusline):
        monkeypatch.setattr(module, "cmd_status", lambda _: 0)
    before = tree(environment)
    assert invoke(path, "doctor", "--settings", str(environment / "settings.json")) == 0
    text = capsys.readouterr().out
    assert "conversion compatibility" in text and "unfinished conversion transactions" in text
    assert tree(environment) == before


def test_explicit_xdg_roots_survive_journal_serialization(environment):
    roots = builtin_adapters().roots("pi", environment)
    stored = StoredRoots.capture(roots)
    assert StoredRoots(**asdict(stored)).native() == roots
    assert roots.data == environment / "data"
    assert roots.state == environment / "state"
    assert roots.temporary == environment / "tmp"


def test_absent_registry_open_preview_and_transaction_list_create_nothing(environment, capsys):
    path = environment / "nonexistent" / "registry.json"
    before = tree(environment)
    assert invoke(path, "session", "convert", "--open", "--to", "pi", "--dry-run", "--json") == 0
    assert output(capsys) == {"results": [], "wouldSelfVerify": [], "errors": []}
    assert invoke(path, "data", "transaction", "list", "--json") == 0
    assert output(capsys) == []
    assert tree(environment) == before


def test_reused_archived_and_deleted_target_is_reported_and_cannot_resume(environment, capsys):
    path, reg = seed(environment)
    identifier = reg["threads"][0]["sessionId"]
    assert invoke(path, "session", "convert", identifier, "--to", "pi", "--json") == 0
    created = output(capsys)["results"][0]
    target = created["target"]["native_id"]
    current = registry.load_registry(path)
    row = registry.find_session(current, target)
    archive = environment / "archived-target.jsonl"
    Path(row["file"]).rename(archive)
    registry.archive_session(
        current, "pi:" + target, str(archive), "2026-01-01", project="git-label"
    )
    path.write_text(json.dumps(current))
    assert invoke(path, "session", "convert", identifier, "--to", "pi", "--json") == 0
    assert output(capsys)["results"][0]["lifecycle"] == "archived"
    current = registry.load_registry(path)
    archived = current["archived"].pop()
    current["deleted"].append({"sessionId": target, "source": "pi", "thread": archived})
    path.write_text(json.dumps(current))
    assert invoke(path, "session", "convert", identifier, "--to", "pi", "--json") == 0
    assert output(capsys)["results"][0]["lifecycle"] == "deleted"
    assert invoke(path, "session", "resume", "pi:" + target, "--json") == 1
    assert "deleted" in output(capsys)["error"]
    current["deleted"] = []
    path.write_text(json.dumps(current))
    assert invoke(path, "session", "lineage", "pi:" + target, "--json") == 0
    assert output(capsys)[0]["target"]["nativeId"] == target


def test_opencode_source_without_file_and_schema_backed_target(environment, capsys, monkeypatch):
    from ringleader.portability.opencode import OpenCodeAdapter
    from tests.portability.test_opencode import roots as build_roots
    from tests.portability.test_opencode import stage

    roots = build_roots.__wrapped__(environment / "opencode-fixture")
    monkeypatch.setenv("XDG_DATA_HOME", str(roots.store.parent))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(roots.config))
    source_request = source(environment / "pi-fixture")
    conversation = builtin_adapters().reader("pi").snapshot(source_request).conversation
    native = stage(roots, conversation)
    OpenCodeAdapter().register_stage(native)
    reg = registry.empty_registry()
    reg["threads"].append(
        {
            "sessionId": native.identity.native_id,
            "source": "opencode",
            "sourceRef": native.identity.native_id,
            "file": None,
            "project": str(roots.cwd),
            "status": "active",
        }
    )
    path = environment / "registry.json"
    path.write_text(json.dumps(reg))
    before = tree(environment)
    assert (
        invoke(
            path,
            "session",
            "convert",
            native.identity.native_id,
            "--to",
            "pi",
            "--dry-run",
            "--json",
        )
        == 0
    )
    assert output(capsys)["results"][0]["source"]["agent"] == "opencode"
    assert tree(environment) == before
    assert (
        invoke(path, "session", "fork", native.identity.native_id, "--to", "opencode", "--json")
        == 0
    )
    created = output(capsys)
    assert created["lifecycle"] == "active"
    assert "native runtime smoke unverified" in created["diagnostics"][0]
    assert (
        registry.find_session(registry.load_registry(path), created["target"]["native_id"])["file"]
        is None
    )


def test_running_unknown_identity_is_a_failure_without_fuzzy_matching(
    environment, monkeypatch, capsys
):
    path, reg = seed(environment)
    reg["threads"][0]["title"] = "not-a-native-id"
    path.write_text(json.dumps(reg))
    monkeypatch.setattr(
        conversion.PresenceClient,
        "sessions",
        lambda _: PresenceResult((PresenceSession("pi", "not-a-native-id", True),)),
    )
    assert invoke(path, "session", "convert", "--running", "--to", "pi", "--json") == 1
    assert "not found" in output(capsys)["errors"][0]["error"]


def test_running_identity_must_be_exact(environment, monkeypatch, capsys):
    path, reg = seed(environment)
    prefix = reg["threads"][0]["sessionId"][:8]
    monkeypatch.setattr(
        conversion.PresenceClient,
        "sessions",
        lambda _: PresenceResult((PresenceSession("pi", prefix, True),)),
    )
    assert invoke(path, "session", "convert", "--running", "--to", "pi", "--json") == 1
    assert "exact identity" in output(capsys)["errors"][0]["error"]


def test_public_probe_uses_fake_version_binary_and_isolated_environment(
    environment, monkeypatch, capsys
):
    from ringleader.portability.launch import ProbeRunner
    from ringleader.portability.pi import PiAdapter

    path, reg = seed(environment)
    binary = environment / "fake-pi"
    binary.write_text(
        '#!/bin/sh\n[ "$1" = "--version" ] || exit 2\n[ -z "$TEST_PROVIDER_CREDENTIAL" ] || exit 3\n[ -d "$HOME" ] || exit 4\nprintf "0.85.1\\n"\n'
    )
    binary.chmod(0o700)
    monkeypatch.setenv("TEST_PROVIDER_CREDENTIAL", "fixture-only")
    original = conversion.service

    def build(args):
        worker = original(args)
        adapters = builtin_adapters()
        adapter = adapters.get("pi")
        writer = PiAdapter(executable=str(binary))
        adapters._adapters["pi"] = replace(adapter, writer=writer)
        worker.adapters = adapters
        worker.runner = ProbeRunner()
        return worker

    monkeypatch.setattr(conversion, "service", build)
    before = tree(environment)
    assert (
        invoke(
            path,
            "session",
            "convert",
            reg["threads"][0]["sessionId"],
            "--to",
            "pi",
            "--compact",
            "--dry-run",
            "--json",
        )
        == 0
    )
    result = output(capsys)["results"][0]
    assert result["report"]["omitted_calls"] == 1
    assert tree(environment) == before


@pytest.mark.parametrize("recorded", [True, False])
def test_copied_opencode_source_any_path_is_read_only_but_not_a_target(
    environment, monkeypatch, capsys, recorded
):
    from ringleader.portability.capabilities import SourceRequest
    from ringleader.portability.opencode import OpenCodeAdapter
    from tests.portability.test_opencode import roots as build_roots
    from tests.portability.test_opencode import stage

    roots = build_roots.__wrapped__(environment / "opencode-fixture")
    conversation = (
        builtin_adapters().reader("pi").snapshot(source(environment / "pi-fixture")).conversation
    )
    native = stage(roots, conversation)
    OpenCodeAdapter().register_stage(native)
    copied = environment / "arbitrary" / "copied.sqlite"
    copied.parent.mkdir()
    (roots.store / "opencode.db").rename(copied)
    monkeypatch.setenv("OPENCODE_DB", str(copied))
    reg = registry.empty_registry()
    reg["threads"].append(
        {
            "sessionId": native.identity.native_id,
            "source": "opencode",
            "sourceRef": native.identity.native_id,
            "file": str(copied) if recorded else None,
            "project": str(roots.cwd),
            "status": "active",
        }
    )
    path = environment / "registry.json"
    path.write_text(json.dumps(reg))
    before = tree(environment)
    inspection = OpenCodeAdapter().inspect_source(
        SourceRequest(native.identity, roots, str(copied))
    )
    assert inspection.supported
    assert (
        invoke(
            path,
            "session",
            "convert",
            native.identity.native_id,
            "--to",
            "pi",
            "--dry-run",
            "--json",
        )
        == 0
    )
    assert output(capsys)["results"][0]["lifecycle"] == "preview"
    assert (
        invoke(path, "session", "convert", native.identity.native_id, "--to", "opencode", "--json")
        == 1
    )
    assert "requires a store named opencode" in output(capsys)["errors"][0]["error"]
    assert invoke(path, "session", "resume", native.identity.native_id, "--json") == 1
    assert "requires a store named opencode" in output(capsys)["error"]
    assert tree(environment) == before


def raw_presence(monkeypatch, environment, payload):
    from ringleader.presence import PresenceClient, RunResult

    client = PresenceClient(
        executable=environment / "fake-hooklinesinker",
        runner=lambda _: RunResult(0, json.dumps(payload), ""),
    )
    monkeypatch.setattr(conversion, "PresenceClient", lambda: client)


@pytest.mark.parametrize("agent", ["claude", "claude-code"])
def test_running_claude_protocol_alias_with_source_filter(environment, monkeypatch, capsys, agent):
    path, reg = seed(environment)
    assert (
        invoke(
            path, "session", "convert", reg["threads"][0]["sessionId"], "--to", "claude", "--json"
        )
        == 0
    )
    target = output(capsys)["results"][0]["target"]
    raw_presence(
        monkeypatch,
        environment,
        {
            "protocol": 1,
            "sessions": [
                {"agent": "claude", "running": True, "session": {"id": target["native_id"]}}
            ],
        },
    )
    assert (
        invoke(
            path,
            "session",
            "convert",
            "--running",
            "--from",
            agent,
            "--to",
            "pi",
            "--dry-run",
            "--json",
        )
        == 0
    )
    results = output(capsys)["results"]
    assert [item["source"] for item in results] == [target]


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol": 1},
        {"protocol": 1, "sessions": None},
        {"protocol": 1, "sessions": {}},
        {"protocol": 1, "sessions": "invalid"},
        {"protocol": True, "sessions": []},
        {"protocol": 1, "sessions": [], "problems": "unreported error"},
    ],
)
def test_malformed_presence_envelopes_fail_public_running_selection(
    environment, monkeypatch, capsys, payload
):
    path, _ = seed(environment)
    raw_presence(monkeypatch, environment, payload)
    before = tree(environment)
    assert invoke(path, "session", "convert", "--running", "--to", "pi", "--json") == 1
    error = output(capsys)["error"]
    assert "Running selection unavailable or incomplete" in error
    assert "rl data presence install" in error
    assert tree(environment) == before


@pytest.mark.parametrize("running", [None, "false", "true", 0, 1, [], {}])
def test_running_requires_native_boolean_even_with_other_valid_records(
    environment, monkeypatch, capsys, running
):
    path, reg = seed(environment, 2)
    records = [
        {"agent": "pi", "running": True, "session": {"id": reg["threads"][0]["sessionId"]}},
        {"agent": "pi", "running": running, "session": {"id": reg["threads"][1]["sessionId"]}},
    ]
    if running is None:
        del records[1]["running"]
    raw_presence(monkeypatch, environment, {"protocol": 1, "sessions": records})
    before = tree(environment)
    assert invoke(path, "session", "convert", "--running", "--to", "pi", "--json") == 1
    error = output(capsys)["error"]
    assert "unrecognized status record" in error and "rl data presence install" in error
    assert tree(environment) == before


def test_complete_empty_presence_is_a_success(environment, monkeypatch, capsys):
    path, _ = seed(environment)
    raw_presence(monkeypatch, environment, {"protocol": 1, "sessions": []})
    assert invoke(path, "session", "convert", "--running", "--to", "pi", "--json") == 0
    assert output(capsys) == {"results": [], "wouldSelfVerify": [], "errors": []}


@pytest.mark.parametrize("lifecycle", ["active", "archived", "deleted"])
def test_conversion_retry_ignores_execution_roots_and_retains_original_roots(
    environment, monkeypatch, capsys, lifecycle
):
    path, reg = seed(environment)
    identifier = reg["threads"][0]["sessionId"]
    assert invoke(path, "session", "convert", identifier, "--to", "pi", "--json") == 0
    initial = output(capsys)["results"][0]
    target = initial["target"]["native_id"]
    current = registry.load_registry(path)
    record = registry.find_session(current, "pi:" + target)
    initial_roots = record["nativeRoots"]
    if lifecycle != "active":
        archive = environment / "archive" / "target.jsonl"
        archive.parent.mkdir()
        Path(record["file"]).rename(archive)
        registry.archive_session(
            current, "pi:" + target, str(archive), "2026-01-01", project="label"
        )
        if lifecycle == "deleted":
            archived = current["archived"].pop()
            archive.unlink()
            current["deleted"].append({"sessionId": target, "source": "pi", "thread": archived})
        path.write_text(json.dumps(current))
    monkeypatch.setenv("TMPDIR", str(environment / "different-temporary"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(environment / "different-cache"))
    assert invoke(path, "session", "convert", identifier, "--to", "pi", "--json") == 0
    repeated = output(capsys)["results"][0]
    assert repeated["target"] == initial["target"]
    assert repeated["reused"] and repeated["lifecycle"] == lifecycle
    current = registry.load_registry(path)
    bucket = "threads" if lifecycle == "active" else lifecycle
    retained = registry.find_session(current, "pi:" + target, bucket=bucket)
    while retained.get("thread"):
        retained = retained["thread"]
    assert retained["nativeRoots"] == initial_roots
    assert invoke(path, "data", "transaction", "list", "--json") == 0
    assert [operation["id"] for operation in output(capsys)] == [initial["operation_id"]]
