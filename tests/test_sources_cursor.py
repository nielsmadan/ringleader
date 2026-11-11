import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from ringleader import export, lifecycle, scanner
from ringleader.sources.base import (
    CachedMetadataSource,
    CompanionFileSource,
    SessionLaunchingSource,
    WorkspaceRelocatingSource,
)
from ringleader.sources.cursor import CursorSource

CWD = "/Users/me/proj"
SLUG = "Users-me-proj"


def _key(cwd):
    return hashlib.md5(cwd.encode()).hexdigest()


def _user(text):
    return {"role": "user", "message": {"content": [{"type": "text", "text": text}]}}


def _assistant(*blocks):
    return {"role": "assistant", "message": {"content": list(blocks)}}


def _chat(  # noqa: PLR0913 -- one synthetic chat's every knob
    root,
    chat_id="c1",
    *,
    cwd=CWD,
    title="Fix The Build",
    records=None,
    has_conversation=True,
    subagent=False,
    transcript_slug=None,
    data_root=None,
):
    chat = root / "chats" / _key(cwd) / chat_id
    chat.mkdir(parents=True)
    meta = {
        "schemaVersion": 1,
        "createdAtMs": 1794302103092,
        "hasConversation": has_conversation,
        "updatedAtMs": 1794354262678,
        "cwd": cwd,
    }
    if title:
        meta["title"] = title
    (chat / "meta.json").write_text(json.dumps(meta, separators=(",", ":")))
    if not has_conversation:
        return chat, None
    store_meta = {"agentId": chat_id, "name": title or "New Agent"}
    if subagent:
        store_meta["subagentInfo"] = {"parentAgentId": "p"}
    with closing(sqlite3.connect(chat / "store.db")) as db, db:
        db.execute("create table meta (key text primary key, value text)")
        db.execute("create table blobs (id text primary key, data blob)")
        db.execute("insert into meta values ('0', ?)", (json.dumps(store_meta).encode().hex(),))
    (chat / "prompt_history.json").write_text("[]")
    if records is None:
        records = [
            _user(
                "<timestamp>Tuesday, Nov 10, 2026</timestamp>\n<user_query>\nfix the build\n"
                "</user_query>"
            ),
            _assistant({"type": "tool_use", "name": "Shell", "input": {"command": "just"}}),
            _assistant({"type": "text", "text": "Fixed it."}),
            {"type": "turn_ended", "status": "success"},
        ]
    folder = (data_root or root) / "projects" / (transcript_slug or SLUG) / "agent-transcripts"
    folder = folder / chat_id
    folder.mkdir(parents=True)
    transcript = folder / f"{chat_id}.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
    return chat, transcript


def _source(root):
    return CursorSource(config_dir=root, data_dir=root)


def test_source_satisfies_the_protocols_it_declares(tmp_path):
    source = _source(tmp_path)
    assert isinstance(source, CompanionFileSource)
    assert isinstance(source, WorkspaceRelocatingSource)
    assert isinstance(source, SessionLaunchingSource)
    assert isinstance(source, CachedMetadataSource)
    assert source.capabilities == frozenset(
        {"companion_files", "filesystem_lifecycle", "relocates_workspace", "launches_sessions"}
    )


def test_the_default_roots_follow_cursors_own_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CURSOR_DATA_DIR", str(tmp_path / "data"))
    source = CursorSource()
    assert (source.chats_dir, source.projects_dir) == (
        tmp_path / "config" / "chats",
        tmp_path / "data" / "projects",
    )


