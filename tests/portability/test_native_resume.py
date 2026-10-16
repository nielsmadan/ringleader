from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest
from ringleader import registry
from ringleader.commands import doctor
from ringleader.commands import resume as resume_cmd
from ringleader.portability import native_resume as module
from ringleader.portability import service as service_module
from ringleader.portability.capabilities import RunResult
from ringleader.portability.claude import ClaudeAdapter
from ringleader.portability.model import PreservationError
from ringleader.portability.native_resume import ConversionFailure, native_resume
from ringleader.portability.selection import resolve, sessions
from ringleader.portability.service import ConversionService
from ringleader.portability.verification import (
    HARNESS_VERSION,
    VerificationRecord,
    harness_inputs,
    load_verified,
)
from ringleader.portability.verify import claude as claude_harness
from ringleader.portability.verify import workflow
from ringleader.portability.verify.common import HarnessResult
from tests.portability.test_cli import environment, seed, tree
from tests.portability.test_service import OfflineRunner

__all__ = ["environment"]


class AcceptingLauncher:
    validated: ClassVar[list] = []

    def validate(self, request):
        AcceptingLauncher.validated.append(request)


class UnverifiedClaudeRunner:
    def run(self, request):
        assert request.argv[-1] == "--version"
        if request.argv[0] == "claude":
            return RunResult(0, b"9.9.9 (Claude Code)", b"")
        return OfflineRunner().run(request)


class TimingOutRunner:
    def run(self, request):
        raise subprocess.TimeoutExpired(request.argv, 15)


progress_lines: list[str] = []


@pytest.fixture
def offline(environment, monkeypatch):
    progress_lines.clear()
    monkeypatch.setattr(module, "ProbeRunner", OfflineRunner)
    monkeypatch.setattr(module, "ExecLauncher", AcceptingLauncher)
    AcceptingLauncher.validated = []
    path, reg = seed(environment)
    return environment, path, reg["threads"][0]


def _selected(path, thread):
    return resolve(sessions(registry.load_registry(path)), f"pi:{thread['sessionId']}")


def _resume(base, path, thread, target="claude", *, dry_run=False, allow_verify=False):
    return native_resume(
        _selected(path, thread),
        target,
        registry_path=path,
        operations_path=base / "operations",
        verified_path=base / "verified.json",
        dry_run=dry_run,
        allow_verify=allow_verify,
        report=progress_lines.append,
    )


def _claude_on_path(base, monkeypatch):
    binary = base / "bin" / "claude"
    binary.parent.mkdir(exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ['PATH']}")


def test_a_first_attempt_converts_and_launches_the_published_target(offline):
    base, path, thread = offline

    result = _resume(base, path, thread)
    assert result.outcome == "converted"
    assert result.target is not None and result.target.agent == "claude-code"
    assert result.launch is not None
    assert result.launch.argv == (
        "claude",
        "--resume",
        result.target.native_id,
        "--model",
        "default",
    )
    assert result.launch.cwd == Path(thread["project"])
    # CLAUDE_CONFIG_DIR is unset here; setting it would rename Claude's keychain item
    assert result.launch.env == ()
    assert len(AcceptingLauncher.validated) == 1
    created = registry.find_session(registry.load_registry(path), result.target.native_id)
    assert Path(created["file"]).is_file()


def test_a_second_attempt_reopens_the_same_target(offline):
    base, path, thread = offline
    first = _resume(base, path, thread)

    second = _resume(base, path, thread)
    assert second.outcome == "reused"
    assert second.target == first.target
    assert second.launch == first.launch


def test_a_changed_target_is_reopened_rather_than_forked(offline):
    base, path, thread = offline
    first = _resume(base, path, thread)
    assert first.target is not None
    created = registry.find_session(registry.load_registry(path), first.target.native_id)
    Path(created["file"]).write_bytes(b"edited outside rl")

    second = _resume(base, path, thread)
    assert (second.outcome, second.target) == ("reused", first.target)


