import json
from pathlib import Path

from ringleader.sources.base import NameApplyingSource, Source
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


def test_read_only_source_does_not_need_name_writeback():
    class ReadOnlySource:
        name = "read-only"
        capabilities = frozenset()

        def discover(self):
            return iter(())

        def parse(self, ref):
            raise NotImplementedError

    src = ReadOnlySource()
    assert isinstance(src, Source)
    assert not isinstance(src, NameApplyingSource)


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


def test_discover_missing_projects_dir_yields_nothing(tmp_path):
    src = ClaudeCodeSource(projects_dir=tmp_path / "does-not-exist")
    assert list(src.discover()) == []
