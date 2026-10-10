from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from ringleader import export, paths, slug
from ringleader.commands import resume as resume_cmd
from ringleader.picker.app import Choice
from ringleader.portability.capabilities import RunRequest
from ringleader.portability.model import Identity
from ringleader.portability.native_resume import ConversionFailure, NativeResume
from ringleader.presence import PresenceUnavailable
from ringleader.resume import Scope

TURNS = [
    {"type": "user", "message": {"role": "user", "content": "fix the login bug"}},
    {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "on it"}]},
    },
]

RESUME_ARGV = {
    "claude-code": ("claude", "--resume", "aaa111"),
    "codex": ("codex", "resume", "aaa111"),
    "opencode": ("opencode", "--session", "aaa111"),
    "pi": ("pi", "--session", "aaa111"),
}

HANDOFF_PREFIX = {
    "codex": ("codex",),
    "opencode": ("opencode", "--prompt"),
    "pi": ("pi", "--"),
}


class Runner:
    """Stands in for the chdir+exec runner, so a test can read what would have run."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def __call__(self, argv, cwd) -> int:
        self.calls.append((tuple(argv), cwd))
        return 0

    @property
    def argv(self) -> tuple[str, ...]:
        return self.calls[0][0]

    @property
    def cwd(self) -> str:
        return self.calls[0][1]


class Picker:
    """Stands in for the TUI. `scans` is what its background refresh persists before it
    returns, which is how the registry moves on under the command's feet."""

    def __init__(self, choice: Choice | None = None, *, scans: list[dict] | None = None) -> None:
        self.choice = choice
        self.scans = scans
        self.calls: list[dict] = []

    def __call__(self, registry_path, cwd, scope, query, target) -> Choice | None:
        self.calls.append(
            {
                "registry": registry_path,
                "cwd": cwd,
                "scope": scope,
                "query": query,
                "target": target,
            }
        )
        if self.scans is not None:
            _rewrite(registry_path, self.scans)
        return self.choice


class Rescanner:
    """Stands in for `refresh.auto_refresh`, so no test scans the real machine."""

    def __init__(self, threads: list[dict] | None = None) -> None:
        self.threads = threads
        self.calls: list[str] = []

    def __call__(self, registry_path, now_iso) -> tuple[dict, str | None]:
        self.calls.append(registry_path)
        if self.threads is not None:
            _rewrite(registry_path, self.threads)
        return json.loads(Path(registry_path).read_text(encoding="utf-8")), None


def installed(binary: str) -> str:
    return f"/opt/bin/{binary}"


def missing(binary: str) -> None:
    return None


def _run(argv, *, runner=None, picker=None, which=installed, refresher=None, presence=None) -> int:
    return resume_cmd.cmd_resume(
        argv,
        runner=runner or Runner(),
        picker=picker or Picker(),
        which=which,
        refresher=refresher or Rescanner(),
        presence=presence or frozenset,
    )


def _session(
    tmp_path: Path,
    sid: str,
    *,
    source: str = "claude-code",
    project: Path | None = None,
    stored_for: Path | None = None,
    records=TURNS,
    **fields,
) -> dict:
    """A registry thread whose project directory and transcript both exist on disk.

    `stored_for` files the transcript under another directory's Claude slug, which is
    what a session that cd'd away looks like."""
    project = tmp_path / "work" / "api" if project is None else project
    project.mkdir(parents=True, exist_ok=True)
    store = tmp_path / "store" / slug.slugify(str(stored_for or project))
    store.mkdir(parents=True, exist_ok=True)
    transcript = store / f"{sid}.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    thread = {
        "sessionId": sid,
        "source": source,
        "file": str(transcript),
        "project": str(project),
        "title": f"session {sid}",
        "mtime": "2026-09-01T00:00:00+00:00",
        "status": "unfinished",
        "decision": None,
    }
    thread.update(fields)
    return thread


def _registry(tmp_path: Path, *threads: dict, archived: list[dict] | None = None) -> str:
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": list(threads),
                "archived": list(archived or []),
                "deleted": [],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _rewrite(registry_path, threads: list[dict]) -> None:
    path = Path(registry_path)
    reg = json.loads(path.read_text(encoding="utf-8")) | {"threads": list(threads)}
    path.write_text(json.dumps(reg), encoding="utf-8")


def _handoffs() -> list[Path]:
    directory = paths.handoffs_dir()
    return sorted(directory.iterdir()) if directory.is_dir() else []


# --- resuming with the session's own agent -----------------------------------


@pytest.mark.parametrize("source", sorted(RESUME_ARGV))
def test_a_same_agent_resume_runs_that_agents_own_resume_argv(tmp_path, source):
    thread = _session(tmp_path, "aaa111", source=source)
    reg = _registry(tmp_path, thread)
    runner = Runner()

    assert _run(["aaa111", "--registry", reg], runner=runner) == 0
    assert runner.argv == RESUME_ARGV[source]


def test_the_runner_is_given_the_directory_the_session_was_launched_in(tmp_path):
    project = tmp_path / "work" / "api"
    reg = _registry(tmp_path, _session(tmp_path, "aaa111", project=project))
    runner = Runner()

    assert _run(["aaa111", "--registry", reg], runner=runner) == 0
    assert runner.cwd == str(project)


def test_a_drifted_claude_session_is_launched_where_its_transcript_is_filed(tmp_path):
    """The transcript sits under the slug of /work/api while the session recorded
    /work/api/sub, so only the sibling's undrifted record says where to start."""
    launched = tmp_path / "work" / "api"
    drifted_to = launched / "sub"
    drifted_to.mkdir(parents=True, exist_ok=True)
    drifted = _session(tmp_path, "aaa111", project=drifted_to, stored_for=launched)
    sibling = _session(tmp_path, "bbb222", project=launched)
    reg = _registry(tmp_path, drifted, sibling)
    runner = Runner()

    assert _run(["aaa111", "--registry", reg], runner=runner) == 0
    assert runner.cwd == str(launched)
    assert drifted["project"] == str(drifted_to)