def test_a_dry_run_previews_a_new_conversion_and_writes_nothing(offline):
    base, path, thread = offline
    before = tree(base)

    result = _resume(base, path, thread, dry_run=True)
    assert (result.outcome, result.target, result.launch) == ("would-convert", None, None)
    assert tree(base) == before
    assert AcceptingLauncher.validated == []


def test_a_dry_run_names_the_target_a_real_run_would_reopen_and_writes_nothing(offline):
    base, path, thread = offline
    first = _resume(base, path, thread)
    before = tree(base)

    result = _resume(base, path, thread, dry_run=True)
    assert (result.outcome, result.target) == ("reused", first.target)
    assert tree(base) == before


def test_reopening_a_target_never_requires_verifying_the_sources_current_version(
    offline, monkeypatch
):
    base, path, thread = offline
    first = _resume(base, path, thread)
    monkeypatch.setattr(module, "ProbeRunner", UnverifiedClaudeRunner)
    monkeypatch.setattr(workflow, "self_verify", pytest.fail)

    dry = _resume(base, path, thread, dry_run=True)
    assert (dry.outcome, dry.target) == ("reused", first.target)

    second = _resume(base, path, thread, allow_verify=False)
    assert (second.outcome, second.target) == ("reused", first.target)


def _archive(path, target_id, base):
    current = registry.load_registry(path)
    row = registry.find_session(current, target_id)
    archive = base / "archived-target.jsonl"
    Path(row["file"]).rename(archive)
    registry.archive_session(current, f"claude-code:{target_id}", str(archive), "2026-01-01")
    path.write_text(json.dumps(current))


def test_an_archived_reuse_forks_a_fresh_target(offline):
    base, path, thread = offline
    first = _resume(base, path, thread)
    assert first.target is not None
    _archive(path, first.target.native_id, base)
    before = tree(base)

    preview = _resume(base, path, thread, dry_run=True)
    assert (preview.outcome, preview.previous) == ("forked", "archived")
    assert tree(base) == before

    forked = _resume(base, path, thread)
    assert (forked.outcome, forked.previous) == ("forked", "archived")
    assert forked.target is not None and forked.target != first.target
    assert forked.launch is not None
    assert forked.launch.argv == (
        "claude",
        "--resume",
        forked.target.native_id,
        "--model",
        "default",
    )


def test_after_an_archived_target_is_forked_later_resumes_reopen_the_fork(offline):
    base, path, thread = offline
    first = _resume(base, path, thread)
    assert first.target is not None
    _archive(path, first.target.native_id, base)
    forked = _resume(base, path, thread)
    assert forked.outcome == "forked"

    preview = _resume(base, path, thread, dry_run=True)
    assert (preview.outcome, preview.target) == ("reused", forked.target)
    again = _resume(base, path, thread)
    assert (again.outcome, again.target) == ("reused", forked.target)


def _continue_source(thread):
    source = Path(thread["file"])
    parent = json.loads(source.read_text().splitlines()[-1])["id"]
    turn = {
        "type": "message",
        "id": "aaaaaaaa",
        "parentId": parent,
        "timestamp": "2026-10-09T00:00:00Z",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "one more thing"}],
            "timestamp": 1791504000000,
        },
    }
    with source.open("a") as stream:
        stream.write(json.dumps(turn) + "\n")


def test_a_source_that_gained_turns_converts_anew_instead_of_reopening(offline):
    base, path, thread = offline
    first = _resume(base, path, thread)
    _continue_source(thread)

    preview = _resume(base, path, thread, dry_run=True)
    assert preview.outcome == "would-convert"
    second = _resume(base, path, thread)
    assert second.outcome == "converted"
    assert second.target is not None and second.target != first.target
    created = registry.find_session(registry.load_registry(path), second.target.native_id)
    assert "one more thing" in Path(created["file"]).read_text()


