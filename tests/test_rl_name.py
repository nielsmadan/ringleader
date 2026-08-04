"""Integration tests for the `rl session name` CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

# A mtime 30 days in the past so threads are never considered "active".
OLD_MTIME = "2026-05-01T00:00:00+00:00"
SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _make_thread(
    session_id: str,
    file_path: str,
    suggested: str = "demo-name",
    title=None,
    applied=None,
    status: str = "unfinished",
) -> dict:
    """Build a minimal thread dict matching the shape from registry._new_thread."""
    return {
        "sessionId": session_id,
        "file": file_path,
        "project": "test-project",
        "title": title,
        "aiTitle": None,
        "summary": None,
        "lastPrompt": "hello",
        "mtime": OLD_MTIME,
        "sizeBytes": 0,
        "messageCount": 1,
        "gitBranch": "main",
        "gitProjectKey": None,
        "gitProjectLabel": None,
        "gitRoot": None,
        "gitRemote": None,
        "status": status,
        "statusConfidence": 0.9,
        "whatItIs": "a test session",
        "suggestedName": suggested,
        "appliedName": applied,
        "namedAtMtime": None,
        "classifiedAtMtime": OLD_MTIME,
        "decision": None,
        "harvestPath": None,
        "todoId": None,
        "tags": [],
    }


def _make_registry(threads: list[dict]) -> dict:
    return {
        "version": 1,
        "lastScan": OLD_MTIME,
        "threads": threads,
        "tombstones": [],
    }


def _run(*extra_args, registry_path: str, check: bool = False, env: dict | None = None):
    """Run rl session name with the given registry and extra args."""
    cmd = [PYTHON, "-m", "ringleader", "session", "name", "--registry", registry_path, *extra_args]
    process_env = os.environ.copy()
    process_env.update(env or {})
    return subprocess.run(
        cmd, cwd=str(REPO), capture_output=True, text=True, check=check, env=process_env
    )


# ---------------------------------------------------------------------------
# Test 1: dry-run writes nothing
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text('{"type":"say","text":"hello"}\n', encoding="utf-8")
    original_bytes = transcript.read_bytes()

    reg_file = tmp_path / "registry.json"
    thread = _make_thread(SESSION_ID, str(transcript))
    reg_file.write_text(json.dumps(_make_registry([thread])), encoding="utf-8")
    original_reg = reg_file.read_bytes()

    result = _run("--dry-run", registry_path=str(reg_file))

    assert result.returncode == 0, result.stderr
    # dry-run should mention the name
    assert "new:" in result.stdout
    assert "demo-name" in result.stdout
    # transcript byte-identical
    assert transcript.read_bytes() == original_bytes
    # registry byte-identical
    assert reg_file.read_bytes() == original_reg


# ---------------------------------------------------------------------------
# Test 2: apply appends title record + updates registry
# ---------------------------------------------------------------------------


def test_apply_appends_and_updates_registry(tmp_path):
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text('{"type":"say","text":"hello"}\n', encoding="utf-8")

    reg_file = tmp_path / "registry.json"
    thread = _make_thread(SESSION_ID, str(transcript))
    reg_file.write_text(json.dumps(_make_registry([thread])), encoding="utf-8")

    result = _run(registry_path=str(reg_file))

    assert result.returncode == 0, result.stderr

    # Last line of transcript must be the custom-title JSON
    lines = transcript.read_text(encoding="utf-8").strip().splitlines()
    last = json.loads(lines[-1])
    assert last["type"] == "custom-title"
    assert last["customTitle"] == "demo-name"
    assert last["sessionId"] == SESSION_ID

    # Registry must have title == appliedName == suggestedName
    updated_reg = json.loads(reg_file.read_text(encoding="utf-8"))
    t = updated_reg["threads"][0]
    assert t["title"] == "demo-name"
    assert t["appliedName"] == "demo-name"
    assert t["suggestedName"] == "demo-name"

    # Summary output
    assert "named 1" in result.stdout
    assert "renamed 0" in result.stdout


# ---------------------------------------------------------------------------
# Test 3: idempotent second run
# ---------------------------------------------------------------------------


def test_idempotent_second_run(tmp_path):
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text('{"type":"say","text":"hello"}\n', encoding="utf-8")

    reg_file = tmp_path / "registry.json"
    thread = _make_thread(SESSION_ID, str(transcript))
    reg_file.write_text(json.dumps(_make_registry([thread])), encoding="utf-8")

    # First apply
    r1 = _run(registry_path=str(reg_file))
    assert r1.returncode == 0, r1.stderr

    transcript_after_first = transcript.read_bytes()

    # Second apply
    r2 = _run(registry_path=str(reg_file))
    assert r2.returncode == 0, r2.stderr

    # No extra lines appended
    assert transcript.read_bytes() == transcript_after_first

    # Summary shows zero work
    assert "named 0" in r2.stdout
    assert "renamed 0" in r2.stdout


# ---------------------------------------------------------------------------
# Test 4: --limit 0 selects nothing
# ---------------------------------------------------------------------------


def test_limit_zero_selects_nothing(tmp_path):
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text('{"type":"say","text":"hello"}\n', encoding="utf-8")
    original_bytes = transcript.read_bytes()

    reg_file = tmp_path / "registry.json"
    thread = _make_thread(SESSION_ID, str(transcript))
    reg_file.write_text(json.dumps(_make_registry([thread])), encoding="utf-8")

    result = _run("--limit", "0", registry_path=str(reg_file))

    assert result.returncode == 0, result.stderr
    assert transcript.read_bytes() == original_bytes
    # Registry may be saved but with no changes applied; thread still has title=None
    updated_reg = json.loads(reg_file.read_text(encoding="utf-8"))
    assert updated_reg["threads"][0]["title"] is None


# ---------------------------------------------------------------------------
# Test 5: trailing-newline handling — file NOT ending in \n
# ---------------------------------------------------------------------------


def test_trailing_newline_handling(tmp_path):
    transcript = tmp_path / "sess.jsonl"
    # Deliberately omit trailing newline
    transcript.write_bytes(b'{"type":"say","text":"hello"}')

    reg_file = tmp_path / "registry.json"
    thread = _make_thread(SESSION_ID, str(transcript))
    reg_file.write_text(json.dumps(_make_registry([thread])), encoding="utf-8")

    result = _run(registry_path=str(reg_file))

    assert result.returncode == 0, result.stderr

    # Every line must be valid JSON
    content = transcript.read_text(encoding="utf-8")
    lines = [ln for ln in content.splitlines() if ln.strip()]
    parsed = [json.loads(ln) for ln in lines]  # will raise if any line is invalid

    # Last line must be the title record
    assert parsed[-1]["type"] == "custom-title"
    assert parsed[-1]["customTitle"] == "demo-name"
    assert parsed[-1]["sessionId"] == SESSION_ID


# ---------------------------------------------------------------------------
# Codex writes names to its native session index
# ---------------------------------------------------------------------------


def test_codex_thread_uses_native_session_index(tmp_path):
    claude_transcript = tmp_path / "claude.jsonl"
    claude_transcript.write_text('{"type":"say","text":"hello"}\n', encoding="utf-8")
    claude_thread = _make_thread(
        "aaaaaaaa-0000-0000-0000-000000000001",
        str(claude_transcript),
        suggested="claude-name",
    )

    codex_home = tmp_path / "codex-home"
    codex_sessions = codex_home / "sessions"
    codex_sessions.mkdir(parents=True)
    codex_transcript = codex_sessions / "codex.jsonl"
    codex_thread = _make_thread(
        "bbbbbbbb-0000-0000-0000-000000000002",
        str(codex_transcript),
        suggested="codex-name",
    )
    codex_thread["source"] = "codex"
    codex_transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"session_id": codex_thread["sessionId"], "cwd": "/test"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    reg_file = tmp_path / "registry.json"
    reg_file.write_text(json.dumps(_make_registry([claude_thread, codex_thread])), encoding="utf-8")
    original_codex_bytes = codex_transcript.read_bytes()

    result = _run(registry_path=str(reg_file), env={"CODEX_SESSIONS_DIR": str(codex_sessions)})

    assert result.returncode == 0, result.stderr

    # Claude thread got the title record appended.
    lines = claude_transcript.read_text(encoding="utf-8").strip().splitlines()
    last = json.loads(lines[-1])
    assert last["type"] == "custom-title"
    assert last["customTitle"] == "claude-name"

    # Codex names live in its session index, not in the rollout transcript.
    assert codex_transcript.read_bytes() == original_codex_bytes
    index_entries = [
        json.loads(line)
        for line in (codex_home / "session_index.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert index_entries[-1]["id"] == codex_thread["sessionId"]
    assert index_entries[-1]["thread_name"] == "codex-name"

    updated_reg = json.loads(reg_file.read_text(encoding="utf-8"))
    by_id = {t["sessionId"]: t for t in updated_reg["threads"]}
    assert by_id[claude_thread["sessionId"]]["appliedName"] == "claude-name"
    assert by_id[codex_thread["sessionId"]]["appliedName"] == "codex-name"

    # Both sources support native name writeback.
    assert "named 2" in result.stdout
    assert "(0 no-name-support)" in result.stdout


def test_missing_file_skipped_not_fatal(tmp_path):
    # Thread pointing at a non-existent file
    missing_path = str(tmp_path / "does_not_exist.jsonl")
    bad_thread = _make_thread(
        "11111111-2222-3333-4444-555555555555",
        missing_path,
        suggested="bad-session",
    )

    # A second thread with a real file that should still be processed
    good_transcript = tmp_path / "good.jsonl"
    good_transcript.write_text('{"type":"say","text":"hi"}\n', encoding="utf-8")
    good_thread = _make_thread(SESSION_ID, str(good_transcript), suggested="good-session")

    reg_file = tmp_path / "registry.json"
    reg_file.write_text(json.dumps(_make_registry([bad_thread, good_thread])), encoding="utf-8")

    result = _run(registry_path=str(reg_file))

    assert result.returncode == 0, result.stderr
    # Warning about the skipped session goes to stderr
    assert "skipped" in result.stderr
    # Good transcript was still processed
    lines = good_transcript.read_text(encoding="utf-8").strip().splitlines()
    last = json.loads(lines[-1])
    assert last["type"] == "custom-title"
    assert last["customTitle"] == "good-session"
