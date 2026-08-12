from ringleader import overview

NOW = "2026-06-10T00:00:00+00:00"


def _t(sid, mtime):
    return {"sessionId": sid, "mtime": mtime, "status": "unclassified", "project": "/p"}


def test_filter_recent_keeps_within_window():
    threads = [_t("new", "2026-06-09T00:00:00+00:00"), _t("old", "2026-06-01T00:00:00+00:00")]
    kept = overview.filter_recent(threads, NOW, 3)
    assert [t["sessionId"] for t in kept] == ["new"]


def test_filter_recent_none_returns_all():
    threads = [_t("a", "2026-01-01T00:00:00+00:00")]
    assert overview.filter_recent(threads, NOW, None) == threads