def test_the_chats_root_falls_back_to_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.delenv("CURSOR_CONFIG_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert CursorSource().chats_dir == tmp_path / "xdg" / "cursor" / "chats"


def test_storage_encodings_match_cursors_own(tmp_path):
    source = _source(tmp_path)
    assert (
        source.storage_for("/var/folders/v_/x.y/T") == tmp_path / "projects" / "var-folders-v-x-y-T"
    )
    assert source.chats_for(CWD) == tmp_path / "chats" / _key(CWD)


def test_discover_yields_the_transcript_of_each_chat_with_a_conversation(tmp_path):
    _, transcript = _chat(tmp_path, "c1")
    _chat(tmp_path, "c2", has_conversation=False)
    refs = list(_source(tmp_path).discover())
    assert [(r.source, r.source_ref, r.file) for r in refs] == [("cursor", "c1", str(transcript))]


def test_discover_excludes_a_subagent_chat(tmp_path):
    _chat(tmp_path, "parent")
    _chat(tmp_path, "child", subagent=True)
    assert [r.source_ref for r in _source(tmp_path).discover()] == ["parent"]


def test_discover_waits_for_a_chat_whose_transcript_is_not_written_yet(tmp_path):
    _, transcript = _chat(tmp_path, "c1")
    transcript.unlink()
    assert list(_source(tmp_path).discover()) == []


def test_discover_finds_a_transcript_filed_under_another_directory(tmp_path):
    _, transcript = _chat(tmp_path, "c1", transcript_slug="Users-me")
    assert [r.file for r in _source(tmp_path).discover()] == [str(transcript)]


def test_discover_reports_and_skips_a_malformed_meta(tmp_path, capsys):
    chat, _ = _chat(tmp_path, "c1")
    (chat / "meta.json").write_text("{not json")
    _chat(tmp_path, "c2", cwd="/Users/me/other")
    assert [r.source_ref for r in _source(tmp_path).discover()] == ["c2"]
    assert "skipped" in capsys.readouterr().err


def test_parse_reads_the_title_and_cwd_from_meta_and_the_turns_from_the_transcript(tmp_path):
    _chat(tmp_path, "c1")
    source = _source(tmp_path)
    session = source.parse(next(source.discover()))
    assert (
        session.session_id,
        session.cwd,
        session.ai_title,
        session.custom_title,
        session.started_at,
        session.last_prompt,
        session.message_count,
        session.tail_text,
    ) == (
        "c1",
        CWD,
        "Fix The Build",
        None,
        "2026-11-10T09:15:03.092000+00:00",
        "fix the build",
        2,
        "USER: fix the build\nASSISTANT: Fixed it.",
    )


def test_parse_skips_a_truncated_transcript_line(tmp_path):
    _, transcript = _chat(tmp_path, "c1")
    with transcript.open("a") as fh:
        fh.write('{"role":"assistant","message":{"content":[{"type":"te')
    source = _source(tmp_path)
    assert source.parse(next(source.discover())).message_count == 2


def test_parse_keeps_a_prompt_without_the_query_wrapper_whole(tmp_path):
    _chat(tmp_path, "c1", records=[_user("plain words")])
    source = _source(tmp_path)
    assert source.parse(next(source.discover())).last_prompt == "plain words"


def test_a_cached_session_picks_up_a_title_set_later(tmp_path):
    chat, _ = _chat(tmp_path, "c1", title=None)
    source = _source(tmp_path)
    session = source.parse(next(source.discover()))
    meta = json.loads((chat / "meta.json").read_text())
    (chat / "meta.json").write_text(json.dumps({**meta, "title": "Named Later"}))
    source.refresh_cached_metadata(session)
    assert session.ai_title == "Named Later"


def test_companions_are_the_chat_dir_live_and_its_sibling_once_archived(tmp_path):
    chat, transcript = _chat(tmp_path, "c1")
    source = _source(tmp_path)
    archived = tmp_path / "archive" / "proj" / "c1.jsonl"
    assert source.companion_paths(str(transcript)) == (chat,)
    assert source.companion_paths(str(archived)) == (archived.parent / "c1",)


def test_a_restored_chat_dir_goes_back_under_the_md5_of_its_recorded_cwd(tmp_path):
    chat, transcript = _chat(tmp_path, "c1")
    archived = tmp_path / "archive" / "proj" / "c1"
    archived.parent.mkdir(parents=True)
    chat.rename(archived)
    assert _source(tmp_path).companion_destination(archived, str(transcript)) == chat


def test_storage_dir_is_the_slug_dir_above_the_transcript(tmp_path):
    _, transcript = _chat(tmp_path, "c1")
    assert _source(tmp_path).storage_dir(str(transcript)) == tmp_path / "projects" / SLUG


def test_relocate_storage_moves_both_stores_and_rewrites_the_recorded_cwd(tmp_path):
    chat, transcript = _chat(tmp_path, "c1")
    source = _source(tmp_path)
    moved = source.relocate_storage(CWD, "/Users/me/renamed")
    new_transcript = (
        tmp_path / "projects" / "Users-me-renamed" / "agent-transcripts" / "c1" / "c1.jsonl"
    )
    new_chat = tmp_path / "chats" / _key("/Users/me/renamed") / "c1"
    assert moved == {str(transcript): str(new_transcript)}
    assert new_transcript.is_file()
    assert json.loads((new_chat / "meta.json").read_text())["cwd"] == "/Users/me/renamed"
    assert (new_chat / "store.db").is_file()
    assert not chat.parent.exists()


def test_relocate_storage_raises_rather_than_merging_onto_an_occupied_destination(tmp_path):
    _chat(tmp_path, "c1")
    _chat(tmp_path, "c2", cwd="/Users/me/renamed")
    with pytest.raises(RuntimeError, match="already exists"):
        _source(tmp_path).relocate_storage(CWD, "/Users/me/renamed")


def test_relocate_storage_refuses_a_slug_dir_shared_with_another_directory(tmp_path):
    """`/Users/me/proj` and `/Users/me-proj` share Cursor's slug, so moving the slug dir
    would carry the other directory's transcripts along."""
    _chat(tmp_path, "c1")
    _chat(tmp_path, "c2", cwd="/Users/me-proj")
    with pytest.raises(RuntimeError, match="launched elsewhere"):
        _source(tmp_path).relocate_storage(CWD, "/Users/me/renamed")


def test_relocate_session_moves_one_chat_and_rewrites_its_cwd(tmp_path):
    _chat(tmp_path, "c1")
    _, other = _chat(tmp_path, "c2")
    source = _source(tmp_path)
    new = source.relocate_session(str(other), "/Users/me/renamed")
    new_chat = tmp_path / "chats" / _key("/Users/me/renamed") / "c2"
    assert new == str(
        tmp_path / "projects" / "Users-me-renamed" / "agent-transcripts" / "c2" / "c2.jsonl"
    )
    assert json.loads((new_chat / "meta.json").read_text())["cwd"] == "/Users/me/renamed"
    assert sorted(r.source_ref for r in source.discover()) == ["c1", "c2"]


def _thread(transcript):
    return {
        "sessionId": "c1",
        "source": "cursor",
        "file": str(transcript),
        "project": CWD,
    }


def test_archive_then_unarchive_moves_the_transcript_and_chat_both_ways(tmp_path):
    root = tmp_path / "_cursor"  # the store conftest points cursor at
    chat, transcript = _chat(root, "c1")
    reg = {"threads": [_thread(transcript)], "archived": [], "deleted": []}
    archive_root = tmp_path / "archive"

    result = lifecycle.archive_thread(
        reg, reg["threads"][0], archive_root_path=archive_root, now_iso="2026-11-11T00:00:00+00:00"
    )

    archived = Path(result.path)
    assert sorted(p.name for p in archived.parent.iterdir()) == ["c1", "c1.jsonl"]
    assert (archived.parent / "c1" / "store.db").is_file()
    assert not chat.exists()

    restored, _ = lifecycle.restore(reg, reg["archived"][0])

    assert restored == str(transcript)
    assert (chat / "store.db").is_file() and transcript.is_file()
    assert list(archived.parent.iterdir()) == []


def test_delete_removes_the_chat_dir_with_the_transcript(tmp_path):
    root = tmp_path / "_cursor"
    chat, transcript = _chat(root, "c1")
    path = scanner.filesystem_path(_thread(transcript), "delete")
    companions = scanner.companion_paths("cursor", path)
    path.unlink()
    scanner.remove_companions(companions)
    assert (chat.exists(), transcript.exists(), chat.parent.is_dir()) == (False, False, True)


@pytest.mark.parametrize("operation", ["delete", "prune"])
def test_delete_and_prune_accept_a_chat_kept_under_a_separate_config_root(
    tmp_path, monkeypatch, operation
):
    """`XDG_CONFIG_HOME` alone moves Cursor's chats out from under its transcripts."""
    config, data = tmp_path / "config", tmp_path / "data"
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(config))
    monkeypatch.setenv("CURSOR_DATA_DIR", str(data))
    _, transcript = _chat(config, "c1", data_root=data)

    assert scanner.filesystem_path(_thread(transcript), operation) == transcript


def test_delete_refuses_a_transcript_outside_both_cursor_roots(tmp_path, monkeypatch):
    config, data = tmp_path / "config", tmp_path / "data"
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(config))
    monkeypatch.setenv("CURSOR_DATA_DIR", str(data))
    _chat(config, "c1", data_root=data)
    stray = data / "c1.jsonl"
    stray.write_text("{}\n")

    with pytest.raises(RuntimeError, match="outside the cursor session store"):
        scanner.filesystem_path(_thread(stray), "delete")


def test_export_renders_a_cursor_transcript(tmp_path):
    _, transcript = _chat(tmp_path, "c1")
    assert export._render_jsonl(transcript) == (
        "## USER\n\nfix the build\n\n_(tools: Shell)_\n\n## ASSISTANT\n\nFixed it.\n"
    )
