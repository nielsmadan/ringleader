import json
from pathlib import Path

import pytest
from ringleader.slug import normalize, slugify
from ringleader.sources.base import (
    CompanionFileSource,
    RawRef,
    SessionLaunchingSource,
    Source,
    WorkspaceRelocatingSource,
)
from ringleader.sources.droid import DroidSource, default_sessions_dir

SESSION_ID = "032b05aa-7409-4aef-bf01-4ef2ba1220cd"
CALLER_ID = "2a5ae955-2144-49e2-b0ea-e73f5edaa4b7"
CWD = "/Users/me/wrksp/proj"


def _start(**overrides) -> dict:
    record = {
        "type": "session_start",
        "id": SESSION_ID,
        "title": "Rework the scanner seam",
        "isSessionTitleManuallySet": False,
        "owner": "me",
        "callingSessionId": None,
        "callingToolUseId": None,
        "version": 2,
        "cwd": CWD,
        "hostId": "bd9910b8-f01f-4c49-98b8-66275454250b",
    }
    record.update(overrides)
    return record


def _message(role: str, *texts: str) -> dict:
    return {
        "type": "message",
        "id": f"{role}-{texts[0] if texts else 'empty'}",
        "parentId": SESSION_ID,
        "timestamp": "2026-09-21T22:40:18.970Z",
        "message": {"role": role, "content": [{"type": "text", "text": t} for t in texts]},
    }


def _session(
    sessions_dir: Path,
    records: list[dict | str],
    *,
    cwd: str = CWD,
    session_id: str = SESSION_ID,
    sidecars: bool = False,
) -> Path:
    project = sessions_dir / slugify(normalize(cwd))
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) if isinstance(r, dict) else r for r in records) + "\n",
        encoding="utf-8",
    )
    if sidecars:
        (project / f"{session_id}.settings.json").write_text('{"model":"opus"}', encoding="utf-8")
        (project / f"{session_id}.settings.json.bak").write_text("{}", encoding="utf-8")
    return path


def test_source_satisfies_the_protocols_it_declares(tmp_path):
    source = DroidSource(sessions_dir=tmp_path)

    assert isinstance(source, Source)
    assert isinstance(source, WorkspaceRelocatingSource)
    assert isinstance(source, SessionLaunchingSource)
    assert source.name == "droid"
    assert isinstance(source, CompanionFileSource)
    assert source.capabilities == frozenset(
        {
            "companion_files",
            "filesystem_lifecycle",
            "relocates_workspace",
            "launches_sessions",
        }
    )


def test_discover_finds_transcripts_and_ignores_sidecars_and_favorites(tmp_path):
    transcript = _session(tmp_path, [_start()], sidecars=True)
    (tmp_path / ".favorites").write_text("[]", encoding="utf-8")
    (tmp_path / ".favorites.migrated").write_text("true", encoding="utf-8")

    refs = list(DroidSource(sessions_dir=tmp_path).discover())

    assert [ref.file for ref in refs] == [str(transcript)]
    assert isinstance(refs[0], RawRef)
    assert refs[0].source == "droid"
    assert refs[0].source_ref == SESSION_ID
    assert refs[0].fingerprint
    assert refs[0].size_bytes == transcript.stat().st_size


def test_discover_excludes_a_session_spawned_by_another_session(tmp_path):
    own = _session(tmp_path, [_start()])
    _session(
        tmp_path,
        [_start(id="7c1c0b5e-0000-4000-8000-000000000001", callingSessionId=CALLER_ID)],
        session_id="7c1c0b5e-0000-4000-8000-000000000001",
    )

    refs = list(DroidSource(sessions_dir=tmp_path).discover())

    assert [ref.file for ref in refs] == [str(own)]


def test_discover_missing_dir_yields_nothing(tmp_path):
    assert list(DroidSource(sessions_dir=tmp_path / "missing").discover()) == []


