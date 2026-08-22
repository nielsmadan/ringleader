from ringleader.usage.base import UsageRecord, WindowSnapshot, total_of


def test_total_of_sums_disjoint_buckets_and_excludes_reasoning():
    # reasoning is a subset of output and must never be added in
    assert total_of(input=100, cache_read=50, cache_write=10, output=20) == 180


def test_usage_record_defaults():
    r = UsageRecord(
        source="claude",
        session_id="s1",
        project="wrksp/foo",
        timestamp="2026-08-17T10:00:00Z",
        provider=None,
        model="claude-opus-5",
        input=10,
        output=5,
        cache_read=0,
        cache_write=0,
        reasoning=0,
        total=15,
    )
    assert r.cost is None
    assert r.machine is None


def test_window_snapshot_fields():
    w = WindowSnapshot(
        source="codex",
        timestamp="2026-08-17T21:06:26Z",
        plan_type="pro",
        window="weekly",
        window_minutes=10080,
        used_percent=47.0,
        resets_at="2026-08-24T12:09:14+00:00",
    )
    assert w.window == "weekly" and w.used_percent == 47.0