def test_the_picker_path_launches_where_the_list_the_picker_returned_says(tmp_path):
    """Textual cancels the picker's scan worker without joining its thread, so the sibling
    that recovers the drift can still be missing from the registry on disk."""
    launched = tmp_path / "work" / "api"
    drifted_to = launched / "sub"
    drifted_to.mkdir(parents=True, exist_ok=True)
    drifted = _session(tmp_path, "aaa111", project=drifted_to, stored_for=launched)
    sibling = _session(tmp_path, "bbb222", project=launched)
    reg = _registry(tmp_path, drifted)
    picker = Picker(Choice(thread=drifted, target="claude-code", threads=(drifted, sibling)))
    runner = Runner()

    assert _run(["--all", "--registry", reg], runner=runner, picker=picker) == 0
    assert runner.cwd == str(launched)
    assert json.loads(Path(reg).read_text(encoding="utf-8"))["threads"] == [drifted]


def test_the_picker_path_falls_back_to_the_registry_on_disk_for_its_thread_list(tmp_path):
    """A choice that carries no list — the picker quit before its scan landed — still has
    the sibling to find, and the registry on disk is the only other place to look."""
    launched = tmp_path / "work" / "api"
    drifted_to = launched / "sub"
    drifted_to.mkdir(parents=True, exist_ok=True)
    drifted = _session(tmp_path, "aaa111", project=drifted_to, stored_for=launched)
    sibling = _session(tmp_path, "bbb222", project=launched)
    reg = _registry(tmp_path, drifted)
    picker = Picker(Choice(thread=drifted, target="claude-code"), scans=[drifted, sibling])
    runner = Runner()

    assert _run(["--all", "--registry", reg], runner=runner, picker=picker) == 0
    assert runner.cwd == str(launched)


# --- handing off to another agent --------------------------------------------


@pytest.mark.parametrize("target", sorted(HANDOFF_PREFIX))
def test_a_handoff_launches_the_target_agent_with_a_prompt_naming_the_transcript(tmp_path, target):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    assert _run(["aaa111", "--to", target, "--markdown", "--registry", reg], runner=runner) == 0
    prefix = HANDOFF_PREFIX[target]
    assert runner.argv[: len(prefix)] == prefix
    assert len(runner.argv) == len(prefix) + 1
    written = _handoffs()[0]
    assert str(written) in runner.argv[-1]
    assert "claude" in runner.argv[-1]


def test_a_handoff_writes_the_rendered_conversation_under_the_handoffs_directory(tmp_path):
    thread = _session(tmp_path, "aaa111")
    reg = _registry(tmp_path, thread)

    assert _run(["aaa111", "--to", "codex", "--markdown", "--registry", reg]) == 0
    written = _handoffs()
    assert len(written) == 1
    assert written[0].parent == paths.handoffs_dir()
    assert written[0].name.startswith("aaa111-")
    assert written[0].suffix == ".md"
    assert written[0].read_text(encoding="utf-8") == export.render_conversation(thread)
    assert "fix the login bug" in written[0].read_text(encoding="utf-8")


