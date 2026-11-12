from datetime import UTC, datetime, timedelta

from ringleader import decisions, registry
from ringleader.decisions import Outcome
from ringleader.devtools.tui_fixture import build_fixture
from ringleader.home.app import InboxApp
from ringleader.home.intents import AgentHandoffIntent
from ringleader.home.screens.projects import ProjectsScreen
from ringleader.home.screens.triage import TriageScreen
from ringleader.home.state import ProjectsView
from ringleader.resume import launchable_agents
from ringleader.tui.snapshot import screen_text
from textual.widgets import OptionList, Static, Tree

from tui_helpers import drawn_text, until

NOW_DT = datetime(2026, 10, 15, 12, tzinfo=UTC)


def _iso(days):
    return (NOW_DT - timedelta(days=days)).isoformat()


def _thread(sid, *, days, project, status="unclassified", **extra):
    return {
        "sessionId": sid,
        "source": "claude-code",
        "file": f"/nowhere/{sid}.jsonl",
        "project": project,
        "title": f"title {sid}",
        "mtime": _iso(days),
        "status": status,
        "decision": None,
        **extra,
    }


def _write(path, *threads):
    reg = {**registry.empty_registry(), "threads": list(threads), "lastRoundupAt": _iso(0.1)}
    registry.save_registry(path, reg)
    return path


def _reload(path, now_iso):
    return registry.load_registry(path), None


def _execute(op, ctx):
    return Outcome(ok=True, message="", wrote=False)


def _app(path, *, execute=_execute, home_dir="/home/nobody"):
    return InboxApp(
        path,
        refresher=_reload,
        presence=frozenset,
        now=lambda: NOW_DT,
        models=lambda task: None,
        execute=execute,
        cwd=path.parent,
        strict=True,
        tz=UTC,
        home_dir=home_dir,
    )


def _project_ids(app):
    rows = app.screen.query_one("#projects", OptionList)
    return [rows.get_option_at_index(i).id for i in range(rows.option_count)]


async def _open_projects(app, pilot):
    await until(pilot, lambda: app.idle)
    await pilot.press("2")
    await until(pilot, lambda: "Projects · " in screen_text(app))


async def test_projects_are_listed_oldest_activity_first(tmp_path):
    path = _write(
        tmp_path / "registry.json",
        _thread("a1", days=2, project="/w/newer"),
        _thread("b1", days=30, project="/w/older"),
        _thread("b2", days=1, project="/w/older", status="finished"),
        _thread("c1", days=10, project="/w/middle"),
    )
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_projects(app, pilot)
        assert _project_ids(app) == ["/w/older", "/w/middle", "/w/newer"]
        text = screen_text(app)
        assert "Projects · Repos — 4 waiting across 3 projects" in text
        assert "/w/older" in text
        assert "2 waiting" in text


async def test_enter_opens_that_projects_triage(tmp_path):
    path = _write(
        tmp_path / "registry.json",
        _thread("a1", days=2, project="/w/newer"),
        _thread("b1", days=30, project="/w/older"),
    )
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_projects(app, pilot)
        await pilot.press("down", "enter")
        await until(pilot, lambda: isinstance(app.screen, TriageScreen))
        await until(pilot, lambda: "/w/newer · 1 waiting" in screen_text(app))
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, ProjectsScreen))