def test_parse_extracts_the_session_start_and_conversation(tmp_path):
    _session(
        tmp_path,
        [
            _start(),
            _message("user", "first prompt"),
            {
                "type": "todo_state",
                "id": "todo-1",
                "todos": [{"content": "ship it", "status": "pending"}],
            },
            _message("assistant", "the answer"),
            _message("user", "last", "prompt"),
        ],
    )
    source = DroidSource(sessions_dir=tmp_path, tail_turns=2)

    parsed = source.parse(next(iter(source.discover())))

    assert parsed.source == "droid"
    assert parsed.session_id == SESSION_ID
    assert parsed.cwd == CWD
    assert parsed.ai_title == "Rework the scanner seam"
    assert parsed.custom_title is None
    assert parsed.message_count == 3
    assert parsed.last_prompt == "last prompt"
    assert parsed.tail_text == "ASSISTANT: the answer\nUSER: last prompt"
    assert parsed.file == str(tmp_path / slugify(normalize(CWD)) / f"{SESSION_ID}.jsonl")


def test_parse_reports_a_hand_set_title_as_the_custom_title(tmp_path):
    _session(tmp_path, [_start(title="my thread", isSessionTitleManuallySet=True)])
    source = DroidSource(sessions_dir=tmp_path)

    parsed = source.parse(next(iter(source.discover())))

    assert parsed.custom_title == "my thread"
    assert parsed.ai_title is None


def test_parse_truncates_a_long_prompt_to_five_hundred_characters(tmp_path):
    _session(tmp_path, [_start(), _message("user", "x" * 900)])
    source = DroidSource(sessions_dir=tmp_path)

    parsed = source.parse(next(iter(source.discover())))

    assert parsed.last_prompt == "x" * 500


def test_a_scan_skips_files_whose_first_record_is_not_a_session_start(tmp_path):
    good = _session(tmp_path, [_start()])
    _session(
        tmp_path,
        [_message("user", "orphan turn")],
        session_id="11111111-0000-4000-8000-000000000001",
    )
    _session(
        tmp_path,
        ["this is not json at all"],
        session_id="22222222-0000-4000-8000-000000000002",
    )

    refs = list(DroidSource(sessions_dir=tmp_path).discover())

    assert [ref.file for ref in refs] == [str(good)]


def test_the_factory_home_override_replaces_home_not_the_factory_dir(tmp_path, monkeypatch):
    """FACTORY_HOME_OVERRIDE stands in for HOME, so droid still puts its store under
    a `.factory` directory beneath it. Spelled out rather than recomputed: sharing the
    decoder with the code under test would make this pass for any encoding."""
    monkeypatch.setenv("FACTORY_HOME_OVERRIDE", str(tmp_path / "elsewhere"))
    expected = Path(f"{tmp_path}/elsewhere/.factory/sessions")

    assert default_sessions_dir() == expected
    assert DroidSource().root == expected


def test_storage_for_uses_the_shared_launch_directory_slug(tmp_path):
    source = DroidSource(sessions_dir=tmp_path)

    assert source.storage_for(CWD) == tmp_path / slugify(normalize(CWD))


def test_relocate_storage_renames_the_slug_directory_and_maps_transcripts(tmp_path):
    transcript = _session(tmp_path, [_start()], sidecars=True)
    source = DroidSource(sessions_dir=tmp_path)
    new_cwd = "/Users/me/wrksp/renamed"

    moved = source.relocate_storage(CWD, new_cwd)

    destination = tmp_path / slugify(normalize(new_cwd))
    assert moved == {str(transcript): str(destination / transcript.name)}
    assert (destination / f"{SESSION_ID}.jsonl").is_file()
    assert (destination / f"{SESSION_ID}.settings.json").is_file()
    assert (destination / f"{SESSION_ID}.settings.json.bak").is_file()
    assert source.relocate_storage("/Users/me/wrksp/never-used", new_cwd) == {}


def test_relocate_storage_raises_rather_than_merging_onto_an_occupied_destination(tmp_path):
    new_cwd = "/Users/me/wrksp/renamed"
    _session(tmp_path, [_start()])
    _session(tmp_path, [_start()], cwd=new_cwd, session_id="33333333-0000-4000-8000-000000000003")
    source = DroidSource(sessions_dir=tmp_path)

    with pytest.raises(RuntimeError, match="already exists"):
        source.relocate_storage(CWD, new_cwd)

    assert (tmp_path / slugify(normalize(CWD)) / f"{SESSION_ID}.jsonl").is_file()


