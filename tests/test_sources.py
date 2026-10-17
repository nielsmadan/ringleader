import json
import os
from datetime import UTC, datetime
from pathlib import Path

from ringleader.sources.base import CompanionFileSource, NameApplyingSource, Source
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
    assert refs[0].source_ref == "aaaaaaaa-0000-0000-0000-000000000000"
    ns = src.parse(refs[0])
    assert ns.source == "claude-code"
    assert ns.session_id == "aaaaaaaa-0000-0000-0000-000000000000"
    assert ns.cwd == "/Users/me/wrksp/proj"
    assert ns.custom_title == "my-thread"


def test_source_satisfies_protocol():
    src = ClaudeCodeSource(projects_dir=Path("/does-not-exist"))
    assert isinstance(src, Source)
    assert isinstance(src, CompanionFileSource)
    assert src.name == "claude-code"
    assert "companion_files" in src.capabilities


def test_companion_paths_names_the_session_id_sidecar_directory():
    src = ClaudeCodeSource(projects_dir=Path("/projects"))

    assert src.companion_paths("/projects/-w-api/abc.jsonl") == (Path("/projects/-w-api/abc"),)


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


LAST_SCAN = datetime(2026, 7, 1, tzinfo=UTC)
BEFORE_LAST_SCAN = LAST_SCAN.timestamp() - 1


def _transcript(root: Path, cwd: str, sid: str, *, filed_under: str | None = None) -> Path:
    store = ClaudeCodeSource(projects_dir=root).storage_for(filed_under or cwd)
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{sid}.jsonl"
    path.write_text(json.dumps({"sessionId": sid, "cwd": cwd, "type": "user"}) + "\n")
    return path


def _scoped(root: Path, cwd: str, known: frozenset[str] = frozenset()) -> list[str]:
    refs = ClaudeCodeSource(projects_dir=root).discover_scoped(
        cwd, recursive=False, since=LAST_SCAN, known=known
    )
    return [r.source_ref for r in refs]


def test_scoped_discovery_skips_transcripts_unchanged_since_the_last_scan(tmp_path):
    stale = _transcript(tmp_path, "/Users/me/proj", "old")
    _transcript(tmp_path, "/Users/me/proj", "new")
    os.utime(stale, (BEFORE_LAST_SCAN, BEFORE_LAST_SCAN))

    assert _scoped(tmp_path, "/Users/me/proj") == ["new"]


def test_scoped_discovery_includes_a_known_transcript_filed_under_another_workspace(tmp_path):
    """A session that cd'd away is filed under the directory it was launched in."""
    drifted = _transcript(tmp_path, "/Users/me/proj/wt", "drift", filed_under="/Users/me/proj")

    known = frozenset({str(drifted)})
    assert _scoped(tmp_path, "/Users/me/proj/wt", known) == ["drift"]