async def test_t_hands_the_project_to_an_agent_in_its_directory(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    path = _write(
        tmp_path / "registry.json",
        _thread("a1", days=2, project=str(project), gitProjectKey="github.com/me/proj"),
    )
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_projects(app, pilot)
        await pilot.press("t")
        await until(pilot, lambda: "with which agent?" in screen_text(app))
        await pilot.press("enter")
        await until(pilot, lambda: app._exit or not app.is_running)
    intent = app.return_value
    assert isinstance(intent, AgentHandoffIntent)
    assert (intent.agent, intent.cwd, intent.purpose) == (
        launchable_agents()[0],
        str(project),
        "triage",
    )
    assert "ringleader-triage" in intent.prompt
    assert "rl prune --project github.com/me/proj" in intent.prompt


async def test_golden_projects(tmp_path, golden):
    fixture = build_fixture(tmp_path, threads=40, now=NOW_DT)
    app = _app(fixture.registry_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_projects(app, pilot)
        golden("inbox/projects", await drawn_text(pilot, app), replace={str(tmp_path): "<root>"})


def _toasts(app):
    return [n.message for n in app._notifications]


def _title(app):
    return app.screen.query_one("#projects-title", Static).render().plain


def _triage_ids(app):
    rows = app.screen.query_one("#rows", OptionList)
    return [rows.get_option_at_index(i).id for i in range(rows.option_count)]


def _tree(app):
    return app.screen.query_one("#tree", Tree)


def _tree_lines(app):
    tree = _tree(app)
    lines = []
    pending = list(reversed(tree.root.children))
    while pending:
        node = pending.pop()
        lines.append(node.data.name)
        if node.is_expanded:
            pending.extend(reversed(node.children))
    return lines


def _cursor_path(app):
    return _tree(app).cursor_node.data.path


def _tree_threads():
    return (
        _thread("x", days=3, project="/w/a/x"),
        _thread("y", days=4, project="/w/a/y"),
        _thread("b", days=5, project="/w/b"),
        _thread("z", days=6, project="/v/z"),
    )


async def _open_view(app, pilot, view):
    await _open_projects(app, pilot)
    for _ in range(len(ProjectsView)):
        if f"Projects · {view} " in _title(app):
            break
        await pilot.press("tab")
        await pilot.pause()
    assert f"Projects · {view} " in _title(app)


async def test_tab_cycles_the_views_and_shift_tab_goes_back(tmp_path):
    path = _write(tmp_path / "registry.json", *_tree_threads())
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_projects(app, pilot)
        assert "tab next view" in screen_text(app)
        titles = [_title(app)]
        for _ in range(4):
            await pilot.press("tab")
            await pilot.pause()
            titles.append(_title(app))
        assert titles == [
            "Projects · Repos — 4 waiting across 4 projects",
            "Projects · Folders — 4 waiting across 4 folders",
            "Projects · Tree — 4 waiting across 4 folders",
            "Projects · Dates — 4 waiting across 4 periods",
            "Projects · Repos — 4 waiting across 4 projects",
        ]
        await pilot.press("shift+tab")
        await until(pilot, lambda: _title(app).startswith("Projects · Dates"))


async def test_reopening_projects_shows_the_last_view_used(tmp_path):
    path = _write(tmp_path / "registry.json", *_tree_threads())
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Dates")
        await pilot.press("escape", "2")
        await until(pilot, lambda: _title(app).startswith("Projects · Dates"))


async def test_the_folders_view_opens_one_folders_triage(tmp_path):
    path = _write(tmp_path / "registry.json", *_tree_threads())
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Folders")
        assert _project_ids(app) == ["/v/z", "/w/a/x", "/w/a/y", "/w/b"]
        await pilot.press("down", "enter")
        await until(pilot, lambda: isinstance(app.screen, TriageScreen))
        assert _triage_ids(app) == ["claude-code:x"]


async def test_a_long_folder_keeps_the_end_of_its_path(tmp_path):
    deep = "/w/" + "/".join(f"level-{n}" for n in range(8)) + "/project"
    path = _write(tmp_path / "registry.json", _thread("d", days=2, project=deep))
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Folders")
        row = _project_ids(app).index(deep)
        shown = app.screen.query_one("#projects", OptionList).get_option_at_index(row).prompt
        assert shown.plain.startswith("…l-4/level-5/level-6/level-7/project ")


async def test_the_tree_starts_two_levels_open_and_expands_and_collapses(tmp_path):
    path = _write(tmp_path / "registry.json", *_tree_threads())
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Tree")
        assert _tree_lines(app) == ["/", "v/z", "w", "a", "b"]
        assert "/  4" in screen_text(app)
        await pilot.press("j", "j", "j")
        await until(pilot, lambda: _cursor_path(app) == "/w/a")
        await pilot.press("l")
        await until(pilot, lambda: _tree_lines(app) == ["/", "v/z", "w", "a", "x", "y", "b"])
        await pilot.press("j", "left")
        await until(pilot, lambda: _cursor_path(app) == "/w/a")
        await pilot.press("h")
        await until(pilot, lambda: _tree_lines(app) == ["/", "v/z", "w", "a", "b"])
        await pilot.press("right")
        await until(pilot, lambda: "x" in _tree_lines(app))


async def test_left_on_a_collapsed_folder_moves_to_its_parent(tmp_path):
    path = _write(tmp_path / "registry.json", *_tree_threads())
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Tree")
        await pilot.press("j", "j", "j", "h")
        await until(pilot, lambda: _cursor_path(app) == "/w")


async def test_left_on_a_later_root_stays_on_it(tmp_path):
    path = _write(
        tmp_path / "registry.json",
        _thread("z", days=6, project="/v/z"),
        _thread("u", days=6, project=None),
    )
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Tree")
        await pilot.press("j", "h")
        await pilot.pause()
        assert _cursor_path(app) == "(unknown)"


async def test_the_tree_keeps_the_cursor_and_toggles_after_a_decision(tmp_path):
    threads = (*_tree_threads(), _thread("b2", days=7, project="/w/b"))
    path = _write(tmp_path / "registry.json", *threads)
    app = _app(path, execute=decisions.execute)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Tree")
        await pilot.press("j", "j", "j", "l", "j", "j", "j")
        await until(pilot, lambda: _cursor_path(app) == "/w/b")
        await pilot.press("enter")
        await until(pilot, lambda: _triage_ids(app) == ["claude-code:b2", "claude-code:b"])
        await pilot.press("K")
        await until(pilot, lambda: _triage_ids(app) == ["claude-code:b"])
        await pilot.press("escape")
        await until(pilot, lambda: "b  1" in screen_text(app))
        assert _tree_lines(app) == ["/", "v/z", "w", "a", "x", "y", "b"]
        await until(pilot, lambda: _cursor_path(app) == "/w/b")


async def test_finishing_a_tree_folder_puts_the_cursor_on_the_folder_around_it(tmp_path):
    threads = (*_tree_threads(), _thread("c", days=8, project="/w/c"))
    path = _write(tmp_path / "registry.json", *threads)
    app = _app(path, execute=decisions.execute)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Tree")
        await pilot.press("j", "j", "j", "j")
        await until(pilot, lambda: _cursor_path(app) == "/w/b")
        await pilot.press("enter")
        await until(pilot, lambda: _triage_ids(app) == ["claude-code:b"])
        await pilot.press("K")
        await until(pilot, lambda: "/w/b done." in screen_text(app))
        await pilot.press("escape")
        await until(pilot, lambda: _tree_lines(app) == ["/", "v/z", "w", "a", "c"])
        await until(pilot, lambda: _cursor_path(app) == "/w")


async def test_a_folder_pushed_under_a_closed_parent_leaves_the_cursor_on_the_parent(tmp_path):
    threads = [
        _thread("x", days=3, project="/w/a/x"),
        _thread("y", days=4, project="/w/a/y"),
        _thread("b", days=5, project="/w/b"),
    ]
    path = _write(tmp_path / "registry.json", *threads)
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Tree")
        assert _tree_lines(app) == ["/w", "a", "x", "y", "b"]
        await pilot.press("j", "j")
        await until(pilot, lambda: _cursor_path(app) == "/w/a/x")
        _write(path, *threads, _thread("z", days=6, project="/v/z"))
        await pilot.press("ctrl+r")
        await until(pilot, lambda: _tree_lines(app) == ["/", "v/z", "w", "a", "b"])
        await until(pilot, lambda: _cursor_path(app) == "/w/a")


async def test_reopening_projects_keeps_the_folders_the_user_opened(tmp_path):
    path = _write(tmp_path / "registry.json", *_tree_threads())
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Tree")
        await pilot.press("j", "j", "j", "l")
        await until(pilot, lambda: "x" in _tree_lines(app))
        await pilot.press("escape", "2")
        await until(pilot, lambda: _title(app).startswith("Projects · Tree"))
        assert _tree_lines(app) == ["/", "v/z", "w", "a", "x", "y", "b"]


async def test_enter_on_a_tree_folder_triages_everything_under_it(tmp_path):
    path = _write(tmp_path / "registry.json", *_tree_threads())
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Tree")
        await pilot.press("j", "j")
        await until(pilot, lambda: _cursor_path(app) == "/w")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, TriageScreen))
        assert _triage_ids(app) == ["claude-code:b", "claude-code:y", "claude-code:x"]