def test_a_target_from_an_older_transform_version_is_converted_anew(offline, monkeypatch):
    base, path, thread = offline
    first = _resume(base, path, thread)
    monkeypatch.setattr(service_module, "PORTABLE_TRANSFORM_VERSION", 999)

    second = _resume(base, path, thread)
    assert second.outcome == "converted"
    assert second.target != first.target


def test_a_conversion_bound_to_another_registry_is_never_reopened(offline):
    base, path, thread = offline
    _resume(base, path, thread)
    other = base / "other-registry.json"
    other.write_text(path.read_text())

    with pytest.raises(ConversionFailure) as caught:
        _resume(base, other, thread)
    assert f"Conversion belongs to registry {path.resolve()}" in str(caught.value)


def test_an_adapter_bug_is_a_conversion_failure_not_a_refusal(offline, monkeypatch):
    base, path, thread = offline

    def broken(self, request):
        raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setattr(ConversionService, "prepare", broken)
    with pytest.raises(ConversionFailure) as caught:
        _resume(base, path, thread)
    assert str(caught.value) == (
        "native conversion to claude failed: TypeError: 'NoneType' object is not "
        "subscriptable; nothing was published"
    )


def test_a_target_without_a_portability_adapter_is_a_refusal(offline):
    base, path, thread = offline

    result = _resume(base, path, thread, target="kimi")
    assert (result.outcome, result.reason) == (
        "refused",
        "No portability adapter registered for kimi",
    )


def test_a_source_the_reader_cannot_read_is_refused_before_any_journal(offline):
    base, path, thread = offline
    Path(thread["file"]).write_text("not a pi session\n")

    result = _resume(base, path, thread)
    assert result.outcome == "refused"
    assert result.reason
    assert not (base / "operations").exists()


def test_a_dry_run_reports_an_unverified_version_and_never_verifies(offline, monkeypatch):
    base, path, thread = offline
    monkeypatch.setattr(module, "ProbeRunner", UnverifiedClaudeRunner)
    monkeypatch.setattr(workflow, "self_verify", pytest.fail)

    result = _resume(base, path, thread, dry_run=True, allow_verify=True)
    assert (result.outcome, result.version) == ("would-verify", "9.9.9")


def test_an_unverified_version_off_a_terminal_is_refused(offline, monkeypatch):
    base, path, thread = offline
    monkeypatch.setattr(module, "ProbeRunner", UnverifiedClaudeRunner)
    monkeypatch.setattr(workflow, "self_verify", pytest.fail)

    result = _resume(base, path, thread, allow_verify=False)
    assert result.outcome == "refused"
    assert "needs self-verification" in str(result.reason)


def test_a_passing_self_verification_is_recorded_and_the_conversion_continues(offline, monkeypatch):
    base, path, thread = offline
    _claude_on_path(base, monkeypatch)
    monkeypatch.setattr(module, "ProbeRunner", UnverifiedClaudeRunner)
    calls = []

    def verify(agent, harness, executable, *, workdir):
        calls.append((agent, Path(executable).name))
        return VerificationRecord(
            agent,
            "9.9.9",
            HARNESS_VERSION[agent],
            "model-request-capture",
            "2026-10-08T00:00:00",
            inputs=harness_inputs(),
        )

    monkeypatch.setattr(workflow, "self_verify", verify)

    result = _resume(base, path, thread, allow_verify=True)
    assert result.outcome == "converted"
    assert calls == [("claude-code", "claude")]
    assert [item.version for item in load_verified(base / "verified.json")] == ["9.9.9"]
    assert progress_lines == [
        f"verifying claude with {base / 'bin' / 'claude'} (offline, about a minute)...",
        f"✓ claude 9.9.9: verified (model-request-capture); recorded in {base / 'verified.json'}",
    ]


