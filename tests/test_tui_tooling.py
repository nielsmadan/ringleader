import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ringleader import overview, refresh, registry
from ringleader.picker.app import PickerApp
from ringleader.resume import Scope
from ringleader.slug import droid_slug, slugify
from ringleader.sources.claude_code import ClaudeCodeSource
from ringleader.tui.snapshot import screen_text
from scripts.tui_fixture import build_fixture
from scripts.tui_shot import parse_keys, settle_workers, shoot
from textual.widgets import OptionList

from conftest import golden_checker
from tui_helpers import until

NOW = datetime(2026, 5, 5, 12, tzinfo=UTC)


def _thread(session_id, *, source, project, title, mtime):
    return {
        "sessionId": session_id,
        "source": source,
        "file": str(ClaudeCodeSource().storage_for(project) / f"{session_id}.jsonl"),
        "project": project,
        "title": title,
        "aiTitle": None,
        "lastPrompt": f"last prompt for {title}",
        "summary": None,
        "mtime": f"{mtime}T00:00:00+00:00",
        "status": "unfinished",
        "decision": None,
    }


def _picker(tmp_path):
    threads = [
        _thread(
            "s0", source="claude-code", project="/work/repo", title="alpha", mtime="2026-05-03"
        ),
        _thread("s1", source="codex", project="/work/repo", title="bravo", mtime="2026-04-20"),
        _thread("s2", source="pi", project="/work/repo/sub", title="charlie", mtime="2026-03-01"),
        _thread("s3", source="opencode", project="/elsewhere", title="delta", mtime="2026-05-05"),
    ]
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({**registry.empty_registry(), "threads": threads}))
    return PickerApp(
        path,
        "/",
        scope=Scope.ALL,
        refresher=lambda p, now: (registry.load_registry(p), None),
        presence=frozenset,
        now=lambda: NOW,
    )


def _paths(tmp_path):
    return {str(tmp_path): "<root>", str(Path.home()): "~"}


