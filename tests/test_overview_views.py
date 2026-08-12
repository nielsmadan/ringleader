from ringleader import jsonout

NOW = "2026-06-10T00:00:00+00:00"


def _t(sid, **o):
    t = {
        "sessionId": sid,
        "mtime": "2026-06-09T00:00:00+00:00",
        "status": "active",
        "project": "/p",
        "snoozedAt": None,
        "snoozedUntil": None,
    }
    t.update(o)
    return t


def test_sessions_json_shape():
    rows = jsonout.sessions_json([_t("s1")], NOW)
    assert rows[0]["session_id"] == "s1" and "age_days" in rows[0]


def test_snoozed_json_only_active_with_until():
    threads = [
        _t("awake"),
        _t(
            "napping",
            snoozedAt="2026-06-01T00:00:00+00:00",
            snoozedUntil="2026-06-20T00:00:00+00:00",
        ),
        _t(
            "expired",
            snoozedAt="2026-06-01T00:00:00+00:00",
            snoozedUntil="2026-06-05T00:00:00+00:00",
        ),
    ]
    rows = jsonout.snoozed_json(threads, NOW)
    assert [r["session_id"] for r in rows] == ["napping"]
    assert rows[0]["snoozed_until"] == "2026-06-20T00:00:00+00:00"
