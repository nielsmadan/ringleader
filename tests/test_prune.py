from ringleader.prune import (
    prune_candidates, DEFAULT_MAX_SIZE_BYTES, DEFAULT_MAX_TURNS,
)

NOW = "2026-06-30T00:00:00+00:00"
OLD = "2026-06-23T00:00:00+00:00"        # exactly 7 days before NOW
RECENT = "2026-06-28T00:00:00+00:00"     # 2 days before NOW


def _t(sid, mtime=OLD, size=200_000, msgs=50, decision=None):
    return {"sessionId": sid, "mtime": mtime, "sizeBytes": size,
            "messageCount": msgs, "decision": decision, "status": "blocked",
            "title": None, "suggestedName": f"{sid}-name"}


def _reg(threads):
    return {"version": 1, "lastScan": NOW, "threads": threads, "tombstones": []}


def _ids(cands):
    return sorted(c["sessionId"] for c in cands)


def test_recent_is_never_a_candidate_even_if_tiny():
    reg = _reg([_t("a", mtime=RECENT, size=10, msgs=1)])
    assert prune_candidates(reg, NOW) == []


def test_age_boundary_exactly_threshold_is_included():
    # exactly 7 days old + tiny → included (>= threshold)
    reg = _reg([_t("a", mtime=OLD, size=10, msgs=1)])
    assert _ids(prune_candidates(reg, NOW)) == ["a"]


def test_small_by_size_or_by_turns_each_qualify():
    reg = _reg([
        _t("small_size", size=10_000, msgs=999),   # under size, many turns
        _t("few_turns", size=5_000_000, msgs=3),    # huge, but few turns
        _t("big", size=5_000_000, msgs=999),        # neither → excluded
    ])
    assert _ids(prune_candidates(reg, NOW)) == ["few_turns", "small_size"]


def test_thresholds_are_strict_less_than():
    reg = _reg([
        _t("at_size", size=DEFAULT_MAX_SIZE_BYTES, msgs=DEFAULT_MAX_TURNS),  # == both → not small
        _t("under_size", size=DEFAULT_MAX_SIZE_BYTES - 1, msgs=999),
    ])
    assert _ids(prune_candidates(reg, NOW)) == ["under_size"]


def test_keep_and_harvested_protected_other_decisions_eligible():
    reg = _reg([
        _t("keep", size=10, msgs=1, decision="keep"),
        _t("harvested", size=10, msgs=1, decision="harvested"),
        _t("abandon", size=10, msgs=1, decision="abandon"),
        _t("none", size=10, msgs=1, decision=None),
    ])
    assert _ids(prune_candidates(reg, NOW)) == ["abandon", "none"]


def test_custom_thresholds():
    reg = _reg([_t("a", size=80_000, msgs=999)])      # not tiny under defaults
    assert prune_candidates(reg, NOW) == []
    got = prune_candidates(reg, NOW, max_size_bytes=100_000)
    assert _ids(got) == ["a"]
