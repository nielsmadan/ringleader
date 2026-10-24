from __future__ import annotations

import ast
import importlib.util
import json
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from ringleader.commands import doctor
from ringleader.portability import verification
from ringleader.portability.adapters import builtin_adapters
from ringleader.portability.capabilities import (
    Compatibility,
    NeedsVerification,
    RunResult,
    TargetRequest,
)
from ringleader.portability.claude import ClaudeAdapter
from ringleader.portability.codex import PROFILE as CODEX_PROFILE
from ringleader.portability.codex import CodexAdapter
from ringleader.portability.codex_state import SCHEMA_ID as CODEX_SCHEMA
from ringleader.portability.model import Identity, PreservationError
from ringleader.portability.opencode import OpenCodeAdapter
from ringleader.portability.operations import decode, digest, encode
from ringleader.portability.pi import PROFILE as PI_PROFILE
from ringleader.portability.pi import PiAdapter
from ringleader.portability.serialization import encode_value
from ringleader.portability.service import ConversionService
from ringleader.portability.verification import (
    HARNESS_VERSION,
    VerificationRecord,
    harness_inputs,
    load_verified,
    record_verified,
)
from ringleader.portability.verify import workflow
from ringleader.portability.verify.common import HarnessResult
from ringleader.portability.verify.workflow import self_verify
from ringleader.portability.versions import check_version, parse_version
from ringleader.sources.pi import PiSource
from tests.portability.fixtures import conversation
from tests.portability.test_codex import initialized
from tests.portability.test_native_adapters import roots
from tests.portability.test_service import request, service, source


def record(agent, version, *, harness=None, schema=None, inputs=None):
    return VerificationRecord(
        agent,
        version,
        HARNESS_VERSION[agent] if harness is None else harness,
        "cli-acceptance" if agent == "opencode" else "model-request-capture",
        "2026-10-08T00:00:00+00:00",
        schema,
        harness_inputs() if inputs is None else inputs,
    )


class VersionRunner:
    def __init__(self, output):
        self.output = output

    def run(self, request):
        assert request.argv[-1] == "--version"
        return RunResult(0, self.output.encode(), b"")