def test_relocate_session_carries_the_settings_sidecars_along(tmp_path):
    transcript = _session(tmp_path, [_start()], sidecars=True)
    source = DroidSource(sessions_dir=tmp_path)
    new_cwd = "/Users/me/wrksp/moved"

    destination = source.relocate_session(str(transcript), new_cwd)

    expected = tmp_path / slugify(normalize(new_cwd))
    assert destination == str(expected / transcript.name)
    assert (expected / f"{SESSION_ID}.settings.json").read_text() == '{"model":"opus"}'
    assert (expected / f"{SESSION_ID}.settings.json.bak").read_text() == "{}"
    assert sorted(p.name for p in (tmp_path / slugify(normalize(CWD))).iterdir()) == []


def test_relocate_session_raises_when_a_sidecar_already_sits_at_the_destination(tmp_path):
    transcript = _session(tmp_path, [_start()], sidecars=True)
    new_cwd = "/Users/me/wrksp/moved"
    occupied = tmp_path / slugify(normalize(new_cwd))
    occupied.mkdir(parents=True)
    (occupied / f"{SESSION_ID}.settings.json").write_text("{}", encoding="utf-8")
    source = DroidSource(sessions_dir=tmp_path)

    with pytest.raises(RuntimeError, match="already exists"):
        source.relocate_session(str(transcript), new_cwd)

    assert transcript.is_file()


def test_discover_keeps_a_session_start_that_omits_calling_session_id_entirely(tmp_path):
    """Droid writes no callingSessionId key at all on a top-level session; it is never
    present-and-null. Pinning the null shape alone lets a stricter default slip through
    and drop every real session."""
    start = _start()
    del start["callingSessionId"]
    del start["callingToolUseId"]
    own = _session(tmp_path, [start])

    refs = list(DroidSource(sessions_dir=tmp_path).discover())

    assert [ref.file for ref in refs] == [str(own)]


def test_parse_treats_a_session_start_without_the_manual_title_flag_as_ai_named(tmp_path):
    start = _start()
    del start["isSessionTitleManuallySet"]
    _session(tmp_path, [start])
    source = DroidSource(sessions_dir=tmp_path)

    parsed = source.parse(next(iter(source.discover())))

    assert parsed.ai_title == "Rework the scanner seam"
    assert parsed.custom_title is None


def test_parse_counts_conversation_turns_and_not_tool_plumbing(tmp_path):
    """Droid files a tool result as a user-role message. Counting those would put
    message_count out of scale with every other source and with `rl prune`'s ceiling."""
    tool_result = {
        "type": "message",
        "id": "tool-result-1",
        "parentId": SESSION_ID,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
    }
    tool_use = {
        "type": "message",
        "id": "tool-use-1",
        "parentId": SESSION_ID,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "read", "input": {}}],
        },
    }
    _session(
        tmp_path,
        [_start(), _message("user", "do it"), tool_use, tool_result, _message("assistant", "done")],
    )
    source = DroidSource(sessions_dir=tmp_path)

    parsed = source.parse(next(iter(source.discover())))

    assert parsed.message_count == 2
    assert parsed.last_prompt == "do it"
    assert parsed.tail_text == "USER: do it\nASSISTANT: done"


def test_companion_paths_names_both_settings_sidecars(tmp_path):
    transcript = _session(tmp_path, [_start()], sidecars=True)
    source = DroidSource(sessions_dir=tmp_path)

    assert source.companion_paths(str(transcript)) == (
        transcript.with_name(f"{SESSION_ID}.settings.json"),
        transcript.with_name(f"{SESSION_ID}.settings.json.bak"),
    )


def test_relocate_session_raises_when_only_the_backup_sidecar_sits_at_the_destination(tmp_path):
    transcript = _session(tmp_path, [_start()], sidecars=True)
    new_cwd = "/Users/me/wrksp/moved"
    occupied = tmp_path / slugify(normalize(new_cwd))
    occupied.mkdir(parents=True)
    (occupied / f"{SESSION_ID}.settings.json.bak").write_text("{}", encoding="utf-8")
    source = DroidSource(sessions_dir=tmp_path)

    with pytest.raises(RuntimeError, match="already exists"):
        source.relocate_session(str(transcript), new_cwd)

    assert transcript.is_file()