async def test_picker_golden_over_every_scope(tmp_path, golden):
    app = _picker(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle_workers(app, pilot)
        golden("picker/all-scope", screen_text(app), replace=_paths(tmp_path))


async def test_picker_golden_after_typing_a_filter(tmp_path, golden):
    app = _picker(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle_workers(app, pilot)
        await pilot.press(*"bravo")
        await until(pilot, lambda: app.screen.query_one("#rows", OptionList).option_count == 1)
        await until(pilot, lambda: "[all] (1)" in screen_text(app))
        golden("picker/filtered", screen_text(app), replace=_paths(tmp_path))


async def test_screen_text_draws_a_modal_over_its_background(tmp_path):
    app = _picker(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle_workers(app, pilot)
        await pilot.press("enter")
        await until(pilot, lambda: "resume with which agent?" in screen_text(app))
        text = screen_text(app)
    assert "opencode  12h ago" in text


def test_parse_keys_splits_on_commas_and_drops_empty_items():
    assert parse_keys(" down, ,settle,ctrl+r,") == ["down", "settle", "ctrl+r"]
    assert parse_keys("") == []


async def test_shoot_takes_an_initial_shot_and_one_per_key(tmp_path):
    result = await shoot(
        _picker(tmp_path),
        size=(100, 30),
        keys=["down", "settle", "down"],
        settle=settle_workers,
        each=True,
    )
    assert [shot.after for shot in result.shots] == ["", "down", "settle", "down"]
    assert result.exited is False
    assert "alpha" in result.shots[0].text


async def test_shoot_without_each_takes_one_final_shot(tmp_path):
    result = await shoot(_picker(tmp_path), size=(100, 30), keys=["down"], settle=settle_workers)
    assert [shot.after for shot in result.shots] == ["down"]


async def test_shoot_stops_once_the_app_exits(tmp_path):
    result = await shoot(
        _picker(tmp_path),
        size=(100, 30),
        keys=["escape", "down"],
        settle=settle_workers,
        each=True,
    )
    assert result.exited is True
    assert result.return_value is None
    assert [shot.after for shot in result.shots] == [""]


def _registry(fixture):
    return json.loads(fixture.registry_path.read_text())


def test_build_fixture_spreads_threads_over_projects_and_tombstones(tmp_path):
    fixture = build_fixture(tmp_path, threads=200, now=NOW)
    reg = _registry(fixture)
    assert len(reg["threads"]) == 200
    assert len({t["project"] for t in reg["threads"]}) >= 14
    assert len(reg["archived"]) == 30
    assert len(reg["deleted"]) == 100
    assert fixture.registry_path == tmp_path / "xdg-data" / "ringleader" / "registry.json"
    assert fixture.env["XDG_DATA_HOME"] == str(tmp_path / "xdg-data")


def test_build_fixture_covers_every_lifecycle(tmp_path):
    reg = _registry(build_fixture(tmp_path, threads=200, now=NOW))
    now = NOW.isoformat()
    threads = reg["threads"]
    decisions = [t["decision"] for t in threads]
    snoozed = [t for t in threads if t["snoozedAt"]]
    assert decisions.count("harvested") == 1
    assert decisions.count("keep") >= 2
    assert sum(1 for t in threads if t.get("finishOnExit")) == 1
    assert any(overview.snooze_active(t, now) for t in snoozed)
    assert any(not overview.snooze_active(t, now) for t in snoozed)
    assert {t["filteredBy"] for t in threads if t["filteredBy"]} == {"zz-hidden*"}
    assert sum(1 for t in threads if t["classifiedAtMtime"] == t["mtime"]) >= 10


def _rooted(fixture):
    root = str(fixture.root)
    text = fixture.registry_path.read_text()
    for spelling in (root, slugify(root), droid_slug(root)):
        text = text.replace(spelling, "<root>")
    return text


def test_build_fixture_is_deterministic_for_a_seed(tmp_path):
    first = build_fixture(tmp_path / "a", threads=200, now=NOW)
    second = build_fixture(tmp_path / "b", threads=200, now=NOW)
    assert _rooted(first) == _rooted(second)


def test_build_fixture_with_transcripts_survives_a_real_scan(tmp_path, monkeypatch):
    fixture = build_fixture(tmp_path, threads=40, now=NOW, transcripts=True)
    for key, value in fixture.env.items():
        monkeypatch.setenv(key, value)
    now = NOW.isoformat()
    before = registry.load_registry(fixture.registry_path)

    after, note = refresh.auto_refresh(
        fixture.registry_path, now, persist=False, claude_projects_dir=tmp_path / "_claude"
    )

    assert note is None
    assert len(after["threads"]) == 40
    assert overview.triage_count(after["threads"], now) == overview.triage_count(
        before["threads"], now
    )
    assert len(registry.sessions_needing_classification(after)) == len(
        registry.sessions_needing_classification(before)
    )


def test_golden_helper_writes_the_file_under_update_goldens(request, tmp_path, monkeypatch):
    original = request.config.getoption

    def getoption(name, *args, **kwargs):
        return True if name == "--update-goldens" else original(name, *args, **kwargs)

    monkeypatch.setattr(request.config, "getoption", getoption)
    check = golden_checker(request, root=tmp_path)

    check("nested/shot", f"at {tmp_path}/x   \n\n\n", replace={str(tmp_path): "<root>"})

    assert (tmp_path / "nested" / "shot.txt").read_text() == "at <root>/x\n"


def _without_update(request, monkeypatch):
    original = request.config.getoption

    def getoption(name, *args, **kwargs):
        return False if name == "--update-goldens" else original(name, *args, **kwargs)

    monkeypatch.setattr(request.config, "getoption", getoption)


def test_golden_helper_names_the_update_command_when_the_file_is_missing(
    request, tmp_path, monkeypatch
):
    _without_update(request, monkeypatch)
    check = golden_checker(request, root=tmp_path)
    with pytest.raises(pytest.fail.Exception, match="--update-goldens"):
        check("absent", "text")


def test_golden_helper_shows_a_diff_on_mismatch(request, tmp_path, monkeypatch):
    _without_update(request, monkeypatch)
    (tmp_path / "shot.txt").write_text("old line\n")
    check = golden_checker(request, root=tmp_path)
    with pytest.raises(pytest.fail.Exception, match=r"-old line\n\+new line"):
        check("shot", "new line")
