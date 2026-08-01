import json
from pathlib import Path
from unittest.mock import patch
from ringleader.scanner import scan, iter_session_files, SessionMeta


def _session(dir_: Path, sid: str, cwd: str, content="hello"):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{sid}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": cwd,
                    "message": {"role": "user", "content": content}}) + "\n",
        encoding="utf-8")


def test_iter_excludes_subagents(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    _session(proj, "aaaaaaaa-0000-0000-0000-000000000001", "/Users/me/proj")
    # a subagent transcript that must be ignored
    sub = proj / "aaaaaaaa-0000-0000-0000-000000000001" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x.jsonl").write_text("{}\n", encoding="utf-8")

    files = list(iter_session_files(tmp_path))
    assert len(files) == 1
    assert files[0].name == "aaaaaaaa-0000-0000-0000-000000000001.jsonl"


def test_scan_builds_session_meta(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    _session(proj, "bbbbbbbb-0000-0000-0000-000000000002", "/Users/me/proj", "do thing")
    metas = scan(tmp_path)
    assert len(metas) == 1
    m = metas[0]
    assert isinstance(m, SessionMeta)
    assert m.session_id == "bbbbbbbb-0000-0000-0000-000000000002"
    assert m.project == "/Users/me/proj"
    assert m.size_bytes > 0
    assert m.mtime.endswith("+00:00")  # tz-aware iso
    assert "do thing" in m.tail_text


def test_scan_missing_root_returns_empty(tmp_path):
    assert scan(tmp_path / "does-not-exist") == []


def test_scan_skips_invalid_utf8_file(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    proj.mkdir(parents=True, exist_ok=True)
    # valid session
    _session(proj, "dddddddd-0000-0000-0000-000000000001", "/Users/me/proj", "valid content")
    # file with invalid UTF-8 bytes — should NOT abort the scan
    bad = proj / "eeeeeeee-0000-0000-0000-000000000002.jsonl"
    bad.write_bytes(b'\xff\xfe not utf8\n')
    metas = scan(tmp_path)
    session_ids = [m.session_id for m in metas]
    assert "dddddddd-0000-0000-0000-000000000001" in session_ids
    assert len(metas) >= 1


def test_scan_skips_oserror_file(tmp_path):
    proj = tmp_path / "-Users-me-proj"
    proj.mkdir(parents=True, exist_ok=True)
    _session(proj, "ffffffff-0000-0000-0000-000000000001", "/Users/me/proj", "good session")
    _session(proj, "00000000-0000-0000-0000-000000000002", "/Users/me/proj", "bad session")

    bad_path = proj / "00000000-0000-0000-0000-000000000002.jsonl"

    original_parse = __import__("ringleader.transcript", fromlist=["parse_transcript"]).parse_transcript

    def patched_parse(path, **kwargs):
        if Path(path) == bad_path:
            raise OSError("simulated permission denied")
        return original_parse(path, **kwargs)

    with patch("ringleader.scanner.parse_transcript", side_effect=patched_parse):
        metas = scan(tmp_path)

    session_ids = [m.session_id for m in metas]
    assert "ffffffff-0000-0000-0000-000000000001" in session_ids
    assert "00000000-0000-0000-0000-000000000002" not in session_ids


def test_scan_attaches_injected_git_project(tmp_path):
    from ringleader.gitinfo import GitProject
    proj = tmp_path / "-Users-me-proj"
    _session(proj, "cccccccc-0000-0000-0000-000000000009", "/Users/me/proj")

    fake = lambda cwd: GitProject(root="/Users/me/proj",
                                  remote="github.com/me/proj",
                                  key="github.com/me/proj", label="proj")
    metas = scan(tmp_path, git_resolver=fake)
    assert metas[0].git_project_key == "github.com/me/proj"
    assert metas[0].git_project_label == "proj"
