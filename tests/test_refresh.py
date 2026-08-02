import json

from ringleader import refresh


def _projects_dir(tmp, sid="s1"):
    """Minimal ~/.claude/projects layout: <encoded>/<uuid>.jsonl"""
    proj = tmp / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    (proj / f"{sid}.jsonl").write_text(
        f'{{"sessionId":"{sid}","cwd":"/Users/me/proj","type":"user"}}\n', encoding="utf-8"
    )
    return tmp / "projects"


def _registry(tmp, threads):
    p = tmp / "registry.json"
    p.write_text(
        json.dumps({"version": 1, "lastScan": None, "threads": threads, "tombstones": []}),
        encoding="utf-8",
    )
    return p


def test_delta_note_variants():
    assert refresh.delta_note(0, 0) is None
    assert refresh.delta_note(2, 0) == "(scanning… 2 new)"
    assert refresh.delta_note(0, 1) == "(scanning… 1 gone)"
    assert refresh.delta_note(2, 1) == "(scanning… 2 new, 1 gone)"


def test_auto_refresh_adds_new_session_and_saves(tmp_path):
    root = _projects_dir(tmp_path, "s1")
    reg_path = _registry(tmp_path, [])
    reg, note = refresh.auto_refresh(
        reg_path, "2026-07-19T00:00:00+00:00", projects_root=root, git_resolver=lambda cwd: None
    )
    assert note == "(scanning… 1 new)"
    assert [t["sessionId"] for t in reg["threads"]] == ["s1"]
    # persisted, not just returned
    saved = json.loads(reg_path.read_text())
    assert [t["sessionId"] for t in saved["threads"]] == ["s1"]
    assert saved["lastScan"] == "2026-07-19T00:00:00+00:00"


def test_auto_refresh_no_change_returns_none(tmp_path):
    root = _projects_dir(tmp_path, "s1")
    reg_path = _registry(tmp_path, [])
    refresh.auto_refresh(
        reg_path, "2026-07-19T00:00:00+00:00", projects_root=root, git_resolver=lambda cwd: None
    )
    _, note = refresh.auto_refresh(
        reg_path, "2026-07-19T00:01:00+00:00", projects_root=root, git_resolver=lambda cwd: None
    )
    assert note is None