async def test_a_dates_entry_triages_that_period_and_done_offers_the_next_one(tmp_path):
    path = _write(
        tmp_path / "registry.json",
        _thread("today", days=0.1, project="/w/a"),
        _thread("old", days=30, project="/w/b"),
    )
    app = _app(path, execute=decisions.execute)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Dates")
        assert _project_ids(app) == ["day:2026-10-15", "week:2026-09-14"]
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, TriageScreen))
        assert _triage_ids(app) == ["claude-code:today"]
        await pilot.press("K")
        await until(pilot, lambda: "2026-10-15 done." in screen_text(app))
        assert "Next: Triage 2026-09-14 (1 waiting) — enter" in screen_text(app)
        await pilot.press("enter")
        await until(pilot, lambda: _triage_ids(app) == ["claude-code:old"])


async def test_a_tree_triage_has_no_next(tmp_path):
    path = _write(tmp_path / "registry.json", _thread("z", days=6, project="/v/z"))
    app = _app(path, execute=decisions.execute)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Tree")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, TriageScreen))
        await pilot.press("K")
        await until(
            pilot, lambda: "Nothing next in the tree — escape for the tree" in screen_text(app)
        )


async def test_t_refuses_a_scope_that_spans_several_projects(tmp_path):
    path = _write(tmp_path / "registry.json", *_tree_threads())
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Tree")
        await pilot.press("t")
        await until(
            pilot, lambda: "a hand-off needs one project — / except ~ spans several" in _toasts(app)
        )
        assert isinstance(app.screen, ProjectsScreen)