@pytest.fixture
def sweep_module():
    spec = importlib.util.spec_from_file_location("conversion_sweep", "scripts/conversion-sweep.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules[spec.name]


def test_parse_version_reads_dotted_numbers():
    assert parse_version("2.1.278") == (2, 1, 278)
    assert parse_version(" v0.85.1\n") == (0, 85, 1)
    assert parse_version("2.1.278 (Claude Code)") is None


def test_check_version_accepts_pinned_without_warning():
    verdict = check_version("2.1.283", ("2.1.278", "2.1.283"), ())
    assert (verdict.accepted, verdict.basis, verdict.message) == (
        True,
        "2.1.283",
        None,
    )


def test_check_version_accepts_local_with_a_message():
    verdict = check_version("2.1.293", ("2.1.278",), ("2.1.293",))
    assert (verdict.accepted, verdict.basis, verdict.needs_verification) == (
        True,
        "2.1.293",
        False,
    )
    assert verdict.message == "2.1.293 accepted: self-verified on this machine, not repo-pinned"


def test_check_version_unknown_needs_verification():
    verdict = check_version("2.1.300", ("2.1.278",), ("2.1.293",))
    assert (verdict.accepted, verdict.needs_verification) == (False, True)
    assert verdict.message == (
        "2.1.300 is not a verified version (verified: 2.1.278, 2.1.293); it needs self-verification"
    )


def test_check_version_unparseable_cannot_be_verified():
    verdict = check_version("command not found", ("2.1.278",), ())
    assert (verdict.accepted, verdict.needs_verification) == (False, False)
    assert verdict.message == "unrecognised version 'command not found'"


def test_verification_records_round_trip_and_merge(tmp_path):
    path = tmp_path / "verified-targets.json"
    first = record("codex", "0.161.0", schema="codex-state-5:new")
    record_verified(path, first)
    record_verified(path, record("pi", "1.0.1"))
    replaced = replace(first, verified_at="2026-10-09T00:00:00+00:00")
    record_verified(path, replaced)
    assert load_verified(path) == (replaced, record("pi", "1.0.1"))


def test_stale_harness_records_are_ignored(tmp_path):
    path = tmp_path / "verified-targets.json"
    record_verified(path, record("pi", "1.0.1", harness=HARNESS_VERSION["pi"] - 1))
    record_verified(path, record("claude-code", "2.1.293"))
    assert load_verified(path) == (record("claude-code", "2.1.293"),)


def test_records_made_by_other_conversion_code_are_ignored(tmp_path):
    path = tmp_path / "verified-targets.json"
    record_verified(path, record("pi", "1.0.1", inputs="digest of an earlier writer"))
    record_verified(path, record("claude-code", "2.1.293"))
    assert load_verified(path) == (record("claude-code", "2.1.293"),)


def test_missing_state_loads_empty_and_corrupt_state_refuses(tmp_path):
    path = tmp_path / "verified-targets.json"
    assert load_verified(path) == ()
    path.write_text('{"version": 1, "records": [{"agent": "pi"}]}')
    with pytest.raises(PreservationError, match="Invalid self-verification record"):
        load_verified(path)


def test_compatibility_without_new_fieldsdecode_values():
    current = Compatibility(False, None, ("old",), "codex-cli 0.155.1", "schema")
    stored = encode_value(current)
    del stored["warnings"], stored["needs_verification"]
    assert decode(json.dumps(stored).encode(), Compatibility) == current


def test_stored_operation_from_before_the_new_fields_is_reused(tmp_path):
    worker = service(tmp_path)
    conversion = request(tmp_path)
    first = worker.convert(conversion)
    operation = worker.transactions()[0]
    path = worker.store.directory(operation.id) / "artifacts.json"
    artifacts = json.loads(path.read_bytes())
    del artifacts["compatibility"]["warnings"], artifacts["compatibility"]["needs_verification"]
    old = encode(artifacts)
    path.write_bytes(old)
    worker.store.save(replace(operation, artifacts_digest=digest(old)))
    assert worker.store.artifacts(worker.store.load(operation.id)).compatibility.warnings == ()
    again = worker.convert(conversion)
    assert (again.reused, again.target) == (True, first.target)


@pytest.mark.parametrize(
    ("adapter", "pinned", "unknown", "agent"),
    [
        (ClaudeAdapter, "2.1.283 (Claude Code)", "2.1.299 (Claude Code)", "claude-code"),
        (PiAdapter, "0.87.1", "1.4.0", "pi"),
    ],
)
def test_file_writers_gate_versions(tmp_path, adapter, pinned, unknown, agent):
    target = roots(tmp_path)
    assert adapter().inspect_target(target, VersionRunner(pinned)).supported
    refused = adapter().inspect_target(target, VersionRunner(unknown))
    assert (refused.supported, refused.needs_verification) == (False, True)
    with pytest.raises(NeedsVerification, match="needs self-verification"):
        refused.require()
    version = unknown.split()[0]
    local = adapter(verified=(record(agent, version),)).inspect_target(
        target, VersionRunner(unknown)
    )
    assert local.supported
    label = "Claude" if agent == "claude-code" else "Pi"
    assert local.warnings == (
        f"{label} writer {version} accepted: self-verified on this machine, not repo-pinned",
    )


def test_codex_gate_pairs_versions_with_schemas(tmp_path):
    target = initialized(tmp_path)
    assert CodexAdapter().inspect_target(target, VersionRunner("codex-cli 0.155.1")).supported
    mismatched = CodexAdapter().inspect_target(target, VersionRunner("codex-cli 0.157.0"))
    assert mismatched.supported is False
    assert mismatched.diagnostics == (
        f"Unsupported Codex SQLite schema/migrations: {CODEX_SCHEMA}",
    )
    unknown = CodexAdapter().inspect_target(target, VersionRunner("codex-cli 0.161.0"))
    assert (unknown.supported, unknown.needs_verification) == (False, True)
    local = CodexAdapter(verified=(record("codex", "0.161.0", schema=CODEX_SCHEMA),))
    accepted = local.inspect_target(target, VersionRunner("codex-cli 0.161.0"))
    assert (accepted.supported, accepted.schema_identity) == (True, CODEX_SCHEMA)


def test_codex_writer_records_the_installed_version(tmp_path):
    target = initialized(tmp_path)
    original = conversation()
    staged = CodexAdapter().stage(
        original,
        TargetRequest(
            Identity("codex", str(uuid4())),
            target,
            CODEX_PROFILE,
            original.title,
            runtime_version="codex-cli 0.157.0",
        ),
    )
    meta = json.loads(staged.files[0].data.splitlines()[0])["payload"]
    assert meta["cli_version"] == "0.157.0"
    assert json.loads(staged.database_payload)["cli_version"] == "0.157.0"


@pytest.fixture
def opencode_target(tmp_path):
    from tests.portability.test_opencode import roots as opencode_roots

    return opencode_roots.__wrapped__(tmp_path)


def test_opencode_unknown_schema_needs_verification(opencode_target):
    with closing(sqlite3.connect(opencode_target.store / "opencode.db")) as connection, connection:
        connection.execute("INSERT INTO migration VALUES ('29991231000000_later_release',0)")
    result = OpenCodeAdapter().inspect_target(opencode_target, None)
    assert (result.supported, result.needs_verification) == (False, True)
    from ringleader.portability.opencode_state import inspect_database

    schema = inspect_database(opencode_target, None)
    local = OpenCodeAdapter(verified=(record("opencode", "1.18.40", schema=schema),))
    accepted = local.inspect_target(opencode_target, None)
    assert (accepted.supported, accepted.schema_identity) == (True, schema)
    assert accepted.warnings == (
        f"OpenCode writer schema {schema} accepted: self-verified on this machine",
    )
    assert accepted.diagnostics == ()


def test_opencode_needs_verification_is_scoped_to_the_installed_version(opencode_target):
    with closing(sqlite3.connect(opencode_target.store / "opencode.db")) as connection, connection:
        connection.execute("INSERT INTO migration VALUES ('29991231000000_later_release',0)")
    from ringleader.portability.opencode_state import inspect_database

    mismatched_schema = inspect_database(opencode_target, None)
    local = OpenCodeAdapter(
        verified=(record("opencode", "1.18.40", schema="opencode-1.18.40:other"),)
    )
    # An untried version could still be fixed by re-running self-verification: hint it.
    untried = local.inspect_target(opencode_target, VersionRunner("1.18.50"))
    assert (untried.supported, untried.needs_verification) == (False, True)
    assert untried.diagnostics == (
        (
            f"OpenCode writer schema {mismatched_schema} is not verified; it needs "
            "self-verification; run rl doctor --verify-conversion opencode"
        ),
    )
    # The installed version was already self-verified and produced a different schema:
    # re-verifying it again would not change anything, so don't loop on the hint.
    retried = local.inspect_target(opencode_target, VersionRunner("1.18.40"))
    assert (retried.supported, retried.needs_verification) == (False, False)
    assert retried.diagnostics == (f"OpenCode writer schema {mismatched_schema} is not supported",)


def test_dry_run_surfaces_local_verification_warning(tmp_path):
    adapters = builtin_adapters((record("pi", "0.85.1"),))
    result = service(tmp_path, adapters=adapters).convert(request(tmp_path), dry_run=True)
    assert result.diagnostics == ()
    local = builtin_adapters((record("pi", "1.0.1"),))

    class Runner:
        def run(self, request):
            return RunResult(0, b"1.0.1", b"")

    worker = ConversionService(
        local,
        Runner(),
        operations_path=tmp_path / "state2" / "conversion",
        registry_path=tmp_path / "state2" / "registry.json",
    )
    result = worker.convert(request(tmp_path / "second"), dry_run=True)
    assert result.diagnostics == (
        "Pi writer 1.0.1 accepted: self-verified on this machine, not repo-pinned",
    )


def test_self_verify_builds_a_record_and_wraps_harness_crashes(tmp_path):
    made = self_verify(
        "pi", lambda executable, workdir: HarnessResult("1.0.1", "PASS"), "pi", workdir=tmp_path
    )
    assert (made.agent, made.version, made.harness_version, made.tier, made.inputs) == (
        "pi",
        "1.0.1",
        HARNESS_VERSION["pi"],
        "model-request-capture",
        harness_inputs(),
    )

    def crash(executable, workdir):
        raise KeyError("turns")

    with pytest.raises(PreservationError, match="self-verification failed: KeyError: 'turns'"):
        self_verify("pi", crash, "pi", workdir=tmp_path)


def test_doctor_verify_conversion_records_passes_and_reports_failures(
    tmp_path, monkeypatch, capsys
):
    state = tmp_path / "verified.json"
    monkeypatch.setattr(workflow.shutil, "which", lambda name: f"/bin/{name}")

    def fake(agent, harness, executable, *, workdir):
        if agent == "codex":
            raise PreservationError("self-verification failed: visible history")
        return record(agent, "9.9.9")

    monkeypatch.setattr(workflow, "self_verify", fake)
    assert doctor.cmd_doctor(["--verify-conversion", "--verified", str(state)]) == 1
    output = capsys.readouterr().out
    assert "✖ codex: self-verification failed: visible history" in output
    assert "✓ pi 9.9.9: verified (model-request-capture)" in output
    assert {item.agent for item in load_verified(state)} == {
        "claude-code",
        "pi",
        "opencode",
        "droid",
    }
    assert doctor.cmd_doctor(["--verify-conversion", "pi", "--verified", str(state)]) == 0


def test_doctor_preserves_evidence_on_self_verification_failure(tmp_path, monkeypatch, capsys):
    state = tmp_path / "verified.json"
    monkeypatch.setattr(workflow.shutil, "which", lambda name: f"/bin/{name}")

    def fake(agent, harness, executable, *, workdir):
        (workdir / "transcript.log").write_text("evidence contents")
        raise PreservationError("self-verification failed: boom")

    monkeypatch.setattr(workflow, "self_verify", fake)
    assert doctor.cmd_doctor(["--verify-conversion", "pi", "--verified", str(state)]) == 1
    output = capsys.readouterr().out
    assert "evidence kept at" in output
    [evidence_dir] = list((state.parent / "verify-failures").iterdir())
    assert evidence_dir.name.startswith("pi-")
    assert (evidence_dir / "transcript.log").read_text() == "evidence contents"


def test_doctor_evidence_keeps_links_as_links_and_only_the_newest_runs(tmp_path, monkeypatch):
    state = tmp_path / "verified.json"
    failures = tmp_path / "verify-failures"
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        (failures / f"pi-{stamp}").mkdir(parents=True)
    (failures / "codex-20260101T000000Z").mkdir()
    binary = tmp_path / "codex-binary"
    binary.write_bytes(b"large executable")
    monkeypatch.setattr(workflow.shutil, "which", lambda name: f"/bin/{name}")

    def fake(agent, harness, executable, *, workdir):
        (workdir / "apply_patch").symlink_to(binary)
        raise PreservationError("self-verification failed: boom")

    monkeypatch.setattr(workflow, "self_verify", fake)
    assert doctor.cmd_doctor(["--verify-conversion", "pi", "--verified", str(state)]) == 1
    names = sorted(item.name for item in failures.iterdir())
    assert names[0] == "codex-20260101T000000Z"
    assert names[1:3] == ["pi-20260102T000000Z", "pi-20260103T000000Z"]
    assert len(names) == 1 + workflow.EVIDENCE_KEPT
    assert (failures / names[-1] / "apply_patch").readlink() == binary


def test_doctor_verify_conversion_reports_corrupt_state_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    state = tmp_path / "verified.json"
    state.write_text('{"version": 1, "records": [{"agent": "pi"}]}')
    monkeypatch.setattr(workflow.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        workflow,
        "self_verify",
        lambda agent, harness, executable, *, workdir: record(agent, "9.9.9"),
    )
    assert doctor.cmd_doctor(["--verify-conversion", "pi", "--verified", str(state)]) == 1
    output = capsys.readouterr().out
    assert (
        f"✖ pi 9.9.9: verified (model-request-capture) but failed to record in {state}: "
        "Invalid self-verification record" in output
    )


def test_normalize_reason_groups_messages_differing_only_in_ids(sweep_module):
    first = (
        "Incomplete tool exchanges: toolu_01A2b3C4d5E6f7G8h9 in "
        "/Users/a/.claude/projects/x/019f9b5d-a83f-7f7f-aae9-b65fc6f1a65a.jsonl line 12"
    )
    second = (
        "Incomplete tool exchanges: call_9z8y7x6w5v4u3t2s1r in "
        "/tmp/other/1a2b3c4d-0000-4000-8000-000000000000.jsonl line 400"
    )
    assert sweep_module.normalize_reason(first) == sweep_module.normalize_reason(second)
    assert (
        sweep_module.normalize_reason(first) == "Incomplete tool exchanges: <id> in <path> line <n>"
    )
    assert sweep_module.normalize_reason("Unsupported role: 'system'") == "Unsupported role: <q>"


def test_normalize_reason_collapses_a_path_containing_a_space(sweep_module):
    message = "Attachment missing: ~/Library/Application Support/x/y"
    assert sweep_module.normalize_reason(message) == "Attachment missing: <path>"


def age(path):
    old = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    os.utime(path, (old, old))


def test_sweep_tallies_readable_and_refused_sessions(tmp_path, sweep_module, monkeypatch):
    sessions = tmp_path / "pi" / "sessions"
    monkeypatch.setenv("PI_SESSIONS_DIR", str(sessions))
    monkeypatch.delenv("SESSION_TRACKER_PROJECTS_DIR")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    target = replace(roots(tmp_path), store=tmp_path / "pi", cwd=tmp_path / "work")
    target.cwd.mkdir()
    readable = source(tmp_path / "readable")
    staged = PiAdapter().stage(
        PiAdapter().snapshot(readable).conversation,
        TargetRequest(Identity("pi", str(uuid4())), target, PI_PROFILE, "Readable"),
    )
    path = target.store / staged.source_ref
    path.parent.mkdir(parents=True)
    path.write_bytes(staged.files[0].data)
    header = json.loads(staged.files[0].data.splitlines()[0])
    refused = path.with_name(f"2026-01-01T00-00-00-000Z_{uuid4()}.jsonl")
    rows = [
        {**header, "id": refused.stem.split("_", 1)[1]},
        {
            "type": "message",
            "id": "00000001",
            "parentId": None,
            "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"role": "mystery", "content": "bootstrap"},
        },
    ]
    refused.write_text("".join(json.dumps(row) + "\n" for row in rows))
    age(path)
    age(refused)
    worker = ConversionService(
        builtin_adapters(),
        VersionRunner("1.4.0"),
        registry_path=tmp_path / "registry.json",
        operations_path=tmp_path / "operations",
    )
    pi_source = PiSource(sessions_dir=sessions)
    cutoff = datetime(2026, 10, 6, tzinfo=UTC)
    [reader] = sweep_module.sweep("pi", pi_source, worker, (), limit=None, cutoff=cutoff)
    assert (reader.target, reader.total, reader.ok) == (None, 2, 1)
    assert reader.categories == (("Unsupported Pi message role: mystery", 1),)
    [pair] = sweep_module.sweep(
        "pi", pi_source, worker, ("claude-code",), limit=None, cutoff=cutoff
    )
    assert (pair.target, pair.total, pair.ok) == ("claude-code", 2, 0)
    assert pair.categories == (
        ("Unsupported Pi message role: mystery", 1),
        ("target needs self-verification", 1),
    )
    assert not (tmp_path / "registry.json").exists()
    assert not (tmp_path / "operations").exists()


def test_sweep_sample_applies_cutoff_and_limit(sweep_module):
    from ringleader.sources.base import RawRef

    refs = [
        RawRef("pi", f"/s/{day}.jsonl", f"2026-10-{day:02d}T00:00:00+00:00", 1)
        for day in (1, 7, 3, 5)
    ]
    chosen = sweep_module.sample(refs, datetime(2026, 10, 6, tzinfo=UTC), 2)
    assert [ref.file for ref in chosen] == ["/s/5.jsonl", "/s/3.jsonl"]


def test_sweep_reports_turnless_rollouts_apart_from_the_bar(sweep_module):
    turnless = "Codex rollout has no conversation turns"
    pair = sweep_module.tally("codex", "pi", [None, None, "refused", turnless, turnless])
    assert (pair.total, pair.eligible, pair.ok) == (5, 3, 2)
    assert pair.categories == (("refused", 1),)
    assert pair.excluded == ((turnless, 2),)
    assert sweep_module.render(pair).splitlines() == [
        "codex → pi: convertible 2/3",
        "     1   33.3%  refused",
        f"     2  excluded, inherently empty: {turnless}",
    ]


def test_verification_records_never_import_the_harnesses_they_record():
    tree = ast.parse(Path(verification.__file__).read_text())
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    # the harnesses import these records, so importing them back is a cycle
    assert {module for module in imported if module.partition(".")[0] == "verify"} == set()