def test_a_failing_self_verification_is_a_refusal_with_its_reason(offline, monkeypatch):
    base, path, thread = offline
    _claude_on_path(base, monkeypatch)
    monkeypatch.setattr(module, "ProbeRunner", UnverifiedClaudeRunner)

    def verify(agent, harness, executable, *, workdir):
        raise PreservationError("self-verification failed: capture lacked the image")

    monkeypatch.setattr(workflow, "self_verify", verify)

    result = _resume(base, path, thread, allow_verify=True)
    assert (result.outcome, result.reason) == (
        "refused",
        "self-verification failed: capture lacked the image",
    )
    assert not (base / "verified.json").exists()
    assert not (base / "operations").exists()


def test_a_failure_after_publication_began_names_the_transaction(offline, monkeypatch):
    base, path, thread = offline
    original = module._service

    def crashing(paths):
        worker = original(paths)

        def fault(name, operation):
            if name == "after:file:0:action":
                raise OSError("disk went away")

        return ConversionService(
            worker.adapters,
            worker.runner,
            registry_path=paths.registry,
            operations_path=paths.operations,
            fault=fault,
        )

    monkeypatch.setattr(module, "_service", crashing)

    with pytest.raises(ConversionFailure) as caught:
        _resume(base, path, thread)
    (operation_id,) = (item.name for item in (base / "operations").iterdir() if item.is_dir())
    message = str(caught.value)
    assert message.startswith("native conversion to claude failed: disk went away; ")
    assert f"rl data transaction resume {operation_id}" in message
    assert f"rl data transaction rollback {operation_id}" in message
    assert f"--verified {base / 'verified.json'}" in message


def test_a_dry_run_that_cannot_check_the_priors_lifecycle_names_the_operation(offline):
    base, path, thread = offline
    first = _resume(base, path, thread)
    assert first.target is not None
    created = registry.find_session(registry.load_registry(path), first.target.native_id)
    native_file = Path(created["file"])
    native_file.unlink()
    native_file.symlink_to(base / "elsewhere")

    with pytest.raises(ConversionFailure) as caught:
        _resume(base, path, thread, dry_run=True)

    (operation_id,) = (item.name for item in (base / "operations").iterdir() if item.is_dir())
    message = str(caught.value)
    assert message.startswith("native conversion to claude failed: ")
    assert f"rl data transaction resume {operation_id}" in message
    assert f"rl data transaction rollback {operation_id}" in message