async def test_t_names_a_tree_folder_by_its_path_when_refusing(tmp_path):
    path = _write(tmp_path / "registry.json", *_tree_threads())
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Tree")
        await pilot.press("j", "j", "t")
        await until(
            pilot, lambda: "a hand-off needs one project — /w spans several" in _toasts(app)
        )


async def test_t_refuses_a_dates_entry_that_spans_several_projects(tmp_path):
    path = _write(
        tmp_path / "registry.json",
        _thread("a", days=0.1, project="/w/a"),
        _thread("b", days=0.2, project="/w/b"),
    )
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Dates")
        await pilot.press("t")
        await until(
            pilot, lambda: "a hand-off needs one project — 2026-10-15 spans several" in _toasts(app)
        )


async def test_done_in_folders_offers_the_next_folder(tmp_path):
    path = _write(
        tmp_path / "registry.json",
        _thread("a", days=2, project="/w/a"),
        _thread("b", days=3, project="/w/b"),
    )
    app = _app(path, execute=decisions.execute)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Folders")
        await pilot.press("enter")
        await until(pilot, lambda: _triage_ids(app) == ["claude-code:a"])
        await pilot.press("K")
        await until(pilot, lambda: "Next: Triage /w/b (1 waiting) — enter" in screen_text(app))


async def test_t_on_a_single_project_folder_hands_that_project_off(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    path = _write(
        tmp_path / "registry.json",
        _thread("a1", days=2, project=str(project), gitProjectKey="github.com/me/proj"),
    )
    app = _app(path)
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Folders")
        await pilot.press("t")
        await until(pilot, lambda: "with which agent?" in screen_text(app))
        await pilot.press("enter")
        await until(pilot, lambda: app._exit or not app.is_running)
    assert "rl prune --project github.com/me/proj" in app.return_value.prompt


async def test_golden_projects_tree(tmp_path, golden):
    fixture = build_fixture(tmp_path, threads=40, now=NOW_DT)
    app = _app(fixture.registry_path, home_dir=str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await _open_view(app, pilot, "Tree")
        golden(
            "inbox/projects-tree", await drawn_text(pilot, app), replace={str(tmp_path): "<root>"}
        )
