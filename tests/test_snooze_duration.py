from ringleader import overview, registry


def _reg(**over):
    t = {
        "sessionId": "s1",
        "mtime": "2026-06-01T00:00:00+00:00",
        "snoozedAt": None,
        "snoozedUntil": None,
    }
    t.update(over)
    return {"threads": [t]}


def test_snooze_active_indefinite():
    t = _reg(snoozedAt="2026-06-01T00:00:00+00:00")["threads"][0]
    assert overview.snooze_active(t, "2026-06-10T00:00:00+00:00") is True


def test_snooze_active_until_not_passed():
    t = _reg(snoozedAt="2026-06-01T00:00:00+00:00", snoozedUntil="2026-06-20T00:00:00+00:00")[
        "threads"
    ][0]
    assert overview.snooze_active(t, "2026-06-10T00:00:00+00:00") is True


def test_snooze_auto_wakes_after_until():
    t = _reg(snoozedAt="2026-06-01T00:00:00+00:00", snoozedUntil="2026-06-05T00:00:00+00:00")[
        "threads"
    ][0]
    assert overview.snooze_active(t, "2026-06-10T00:00:00+00:00") is False


def test_snooze_thread_stores_until():
    reg = _reg()
    registry.snooze_thread(
        reg, "s1", "2026-06-01T00:00:00+00:00", until="2026-06-08T00:00:00+00:00"
    )
    assert reg["threads"][0]["snoozedUntil"] == "2026-06-08T00:00:00+00:00"