def test_discover_reports_a_session_start_with_no_usable_id(tmp_path, capsys):
    good = _session(tmp_path, [_start()])
    blank = _session(
        tmp_path,
        [_start(id="")],
        cwd="/Users/me/wrksp/other",
        session_id="44444444-0000-4000-8000-000000000004",
    )
    missing = _start()
    del missing["id"]
    _session(
        tmp_path,
        [missing],
        cwd="/Users/me/wrksp/third",
        session_id="55555555-0000-4000-8000-000000000005",
    )

    refs = list(DroidSource(sessions_dir=tmp_path).discover())

    assert [ref.file for ref in refs] == [str(good)]
    assert f"skipped {blank}: session_start has no usable id" in capsys.readouterr().err


def test_discover_skips_a_first_line_that_is_json_but_not_a_record(tmp_path):
    good = _session(tmp_path, [_start()])
    _session(tmp_path, ["[1, 2, 3]"], session_id="66666666-0000-4000-8000-000000000006")
    _session(tmp_path, ["42"], session_id="77777777-0000-4000-8000-000000000007")

    refs = list(DroidSource(sessions_dir=tmp_path).discover())

    assert [ref.file for ref in refs] == [str(good)]


def test_discover_skips_an_empty_transcript(tmp_path):
    good = _session(tmp_path, [_start()])
    project = tmp_path / slugify(normalize(CWD))
    (project / "88888888-0000-4000-8000-000000000008.jsonl").write_text("", encoding="utf-8")
    (project / "99999999-0000-4000-8000-000000000009.jsonl").write_text("\n\n", encoding="utf-8")

    refs = list(DroidSource(sessions_dir=tmp_path).discover())

    assert [ref.file for ref in refs] == [str(good)]


def test_discover_yields_nothing_for_a_directory_holding_only_sidecars(tmp_path):
    project = tmp_path / slugify(normalize(CWD))
    project.mkdir(parents=True)
    (project / f"{SESSION_ID}.settings.json").write_text("{}", encoding="utf-8")
    (project / f"{SESSION_ID}.settings.json.bak").write_text("{}", encoding="utf-8")

    assert list(DroidSource(sessions_dir=tmp_path).discover()) == []


def test_discover_skips_droids_btw_forks(tmp_path):
    """`sessions/btw/` is a layout of its own, not a waiting room — a fork filed there
    stays there, so skipping it excludes it for good rather than deferring it."""
    own = _session(tmp_path, [_start()])
    forks = tmp_path / "btw"
    forks.mkdir(parents=True)
    (forks / "aaaaaaaa-0000-4000-8000-00000000000a.jsonl").write_text(
        json.dumps(_start(id="aaaaaaaa-0000-4000-8000-00000000000a")) + "\n", encoding="utf-8"
    )

    refs = list(DroidSource(sessions_dir=tmp_path).discover())

    assert [ref.file for ref in refs] == [str(own)]


def test_discover_finds_a_session_written_to_the_global_sessions_root(tmp_path):
    """Droid's third layout: `sessions/<id>.jsonl`, with no launch-dir slug above it."""
    nested = _session(tmp_path, [_start()])
    root_id = "aaaaaaaa-0000-4000-8000-00000000000a"
    at_root = tmp_path / f"{root_id}.jsonl"
    at_root.write_text(json.dumps(_start(id=root_id)) + "\n", encoding="utf-8")

    refs = list(DroidSource(sessions_dir=tmp_path).discover())

    assert sorted(ref.file for ref in refs) == sorted([str(nested), str(at_root)])
    assert sorted(ref.source_ref for ref in refs) == sorted([SESSION_ID, root_id])


def test_discover_yields_one_ref_for_a_session_filed_under_two_directories(tmp_path):
    """`source_ref` is the registry's key, so two transcripts carrying one session id
    must not arrive as two rows — the same guard the usage reader keeps."""
    first = _session(tmp_path, [_start()])
    _session(tmp_path, [_start()], cwd="/Users/me/wrksp/proj-old")

    refs = list(DroidSource(sessions_dir=tmp_path).discover())

    assert [(ref.source_ref, ref.file) for ref in refs] == [(SESSION_ID, str(first))]
