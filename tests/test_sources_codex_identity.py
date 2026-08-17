import json
from pathlib import Path

from ringleader.sources.codex import CodexSource

PARENT = "019fe283-6fd1-7000-0000-000000000001"
CHILD = "019fe283-705d-7000-0000-000000000002"


def _rollout(dir_: Path, *, own_id, root_id=None, source="cli", cwd="/Users/me/proj") -> Path:
    """A Codex rollout. `session_id` is the *root* thread's id, `id` is this
    file's own — they differ for every rollout a session spawns."""
    payload = {"id": own_id, "cwd": cwd, "timestamp": "2026-07-28T17:19:45.210Z"}
    if root_id is not None:
        payload["session_id"] = root_id
    if source is not None:
        payload["source"] = source
    lines = [
        {"type": "session_meta", "payload": payload},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        },
    ]
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / f"rollout-2026-07-28T19-19-45-{own_id}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def _ids(src, root: Path) -> list[str]:
    return [
        src.parse(ref).session_id for ref in src.discover() if Path(ref.file).is_relative_to(root)
    ]


def test_a_spawned_rollout_keeps_its_own_id_not_the_root_threads(tmp_path):
    day = tmp_path / "2026" / "07" / "28"
    child = _rollout(day, own_id=CHILD, root_id=PARENT, source="cli")

    src = CodexSource(sessions_dir=tmp_path)
    ref = next(r for r in src.discover() if r.file == str(child))

    assert src.parse(ref).session_id == CHILD


def test_subagent_rollouts_are_not_registered_as_sessions(tmp_path):
    day = tmp_path / "2026" / "07" / "28"
    _rollout(day, own_id=PARENT, root_id=PARENT, source="cli")
    _rollout(
        day,
        own_id=CHILD,
        root_id=PARENT,
        source={"subagent": {"thread_spawn": {"parent_thread_id": PARENT, "depth": 1}}},
    )

    assert _ids(CodexSource(sessions_dir=tmp_path), tmp_path) == [PARENT]


def test_non_interactive_exec_runs_are_not_registered_as_sessions(tmp_path):
    day = tmp_path / "2026" / "07" / "28"
    _rollout(day, own_id=PARENT, root_id=PARENT, source="cli")
    _rollout(day, own_id=CHILD, root_id=CHILD, source="exec")

    assert _ids(CodexSource(sessions_dir=tmp_path), tmp_path) == [PARENT]


def test_rollouts_predating_the_source_field_are_still_registered(tmp_path):
    # oldest format: has `id` and `cwd`, but no `session_id` and no `source`
    day = tmp_path / "2025" / "09" / "20"
    _rollout(day, own_id=PARENT, root_id=None, source=None)

    src = CodexSource(sessions_dir=tmp_path)
    refs = list(src.discover())

    assert len(refs) == 1
    parsed = src.parse(refs[0])
    assert parsed.session_id == PARENT
    assert parsed.cwd == "/Users/me/proj"


def test_a_malformed_rollout_is_skipped_rather_than_breaking_the_scan(tmp_path, capsys):
    day = tmp_path / "2026" / "07" / "28"
    (day).mkdir(parents=True)
    (day / "rollout-bad.jsonl").write_text("not json\n", encoding="utf-8")
    _rollout(day, own_id=PARENT, root_id=PARENT, source="cli")

    assert _ids(CodexSource(sessions_dir=tmp_path), tmp_path) == [PARENT]
    assert "rollout-bad.jsonl" in capsys.readouterr().err