def test_a_handoff_leaves_the_source_sessions_registry_record_as_it_was(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    before = Path(reg).read_text(encoding="utf-8")

    assert _run(["aaa111", "--to", "pi", "--markdown", "--registry", reg]) == 0
    assert Path(reg).read_text(encoding="utf-8") == before


# --- --dry-run ----------------------------------------------------------------


def test_dry_run_prints_the_launch_directory_and_argv_and_launches_nothing(tmp_path, capsys):
    project = tmp_path / "work" / "api"
    reg = _registry(tmp_path, _session(tmp_path, "aaa111", project=project))
    runner = Runner()
    here = os.getcwd()

    assert _run(["aaa111", "--dry-run", "--registry", reg], runner=runner) == 0
    out = capsys.readouterr().out
    assert str(project) in out
    assert "claude --resume aaa111" in out
    assert runner.calls == []
    assert os.getcwd() == here


def test_a_dry_run_handoff_names_the_transcript_it_would_write_but_writes_none(tmp_path, capsys):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    assert (
        _run(
            ["aaa111", "--to", "codex", "--markdown", "--dry-run", "--registry", reg], runner=runner
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "would write:" in out
    assert str(paths.handoffs_dir()) in out
    assert _handoffs() == []
    assert runner.calls == []


def test_a_dry_run_reports_a_missing_binary_as_a_note_and_still_prints_the_argv(tmp_path, capsys):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    assert (
        _run(
            ["aaa111", "--to", "codex", "--markdown", "--dry-run", "--registry", reg],
            runner=runner,
            which=missing,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "codex is not installed" in out
    assert "argv: codex " in out
    assert runner.calls == []


# --- --to ----------------------------------------------------------------------


def test_to_is_handed_to_the_picker_so_it_never_asks_which_agent(tmp_path):
    thread = _session(tmp_path, "aaa111")
    reg = _registry(tmp_path, thread)
    picker = Picker(Choice(thread=thread, target="pi"))
    runner = Runner()

    assert (
        _run(["--all", "--to", "pi", "--markdown", "--registry", reg], runner=runner, picker=picker)
        == 0
    )
    assert picker.calls[0]["target"] == "pi"
    assert runner.argv[0] == "pi"


def test_the_picker_decides_the_agent_when_no_to_flag_names_one(tmp_path):
    thread = _session(tmp_path, "aaa111")
    reg = _registry(tmp_path, thread)
    picker = Picker(Choice(thread=thread, target="codex"))
    runner = Runner()

    assert _run(["--all", "--markdown", "--registry", reg], runner=runner, picker=picker) == 0
    assert picker.calls[0]["target"] is None
    assert runner.argv[0] == "codex"


def test_to_the_sessions_own_agent_resumes_it_without_writing_a_handoff(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    assert _run(["aaa111", "--to", "claude", "--registry", reg], runner=runner) == 0
    assert runner.argv == ("claude", "--resume", "aaa111")
    assert _handoffs() == []


def test_an_unknown_to_agent_reports_the_agents_that_exist(tmp_path, capsys):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    assert _run(["aaa111", "--to", "emacs", "--registry", reg], runner=runner) == 1
    err = capsys.readouterr().err
    assert "emacs" in err
    for agent in ("claude", "codex", "droid", "opencode", "pi"):
        assert agent in err
    assert runner.calls == []


# --- resolving the query --------------------------------------------------------


def test_a_unique_id_prefix_resumes_without_opening_the_picker(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"), _session(tmp_path, "bbb222"))
    runner, picker = Runner(), Picker()

    assert _run(["aaa1", "--registry", reg], runner=runner, picker=picker) == 0
    assert runner.argv == ("claude", "--resume", "aaa111")
    assert picker.calls == []


def test_an_ambiguous_id_prefix_opens_the_picker(tmp_path):
    thread = _session(tmp_path, "aaa111")
    reg = _registry(tmp_path, thread, _session(tmp_path, "aaa222"))
    picker = Picker(Choice(thread=thread, target="claude-code"))
    runner = Runner()

    assert _run(["--all", "aaa", "--registry", reg], runner=runner, picker=picker) == 0
    assert picker.calls[0]["query"] == "aaa"
    assert runner.argv == ("claude", "--resume", "aaa111")


def test_a_text_query_opens_the_picker_with_that_filter_already_typed(tmp_path):
    thread = _session(tmp_path, "aaa111", title="fix the login bug")
    reg = _registry(tmp_path, thread, _session(tmp_path, "bbb222"))
    picker = Picker(Choice(thread=thread, target="claude-code"))
    runner = Runner()

    assert _run(["--all", "login", "--registry", reg], runner=runner, picker=picker) == 0
    assert picker.calls[0]["query"] == "login"
    assert picker.calls[0]["scope"] is Scope.ALL
    assert runner.argv == ("claude", "--resume", "aaa111")


def test_quitting_the_picker_exits_cleanly_without_launching_anything(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    assert _run(["--all", "--registry", reg], runner=runner, picker=Picker(None)) == 0
    assert runner.calls == []


# --- failure paths ---------------------------------------------------------------


def test_a_query_no_scan_can_explain_is_refused_as_stale(tmp_path, capsys):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner, picker, rescan = Runner(), Picker(), Rescanner()

    assert (
        _run(["--all", "zzz", "--registry", reg], runner=runner, picker=picker, refresher=rescan)
        == 1
    )
    err = capsys.readouterr().err
    assert "zzz" in err
    assert "even after a fresh scan" in err
    assert rescan.calls == [reg]
    assert runner.calls == []
    assert picker.calls == []


def test_a_dry_run_with_no_match_does_not_persist_its_scan(tmp_path, monkeypatch):
    reg = _registry(tmp_path)
    persisted = []

    def fake_auto_refresh(registry_path, now_iso, *, persist=True, **_kwargs):
        persisted.append(persist)
        return resume_cmd.registry.load_registry(registry_path), None

    monkeypatch.setattr(resume_cmd.refresh, "auto_refresh", fake_auto_refresh)

    rc = resume_cmd.cmd_resume(
        ["zzz", "--registry", reg, "--dry-run"],
        runner=Runner(),
        picker=Picker(),
        which=installed,
        presence=frozenset,
    )

    assert rc == 1
    assert persisted == [False]


def test_a_query_the_registry_already_answers_opens_the_picker_without_scanning(tmp_path):
    """Opening instantly is the feature: only a query nothing answers pays for a scan."""
    thread = _session(tmp_path, "aaa111", title="fix the login bug")
    reg = _registry(tmp_path, thread)
    picker = Picker(Choice(thread=thread, target="claude-code"))
    rescan = Rescanner()

    assert _run(["--all", "login", "--registry", reg], picker=picker, refresher=rescan) == 0
    assert picker.calls[0]["query"] == "login"
    assert rescan.calls == []


def test_an_id_that_resolves_outright_resumes_without_scanning(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner, rescan = Runner(), Rescanner()

    assert _run(["aaa111", "--registry", reg], runner=runner, refresher=rescan) == 0
    assert runner.argv == ("claude", "--resume", "aaa111")
    assert rescan.calls == []


def test_an_id_the_registry_learns_only_on_the_rescan_still_resumes(tmp_path):
    fresh = _session(tmp_path, "ccc333")
    reg = _registry(tmp_path)
    runner, picker = Runner(), Picker()
    rescan = Rescanner([fresh])

    assert _run(["ccc333", "--registry", reg], runner=runner, picker=picker, refresher=rescan) == 0
    assert runner.argv == ("claude", "--resume", "ccc333")
    assert picker.calls == []


def test_an_archived_id_names_the_command_that_brings_it_back(tmp_path, capsys):
    reg = _registry(tmp_path, archived=[{"sessionId": "zzz999"}])
    runner, picker, rescan = Runner(), Picker(), Rescanner()

    assert _run(["zzz999", "--registry", reg], runner=runner, picker=picker, refresher=rescan) == 1
    err = capsys.readouterr().err
    assert "rl session unarchive zzz999" in err
    assert rescan.calls == []
    assert runner.calls == []
    assert picker.calls == []


def test_an_empty_scope_opens_the_picker_so_its_own_scan_can_fill_it(tmp_path):
    reg = _registry(tmp_path)
    runner, picker = Runner(), Picker()

    assert _run(["--registry", reg], runner=runner, picker=picker) == 0
    assert len(picker.calls) == 1
    assert picker.calls[0]["scope"] is Scope.CWD
    assert runner.calls == []


def test_a_session_with_no_recorded_directory_cannot_be_resumed(tmp_path, capsys):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111") | {"project": None})
    runner = Runner()

    assert _run(["aaa111", "--registry", reg], runner=runner) == 1
    assert "aaa111" in capsys.readouterr().err
    assert runner.calls == []


def test_a_launch_directory_that_is_gone_is_reported_before_anything_runs(tmp_path, capsys):
    thread = _session(tmp_path, "aaa111", project=tmp_path / "work" / "gone")
    Path(thread["project"]).rmdir()
    reg = _registry(tmp_path, thread)
    runner = Runner()

    assert _run(["aaa111", "--registry", reg], runner=runner) == 1
    assert thread["project"] in capsys.readouterr().err
    assert runner.calls == []


def test_a_source_that_cannot_be_relaunched_points_at_the_handoff_that_works(tmp_path, capsys):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111", source="mystery"))
    runner = Runner()

    assert _run(["aaa111", "--registry", reg], runner=runner) == 1
    err = capsys.readouterr().err
    assert "mystery" in err
    assert "--to <agent>" in err
    assert runner.calls == []


def test_a_session_from_an_unrelaunchable_source_can_still_be_handed_off(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111", source="mystery"))
    runner = Runner()

    assert _run(["aaa111", "--to", "codex", "--markdown", "--registry", reg], runner=runner) == 0
    assert runner.argv[0] == "codex"


def test_an_agent_binary_that_is_not_on_path_is_reported_instead_of_being_exec(tmp_path, capsys):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    assert _run(["aaa111", "--to", "codex", "--registry", reg], runner=runner, which=missing) == 1
    assert "codex is not installed" in capsys.readouterr().err
    assert runner.calls == []
    assert _handoffs() == []


def test_a_missing_binary_for_the_sessions_own_agent_is_reported(tmp_path, capsys):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    assert _run(["aaa111", "--registry", reg], runner=runner, which=missing) == 1
    assert "claude is not installed" in capsys.readouterr().err
    assert runner.calls == []


def test_a_handoff_of_an_unreadable_conversation_is_reported(tmp_path, capsys):
    thread = _session(tmp_path, "aaa111")
    Path(thread["file"]).unlink()
    reg = _registry(tmp_path, thread)
    runner = Runner()

    assert _run(["aaa111", "--to", "codex", "--markdown", "--registry", reg], runner=runner) == 1
    assert "aaa111" in capsys.readouterr().err
    assert runner.calls == []
    assert _handoffs() == []


def test_a_handoff_of_an_empty_conversation_is_reported(tmp_path, capsys):
    records = [{"type": "custom-title", "customTitle": "x"}]
    reg = _registry(tmp_path, _session(tmp_path, "aaa111", records=records))
    runner = Runner()

    assert _run(["aaa111", "--to", "codex", "--markdown", "--registry", reg], runner=runner) == 1
    assert "empty" in capsys.readouterr().err
    assert runner.calls == []
    assert _handoffs() == []


# --- what the handoff file is ---------------------------------------------------


def test_a_handoff_transcript_is_written_readable_only_by_its_owner(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))

    assert _run(["aaa111", "--to", "codex", "--markdown", "--registry", reg]) == 0
    assert _handoffs()[0].stat().st_mode & 0o777 == 0o600


def test_two_handoffs_of_one_session_in_the_same_second_are_two_files(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))

    assert _run(["aaa111", "--to", "codex", "--markdown", "--registry", reg]) == 0
    assert _run(["aaa111", "--to", "pi", "--markdown", "--registry", reg]) == 0
    assert len(_handoffs()) == 2


def test_a_handoff_prompt_names_the_agent_the_session_came_from(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111", source="codex"))
    runner = Runner()

    assert _run(["aaa111", "--to", "pi", "--markdown", "--registry", reg], runner=runner) == 0
    assert "a codex session" in runner.argv[-1]


def test_a_handoff_to_claude_launches_claude_with_the_prompt_as_its_only_argument(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111", source="codex"))
    runner = Runner()

    assert _run(["aaa111", "--to", "claude", "--markdown", "--registry", reg], runner=runner) == 0
    assert runner.argv[0] == "claude"
    assert len(runner.argv) == 2
    assert str(_handoffs()[0]) in runner.argv[-1]


def test_a_handoff_prompt_carries_a_hostile_session_name_through_verbatim(tmp_path):
    title = 'we"re; rm -rf $HOME `id`\nsecond line'
    reg = _registry(tmp_path, _session(tmp_path, "aaa111", title=title))
    runner = Runner()

    assert _run(["aaa111", "--to", "pi", "--markdown", "--registry", reg], runner=runner) == 0
    assert runner.argv[:2] == ("pi", "--")
    assert len(runner.argv) == 3
    assert 'we"re; rm -rf $HOME `id`' in runner.argv[-1]
    assert "second line" in runner.argv[-1]


def test_a_same_agent_resume_leaves_the_registry_record_as_it_was(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    before = Path(reg).read_text(encoding="utf-8")

    assert _run(["aaa111", "--registry", reg]) == 0
    assert Path(reg).read_text(encoding="utf-8") == before


# --- scope and the picker's arguments -------------------------------------------


def test_recursive_opens_the_picker_over_the_whole_directory_tree(tmp_path):
    thread = _session(tmp_path, "aaa111")
    reg = _registry(tmp_path, thread)
    picker = Picker(Choice(thread=thread, target="claude-code"))

    assert _run(["--recursive", "--registry", reg], picker=picker) == 0
    assert picker.calls[0]["scope"] is Scope.RECURSIVE


def test_all_and_recursive_cannot_be_asked_for_together(tmp_path):
    reg = _registry(tmp_path)

    with pytest.raises(SystemExit) as raised:
        _run(["--all", "--recursive", "--registry", reg])
    assert raised.value.code == 2


def test_the_picker_is_given_the_registry_path_and_the_directory_rl_was_run_in(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    picker = Picker()

    assert _run(["--all", "--registry", reg], picker=picker) == 0
    assert picker.calls[0]["registry"] == reg
    assert picker.calls[0]["cwd"] == os.getcwd()


def test_two_sessions_sharing_one_id_go_to_the_picker_rather_than_the_first_of_them(tmp_path):
    claude_side = _session(tmp_path, "same11")
    codex_side = _session(tmp_path, "same11", source="codex")
    reg = _registry(tmp_path, claude_side, codex_side)
    picker = Picker(Choice(thread=codex_side, target="codex"))
    runner = Runner()

    assert _run(["--all", "same11", "--registry", reg], runner=runner, picker=picker) == 0
    assert len(picker.calls) == 1
    assert picker.calls[0]["query"] == "same11"
    assert runner.argv == ("codex", "resume", "same11")


def test_importing_the_resume_command_leaves_textual_unimported(tmp_path):
    """The picker costs ~200ms to import, and `rl resume <id>` never opens one."""
    code = "import sys, ringleader.commands.resume; print('textual' in sys.modules)"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "False"


# --- a session that is running right now ------------------------------------------


def _live(agent: str, session_id: str):
    return lambda: frozenset({(agent, session_id)})


def _refuse_to_be_asked(prompt):
    raise AssertionError(f"asked: {prompt}")


def test_resuming_a_session_presence_reports_as_running_asks_before_launching(
    tmp_path, monkeypatch
):
    """Two agents appending to one transcript is not recoverable, and the id path never
    opens the picker that would otherwise have asked."""
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "y")
    reg = _registry(tmp_path, _session(tmp_path, "aaa111", title="the login work"))
    runner = Runner()

    rc = _run(["aaa111", "--registry", reg], runner=runner, presence=_live("claude", "aaa111"))
    assert rc == 0
    assert "the login work is running right now" in asked[0]
    assert runner.argv == ("claude", "--resume", "aaa111")


def test_declining_the_running_prompt_leaves_the_session_alone(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    rc = _run(["aaa111", "--registry", reg], runner=runner, presence=_live("claude", "aaa111"))
    assert rc == 0
    assert runner.calls == []


def test_a_session_that_is_not_running_is_resumed_without_a_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", _refuse_to_be_asked)
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    rc = _run(["aaa111", "--registry", reg], runner=runner, presence=_live("claude", "bbb222"))
    assert rc == 0
    assert runner.argv == ("claude", "--resume", "aaa111")


def test_handing_a_running_session_to_another_agent_needs_no_prompt(tmp_path, monkeypatch):
    """A handoff starts a new session and leaves the transcript untouched."""
    monkeypatch.setattr("builtins.input", _refuse_to_be_asked)
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    rc = _run(
        ["aaa111", "--to", "codex", "--markdown", "--registry", reg],
        runner=runner,
        presence=_live("claude", "aaa111"),
    )
    assert rc == 0
    assert runner.argv[0] == "codex"


def test_to_the_sessions_own_agent_is_guarded_like_any_other_resume(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "y")
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    rc = _run(
        ["aaa111", "--to", "claude", "--registry", reg],
        runner=runner,
        presence=_live("claude", "aaa111"),
    )
    assert rc == 0
    assert "is running right now" in asked[0]
    assert runner.argv == ("claude", "--resume", "aaa111")


def test_a_presence_probe_that_cannot_answer_resumes_without_a_prompt(tmp_path, monkeypatch):
    """`PresenceUnavailable` means nobody knows, which is not the same as nothing runs;
    either way it must not stand between the user and their session."""
    monkeypatch.setattr("builtins.input", _refuse_to_be_asked)

    def unavailable():
        raise PresenceUnavailable("hooklinesinker is not installed")

    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    assert _run(["aaa111", "--registry", reg], runner=runner, presence=unavailable) == 0
    assert runner.argv == ("claude", "--resume", "aaa111")


def test_a_confirmation_the_picker_already_took_is_not_asked_for_again(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", _refuse_to_be_asked)
    thread = _session(tmp_path, "aaa111")
    reg = _registry(tmp_path, thread)
    picker = Picker(Choice(thread=thread, target="claude-code", confirmed=True))
    runner = Runner()

    rc = _run(
        ["--all", "--registry", reg],
        runner=runner,
        picker=picker,
        presence=_live("claude", "aaa111"),
    )
    assert rc == 0
    assert runner.argv == ("claude", "--resume", "aaa111")


def test_the_picker_path_asks_when_the_picker_itself_did_not(tmp_path, monkeypatch):
    """The picker's own presence probe can fail while the command's answers, so the guard
    holds at the exec site whichever route reached it."""
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "y")
    thread = _session(tmp_path, "aaa111")
    reg = _registry(tmp_path, thread)
    picker = Picker(Choice(thread=thread, target="claude-code"))
    runner = Runner()

    rc = _run(
        ["--all", "--registry", reg],
        runner=runner,
        picker=picker,
        presence=_live("claude", "aaa111"),
    )
    assert rc == 0
    assert "is running right now" in asked[0]
    assert runner.argv == ("claude", "--resume", "aaa111")


def test_a_dry_run_of_a_running_session_prints_the_plan_without_asking(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", _refuse_to_be_asked)
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    rc = _run(
        ["aaa111", "--dry-run", "--registry", reg],
        runner=runner,
        presence=_live("claude", "aaa111"),
    )
    assert rc == 0
    assert runner.calls == []


# --- how much of an id counts ------------------------------------------------------


def test_a_one_character_query_filters_the_picker_rather_than_resuming(tmp_path):
    """A fragment that short is what a user types to start filtering, and on a small
    registry it is unique often enough to exec into an unrelated directory."""
    thread = _session(tmp_path, "aaa111")
    reg = _registry(tmp_path, thread)
    picker = Picker(Choice(thread=thread, target="claude-code"))
    runner = Runner()

    assert _run(["--all", "a", "--registry", reg], runner=runner, picker=picker) == 0
    assert picker.calls[0]["query"] == "a"


def test_an_exact_session_id_resumes_however_short_it_is(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "ab"), _session(tmp_path, "abc"))
    runner, picker = Runner(), Picker()

    assert _run(["ab", "--registry", reg], runner=runner, picker=picker) == 0
    assert runner.argv == ("claude", "--resume", "ab")
    assert picker.calls == []


# --- where the handoff transcript lands ---------------------------------------------


def test_a_session_id_shaped_like_a_path_still_writes_inside_the_handoffs_directory():
    """Ids are copied out of transcripts, not minted here, and `handoffs/` is the only
    directory this command may create."""
    written = resume_cmd._handoff_path("../../../escaped")

    assert written.parent == paths.handoffs_dir()
    assert written.name.startswith("escaped-")


def test_the_handoffs_directory_is_created_owner_only(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))

    assert _run(["aaa111", "--to", "codex", "--markdown", "--registry", reg]) == 0
    assert paths.handoffs_dir().stat().st_mode & 0o777 == 0o700


def test_a_missing_binary_is_reported_before_the_transcript_is_rendered(tmp_path, capsys):
    """Both would fail; the cheap check is the one worth doing first."""
    thread = _session(tmp_path, "aaa111")
    Path(thread["file"]).unlink()
    reg = _registry(tmp_path, thread)
    runner = Runner()

    rc = _run(["aaa111", "--to", "codex", "--registry", reg], runner=runner, which=missing)
    assert rc == 1
    assert "codex is not installed" in capsys.readouterr().err
    assert runner.calls == []


# --- --last ------------------------------------------------------------------


def test_last_resumes_the_most_recent_session_without_opening_the_picker(tmp_path, monkeypatch):
    older = _session(tmp_path, "aaa111", mtime="2026-09-01T00:00:00+00:00")
    newer = _session(tmp_path, "bbb222", mtime="2026-09-20T00:00:00+00:00")
    reg = _registry(tmp_path, older, newer)
    runner, picker = Runner(), Picker()
    monkeypatch.chdir(tmp_path / "work" / "api")

    assert _run(["--last", "--registry", reg], runner=runner, picker=picker) == 0
    assert runner.argv == ("claude", "--resume", "bbb222")
    assert picker.calls == []


def test_last_re_enters_whichever_agent_the_session_belonged_to(tmp_path, monkeypatch):
    """The reason this beats `claude -c`: the most recent session in a directory may
    belong to any agent, and each agent's own shortcut only knows its own."""
    claude = _session(tmp_path, "aaa111", source="claude-code", mtime="2026-09-01T00:00:00+00:00")
    codex = _session(tmp_path, "bbb222", source="codex", mtime="2026-09-20T00:00:00+00:00")
    runner = Runner()
    monkeypatch.chdir(tmp_path / "work" / "api")

    assert _run(["--last", "--registry", _registry(tmp_path, claude, codex)], runner=runner) == 0
    assert runner.argv == ("codex", "resume", "bbb222")


def test_last_obeys_the_scope_flags(tmp_path, monkeypatch):
    here = _session(tmp_path, "aaa111", mtime="2026-09-01T00:00:00+00:00")
    below = _session(
        tmp_path,
        "bbb222",
        project=tmp_path / "work" / "api" / "sub",
        mtime="2026-09-20T00:00:00+00:00",
    )
    reg = _registry(tmp_path, here, below)
    scoped, recursive = Runner(), Runner()
    monkeypatch.chdir(tmp_path / "work" / "api")

    assert _run(["--last", "--registry", reg], runner=scoped) == 0
    assert scoped.argv == ("claude", "--resume", "aaa111")

    assert _run(["--last", "--recursive", "--registry", reg], runner=recursive) == 0
    assert recursive.argv == ("claude", "--resume", "bbb222")


def test_last_hands_the_most_recent_session_to_another_agent(tmp_path, monkeypatch):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()
    monkeypatch.chdir(tmp_path / "work" / "api")

    assert _run(["--last", "--to", "codex", "--markdown", "--registry", reg], runner=runner) == 0
    assert runner.argv[0] == "codex"
    assert len(_handoffs()) == 1


def test_last_scans_once_before_refusing_an_empty_scope(tmp_path, monkeypatch):
    reg = _registry(tmp_path)
    found = _session(tmp_path, "aaa111")
    runner, rescan = Runner(), Rescanner([found])
    monkeypatch.chdir(tmp_path / "work" / "api")

    assert _run(["--last", "--registry", reg], runner=runner, refresher=rescan) == 0
    assert rescan.calls == [reg]
    assert runner.argv == ("claude", "--resume", "aaa111")


def test_last_reports_an_empty_scope_that_survives_a_scan(tmp_path, capsys):
    runner = Runner()
    rc = _run(["--last", "--registry", _registry(tmp_path)], runner=runner, refresher=Rescanner([]))

    assert rc == 1
    assert "even after a fresh scan" in capsys.readouterr().err
    assert runner.calls == []


def test_last_refuses_to_also_take_a_query(tmp_path, capsys):
    runner = Runner()
    rc = _run(["auth", "--last", "--registry", _registry(tmp_path)], runner=runner)

    assert rc == 1
    assert "cannot also take 'auth'" in capsys.readouterr().err
    assert runner.calls == []


# --- native conversion behind --to ------------------------------------------------


class Native:
    """Stands in for `native_resume`, so a test chooses the outcome the command sees."""

    def __init__(self, outcome=None, *, error: Exception | None = None) -> None:
        self.outcome = outcome
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, source, target, **options):
        self.calls.append({"source": source, "target": target, **options})
        if self.error is not None:
            raise self.error
        return self.outcome


class Launches:
    def __init__(self) -> None:
        self.requests: list[RunRequest] = []

    def __call__(self, request: RunRequest) -> None:
        self.requests.append(request)


def _request(tmp_path: Path) -> RunRequest:
    return RunRequest(
        ("codex", "resume", "t-1"),
        tmp_path / "work" / "api",
        (("CODEX_HOME", str(tmp_path / "codex-home")), ("HOME", str(tmp_path))),
    )


def _outcome(tmp_path: Path, outcome: str, **fields) -> NativeResume:
    launches = outcome in ("converted", "reused", "forked", "would-convert")
    defaults = {
        "target": Identity("codex", "t-1"),
        "launch": _request(tmp_path) if launches else None,
        "reason": None,
    }
    return NativeResume(outcome, **(defaults | fields))


def _convert(argv, native, *, runner=None, launch=None, presence=None, which=installed) -> int:
    return resume_cmd.cmd_resume(
        argv,
        runner=runner or Runner(),
        picker=Picker(),
        which=which,
        refresher=Rescanner(),
        presence=presence or frozenset,
        native=native,
        launch=launch or Launches(),
    )


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(resume_cmd, "_interactive", lambda: True)


@pytest.fixture
def no_tty(monkeypatch):
    monkeypatch.setattr(resume_cmd, "_interactive", lambda: False)


@pytest.mark.parametrize("outcome", ["converted", "reused", "forked"])
def test_a_native_conversion_execs_the_whole_run_request_environment_included(
    tmp_path, outcome, no_tty
):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    native, launches, runner = Native(_outcome(tmp_path, outcome)), Launches(), Runner()

    assert (
        _convert(
            ["aaa111", "--to", "codex", "--registry", reg], native, runner=runner, launch=launches
        )
        == 0
    )
    assert launches.requests == [_request(tmp_path)]
    assert runner.calls == []
    assert _handoffs() == []


def test_an_exec_failure_after_conversion_names_the_retained_target_to_resume(
    tmp_path, capsys, no_tty
):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    native = Native(_outcome(tmp_path, "converted", target=Identity("codex", "t-1")))

    def failing_launch(request) -> None:
        raise OSError("no such file or directory")

    rc = _convert(["aaa111", "--to", "codex", "--registry", reg], native, launch=failing_launch)

    assert rc == 1
    err = capsys.readouterr().err
    assert "codex:t-1" in err
    assert "rl resume codex:t-1" in err


def test_the_native_attempt_is_given_the_source_target_and_state_paths(tmp_path, tty):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    native = Native(_outcome(tmp_path, "converted"))
    argv = ["aaa111", "--to", "codex", "--registry", reg]
    argv += ["--operations", str(tmp_path / "ops"), "--verified", str(tmp_path / "v.json")]

    assert _convert(argv, native) == 0
    call = native.calls[0]
    assert (call["source"].identity, call["target"]) == (Identity("claude-code", "aaa111"), "codex")
    assert call["registry_path"] == Path(reg)
    assert call["operations_path"] == tmp_path / "ops"
    assert call["verified_path"] == tmp_path / "v.json"
    assert (call["dry_run"], call["allow_verify"]) == (False, True)


def test_a_native_attempt_off_a_terminal_is_not_allowed_to_self_verify(tmp_path, no_tty):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    native = Native(_outcome(tmp_path, "converted"))

    assert _convert(["aaa111", "--to", "codex", "--registry", reg], native) == 0
    assert native.calls[0]["allow_verify"] is False


def test_reopening_a_reused_target_that_is_running_asks_first(tmp_path, monkeypatch, no_tty):
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "y")
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    launches = Launches()

    rc = _convert(
        ["aaa111", "--to", "codex", "--registry", reg],
        Native(_outcome(tmp_path, "reused")),
        launch=launches,
        presence=_live("codex", "t-1"),
    )
    assert rc == 0
    assert asked == ["codex:t-1 is running right now — resume it anyway? [y/N] "]
    assert launches.requests == [_request(tmp_path)]


def test_declining_a_running_reused_target_launches_nothing(tmp_path, monkeypatch, no_tty):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    launches = Launches()

    rc = _convert(
        ["aaa111", "--to", "codex", "--registry", reg],
        Native(_outcome(tmp_path, "reused")),
        launch=launches,
        presence=_live("codex", "t-1"),
    )
    assert rc == 0
    assert launches.requests == []


def test_converting_a_running_source_asks_before_the_attempt(tmp_path, monkeypatch, no_tty):
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "n")
    reg = _registry(tmp_path, _session(tmp_path, "aaa111", title="the login work"))
    native, launches = Native(_outcome(tmp_path, "converted")), Launches()

    rc = _convert(
        ["aaa111", "--to", "codex", "--registry", reg],
        native,
        launch=launches,
        presence=_live("claude", "aaa111"),
    )
    assert rc == 0
    assert asked == [
        "the login work is running right now — convert its current state anyway? [y/N] "
    ]
    assert native.calls == []
    assert launches.requests == []


def test_a_refusal_hands_off_markdown_when_the_user_says_yes(tmp_path, monkeypatch, tty, capsys):
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "y")
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner, launches = Runner(), Launches()

    rc = _convert(
        ["aaa111", "--to", "codex", "--registry", reg],
        Native(_outcome(tmp_path, "refused", target=None, reason="Unsupported thing")),
        runner=runner,
        launch=launches,
    )
    assert rc == 0
    assert "native conversion to codex refused: Unsupported thing" in capsys.readouterr().err
    assert asked == ["Hand off a Markdown transcript instead? [y/N] "]
    assert runner.argv[0] == "codex"
    assert str(_handoffs()[0]) in runner.argv[-1]
    assert launches.requests == []


def test_a_refusal_the_user_declines_exits_one_with_nothing_launched(
    tmp_path, monkeypatch, tty, capsys
):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner, launches = Runner(), Launches()

    rc = _convert(
        ["aaa111", "--to", "codex", "--registry", reg],
        Native(_outcome(tmp_path, "refused", target=None, reason="Unsupported thing")),
        runner=runner,
        launch=launches,
    )
    assert rc == 1
    assert capsys.readouterr().err.endswith("not resumed\n")
    assert (runner.calls, launches.requests, _handoffs()) == ([], [], [])


def test_a_refusal_off_a_terminal_exits_one_without_asking(tmp_path, monkeypatch, no_tty, capsys):
    monkeypatch.setattr("builtins.input", _refuse_to_be_asked)
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner = Runner()

    rc = _convert(
        ["aaa111", "--to", "codex", "--registry", reg],
        Native(_outcome(tmp_path, "refused", target=None, reason="Unsupported thing")),
        runner=runner,
    )
    assert rc == 1
    assert capsys.readouterr().err == (
        "native conversion to codex refused: Unsupported thing\nnot resumed\n"
    )
    assert (runner.calls, _handoffs()) == ([], [])


def test_a_conversion_failure_exits_one_without_offering_markdown(
    tmp_path, monkeypatch, tty, capsys
):
    monkeypatch.setattr("builtins.input", _refuse_to_be_asked)
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    runner, launches = Runner(), Launches()
    failure = ConversionFailure("native conversion to codex failed: boom; transaction abc")

    rc = _convert(
        ["aaa111", "--to", "codex", "--registry", reg],
        Native(error=failure),
        runner=runner,
        launch=launches,
    )
    assert rc == 1
    assert capsys.readouterr().err == (
        "rl-resume: native conversion to codex failed: boom; transaction abc\n"
    )
    assert (runner.calls, launches.requests, _handoffs()) == ([], [], [])


def test_markdown_hands_off_without_a_native_attempt_or_a_question(tmp_path, monkeypatch, tty):
    monkeypatch.setattr("builtins.input", _refuse_to_be_asked)
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    native, runner = Native(_outcome(tmp_path, "converted")), Runner()

    assert (
        _convert(
            ["aaa111", "--to", "codex", "--markdown", "--registry", reg], native, runner=runner
        )
        == 0
    )
    assert native.calls == []
    assert runner.argv[0] == "codex"
    assert len(_handoffs()) == 1


def test_the_same_agent_path_never_attempts_a_conversion(tmp_path):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    native, runner = Native(_outcome(tmp_path, "converted")), Runner()

    assert _convert(["aaa111", "--to", "claude", "--registry", reg], native, runner=runner) == 0
    assert native.calls == []
    assert runner.argv == ("claude", "--resume", "aaa111")


def test_the_picker_agent_modal_takes_the_native_path(tmp_path, no_tty):
    thread = _session(tmp_path, "aaa111")
    reg = _registry(tmp_path, thread)
    native, launches = Native(_outcome(tmp_path, "converted")), Launches()

    rc = resume_cmd.cmd_resume(
        ["--all", "--registry", reg],
        runner=Runner(),
        picker=Picker(Choice(thread=thread, target="codex")),
        which=installed,
        refresher=Rescanner(),
        presence=frozenset,
        native=native,
        launch=launches,
    )
    assert rc == 0
    assert native.calls[0]["target"] == "codex"
    assert launches.requests == [_request(tmp_path)]


@pytest.mark.parametrize(
    ("outcome", "fields", "line"),
    [
        ("would-convert", {}, "native: would convert to codex (new)"),
        ("reused", {}, "native: would reopen codex:t-1 (reused)"),
        (
            "forked",
            {"target": None, "previous": "archived"},
            "native: would fork codex (previous conversion archived)",
        ),
        (
            "would-verify",
            {"target": None, "version": "9.9.9"},
            "native: would self-verify codex 9.9.9, then convert",
        ),
        (
            "refused",
            {"target": None, "reason": "Unsupported thing"},
            "native: refused — Unsupported thing; fallback would ask",
        ),
    ],
)
def test_a_dry_run_prints_the_one_native_line_and_writes_nothing(
    tmp_path, monkeypatch, capsys, outcome, fields, line
):
    monkeypatch.setattr("builtins.input", _refuse_to_be_asked)
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    before = Path(reg).read_text(encoding="utf-8")
    native, runner, launches = Native(_outcome(tmp_path, outcome, **fields)), Runner(), Launches()

    rc = _convert(
        ["aaa111", "--to", "codex", "--dry-run", "--registry", reg],
        native,
        runner=runner,
        launch=launches,
        presence=_live("claude", "aaa111"),
    )
    assert rc == 0
    native_lines = [row.strip() for row in capsys.readouterr().out.splitlines() if "native:" in row]
    assert native_lines == [line]
    assert native.calls[0]["dry_run"] is True
    assert (runner.calls, launches.requests, _handoffs()) == ([], [], [])
    assert Path(reg).read_text(encoding="utf-8") == before


def test_a_dry_run_reused_native_line_names_the_targets_own_launch_directory(tmp_path, capsys):
    reg = _registry(tmp_path, _session(tmp_path, "aaa111"))
    target_cwd = tmp_path / "other" / "workspace"
    target_cwd.mkdir(parents=True)
    launch = RunRequest(("codex", "resume", "t-1"), target_cwd, ())
    native = Native(_outcome(tmp_path, "reused", target=Identity("codex", "t-1"), launch=launch))

    rc = _convert(["aaa111", "--to", "codex", "--dry-run", "--registry", reg], native)

    assert rc == 0
    out = capsys.readouterr().out
    assert f"directory: {target_cwd}" in out
    assert str(tmp_path / "work" / "api") not in out
