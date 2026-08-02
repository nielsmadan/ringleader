import json
from pathlib import Path

from ringleader.sources.base import NormalizedSession, Source
from ringleader.sources.claude_code import ClaudeCodeSource


def test_parse_extracts_normalized_fields(tmp_path: Path):
    proj = tmp_path / "-Users-me-wrksp-proj"
    proj.mkdir()
    f = proj / "aaaaaaaa-0000-0000-0000-000000000000.jsonl"
    f.write_text(
        "\n".join(
            [
                json.dumps(
                    {"type": "user", "cwd": "/Users/me/wrksp/proj", "message": {"content": "hi"}}
                ),
                json.dumps({"type": "custom-title", "customTitle": "my-thread"}),
            ]
        )
    )
    src = ClaudeCodeSource(projects_dir=tmp_path)
    refs = list(src.discover())
    assert len(refs) == 1
    ns = src.parse(refs[0])
    assert ns.source == "claude-code"
    assert ns.session_id == "aaaaaaaa-0000-0000-0000-000000000000"
    assert ns.cwd == "/Users/me/wrksp/proj"
    assert ns.custom_title == "my-thread"


def test_source_satisfies_protocol():
    src = ClaudeCodeSource(projects_dir=Path("/does-not-exist"))
    assert isinstance(src, Source)
    assert src.name == "claude-code"


def test_discover_excludes_subagent_transcripts(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    proj.mkdir()
    (proj / "aaaaaaaa-0000-0000-0000-000000000001.jsonl").write_text("{}\n")
    sub = proj / "aaaaaaaa-0000-0000-0000-000000000001" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x.jsonl").write_text("{}\n")

    src = ClaudeCodeSource(projects_dir=tmp_path)
    refs = list(src.discover())
    assert len(refs) == 1
    assert refs[0].file.endswith("aaaaaaaa-0000-0000-0000-000000000001.jsonl")


def test_parse_counts_subagent_transcripts(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    proj.mkdir()
    sid = "bbbbbbbb-0000-0000-0000-000000000002"
    (proj / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": "/Users/me/proj", "message": {"content": "hi"}}) + "\n"
    )
    sub = proj / sid / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text("{}\n")
    (sub / "agent-b.jsonl").write_text("{}\n")

    src = ClaudeCodeSource(projects_dir=tmp_path)
    ns = src.parse(next(iter(src.discover())))
    assert isinstance(ns, NormalizedSession)
    assert ns.subagent_count == 2


def test_no_subagents_dir_counts_zero(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    proj.mkdir()
    sid = "cccccccc-0000-0000-0000-000000000003"
    (proj / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": "/Users/me/proj", "message": {"content": "hi"}}) + "\n"
    )

    src = ClaudeCodeSource(projects_dir=tmp_path)
    ns = src.parse(next(iter(src.discover())))
    assert ns.subagent_count == 0


def test_discover_missing_projects_dir_yields_nothing(tmp_path):
    src = ClaudeCodeSource(projects_dir=tmp_path / "does-not-exist")
    assert list(src.discover()) == []
