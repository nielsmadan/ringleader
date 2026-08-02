import json

from ringleader.naming import title_line, unique_name


def test_title_line_exact_format():
    line = title_line("11111111-2222-3333-4444-555555555555", "login-button")
    obj = json.loads(line)
    assert obj == {
        "type": "custom-title",
        "customTitle": "login-button",
        "sessionId": "11111111-2222-3333-4444-555555555555",
    }
    assert "\n" not in line  # caller adds the newline


def test_unique_name_passes_through_when_free():
    assert (
        unique_name("login-button", "abcdef12-0000-0000-0000-000000000000", set()) == "login-button"
    )


def test_unique_name_suffixes_on_collision():
    taken = {"login-button"}
    out = unique_name("login-button", "abcdef12-3456-0000-0000-000000000000", taken)
    assert out == "login-button-abcdef"  # first 6 hex of sessionId, dashes stripped


def test_unique_name_double_collision_falls_through():
    # Both "thing" and "thing-abcdef" are taken; the result must not be in taken.
    taken = {"thing", "thing-abcdef"}
    out = unique_name("thing", "abcdef12-3456-0000-0000-000000000000", taken)
    assert out not in taken


from ringleader.naming import candidate_kind

NOW = "2026-06-30T00:00:00+00:00"
OLD = "2026-06-01T00:00:00+00:00"  # 29 days old → not active


def _thread(sid="s1", mtime=OLD, title=None, applied=None, suggested="thing-x", status="finished"):
    return {
        "sessionId": sid,
        "mtime": mtime,
        "title": title,
        "appliedName": applied,
        "suggestedName": suggested,
        "status": status,
    }


def test_kind_new_when_no_title():
    assert candidate_kind(_thread(title=None), NOW) == "new"


def test_kind_rename_when_our_name_drifted():
    t = _thread(title="old-name", applied="old-name", suggested="new-name")
    assert candidate_kind(t, NOW) == "rename"


def test_kind_none_when_user_owned_title():
    # title set, but not equal to appliedName → user named it
    assert candidate_kind(_thread(title="mine", applied=None, suggested="x"), NOW) is None
    assert candidate_kind(_thread(title="mine", applied="ours", suggested="x"), NOW) is None


def test_kind_none_when_converged():
    t = _thread(title="name", applied="name", suggested="name")
    assert candidate_kind(t, NOW) is None


def test_kind_none_when_unclassified_or_no_suggested():
    assert candidate_kind(_thread(status="unclassified"), NOW) is None
    assert candidate_kind(_thread(suggested=None), NOW) is None


def test_kind_none_when_active():
    # mtime == now → age 0 → within active window → skip
    assert candidate_kind(_thread(mtime=NOW, title=None), NOW) is None


from ringleader.naming import select_eligible


def _reg(threads):
    return {"version": 1, "lastScan": None, "threads": threads, "tombstones": []}


def test_select_returns_new_and_rename_ordered_by_mtime():
    older = _thread("a", mtime="2026-06-01T00:00:00+00:00", title=None, suggested="alpha")
    newer = _thread(
        "b", mtime="2026-06-10T00:00:00+00:00", title="old-b", applied="old-b", suggested="beta"
    )
    reg = _reg([newer, older])  # registry order shuffled
    out = select_eligible(reg, NOW)
    assert [(t["sessionId"], name, kind) for t, name, kind in out] == [
        ("a", "alpha", "new"),  # older first
        ("b", "beta", "rename"),
    ]


def test_select_suffixes_colliding_names():
    a = _thread(
        "aaaaaaaa-1111-0000-0000-000000000000",
        mtime="2026-06-01T00:00:00+00:00",
        title=None,
        suggested="dup",
    )
    b = _thread(
        "bbbbbbbb-2222-0000-0000-000000000000",
        mtime="2026-06-02T00:00:00+00:00",
        title=None,
        suggested="dup",
    )
    out = select_eligible(_reg([a, b]), NOW)
    names = [name for _, name, _ in out]
    assert names == ["dup", "dup-bbbbbb"]  # second collides → suffixed


def test_select_skips_user_owned_and_converged():
    user = _thread("u", title="mine", applied=None, suggested="x")
    conv = _thread("c", title="name", applied="name", suggested="name")
    out = select_eligible(_reg([user, conv]), NOW)
    assert out == []


def test_rename_reconverges_after_apply():
    # before: our name drifted → rename candidate
    t = _thread("s", title="old", applied="old", suggested="new")
    assert select_eligible(_reg([t]), NOW)[0][2] == "rename"
    # simulate apply_name: title + appliedName become the new name
    t["title"] = "new"
    t["appliedName"] = "new"
    assert select_eligible(_reg([t]), NOW) == []  # converged → nothing to do