def test_a_probe_timeout_during_prepare_fails_without_a_markdown_fallback(
    offline, monkeypatch, capsys
):
    base, path, thread = offline
    monkeypatch.setattr(module, "ProbeRunner", TimingOutRunner)
    monkeypatch.setattr(resume_cmd, "_interactive", lambda: False)
    argv = ["--to", "claude", "--registry", str(path)]
    argv += ["--operations", str(base / "operations"), "--verified", str(base / "verified.json")]

    rc = resume_cmd.cmd_resume(
        [thread["sessionId"], *argv],
        runner=lambda argv, cwd: pytest.fail("the handoff runner ran"),
        picker=lambda *args: pytest.fail("the picker opened"),
        which=lambda binary: f"/opt/bin/{binary}",
        refresher=lambda *args: pytest.fail("the registry was rescanned"),
        presence=frozenset,
        launch=lambda request: pytest.fail("something was launched"),
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("rl-resume: native conversion to claude failed: ")
    assert err.endswith("; nothing was published\n")
    assert not (base / "operations").exists()


def test_rl_resume_converts_a_pi_session_into_claude_and_execs_it(offline, monkeypatch):
    base, path, thread = offline
    monkeypatch.setattr(resume_cmd, "_interactive", lambda: False)
    launched = []
    argv = ["--to", "claude", "--registry", str(path)]
    argv += ["--operations", str(base / "operations"), "--verified", str(base / "verified.json")]

    rc = resume_cmd.cmd_resume(
        [thread["sessionId"], *argv],
        runner=lambda argv, cwd: pytest.fail("the handoff runner ran"),
        picker=lambda *args: pytest.fail("the picker opened"),
        which=lambda binary: f"/opt/bin/{binary}",
        refresher=lambda *args: pytest.fail("the registry was rescanned"),
        presence=frozenset,
        launch=launched.append,
    )
    assert rc == 0
    (request,) = launched
    target = registry.find_session(registry.load_registry(path), request.argv[2])
    assert request.argv == ("claude", "--resume", target["sessionId"], "--model", "default")
    assert request.cwd == Path(thread["project"])
    assert request.env == ()
    assert target["source"] == "claude-code"
    assert Path(target["file"]).is_relative_to(base / "home" / ".claude" / "projects")
    assert _selected(path, thread).record == thread


def test_rl_resume_dry_run_prints_the_native_line_for_a_pi_session(offline, capsys):
    base, path, thread = offline
    before = tree(base)
    argv = ["--to", "claude", "--dry-run", "--registry", str(path)]
    argv += ["--operations", str(base / "operations"), "--verified", str(base / "verified.json")]

    assert resume_cmd.cmd_resume([thread["sessionId"], *argv], which=lambda binary: None) == 0
    out = capsys.readouterr().out
    assert "  native: would convert to claude (new)\n" in out
    assert "  note: claude is not installed" in out
    assert tree(base) == before


def test_forking_an_archived_target_probes_and_reads_back_the_target_once(offline, monkeypatch):
    base, path, thread = offline
    first = _resume(base, path, thread)
    assert first.target is not None
    _archive(path, first.target.native_id, base)
    probes: list[tuple[str, ...]] = []
    reads: list[object] = []

    class CountingRunner(OfflineRunner):
        def run(self, request):
            probes.append(request.argv)
            return super().run(request)

    read_stage = ClaudeAdapter.read_stage

    def counting_read(self, stage):
        reads.append(stage.identity)
        return read_stage(self, stage)

    monkeypatch.setattr(module, "ProbeRunner", CountingRunner)
    monkeypatch.setattr(ClaudeAdapter, "read_stage", counting_read)

    forked = _resume(base, path, thread)
    assert forked.outcome == "forked"
    assert probes == [("claude", "--version")]
    assert reads == [forked.target]


@pytest.mark.parametrize("passes", [True, False])
def test_doctor_and_resume_self_verify_through_one_workflow(offline, monkeypatch, capsys, passes):
    base, path, thread = offline
    _claude_on_path(base, monkeypatch)
    monkeypatch.setattr(module, "ProbeRunner", UnverifiedClaudeRunner)

    def harness(executable, workdir):
        (workdir / "capture.log").write_text("evidence")
        if not passes:
            raise PreservationError("self-verification failed: capture lacked the image")
        return HarnessResult("9.9.9", "PASS")

    monkeypatch.setattr(claude_harness, "run", harness)
    from_doctor = base / "doctor" / "verified.json"
    from_doctor.parent.mkdir()

    doctor.verify_conversion("claude", from_doctor)
    doctor_lines = capsys.readouterr().out.splitlines()
    resumed = _resume(base, path, thread, allow_verify=True)
    from_resume = base / "verified.json"

    def masked(lines, state):
        evidence = re.escape(str(state.parent / "verify-failures")) + r"/\S+\)"
        return [
            re.sub(evidence, "<evidence>)", line).replace(str(state), "<state>") for line in lines
        ]

    assert masked(doctor_lines, from_doctor) == masked(progress_lines, from_resume)
    assert [
        item.name.split("-")[0] for item in (from_doctor.parent / "verify-failures").glob("*")
    ] == ([] if passes else ["claude"])
    assert [
        item.name.split("-")[0] for item in (from_resume.parent / "verify-failures").glob("*")
    ] == ([] if passes else ["claude"])
    if passes:
        assert resumed.outcome == "converted"
        assert [
            {**record.__dict__, "verified_at": None} for record in load_verified(from_doctor)
        ] == [{**record.__dict__, "verified_at": None} for record in load_verified(from_resume)]
    else:
        assert resumed.outcome == "refused"
