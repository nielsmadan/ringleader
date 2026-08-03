from ringleader.prune import (
    DEFAULT_MAX_TURNS,
    prune_candidates,
)

NOW = "2026-06-30T00:00:00+00:00"
OLD = "2026-06-23T00:00:00+00:00"  # exactly 7 days before NOW
RECENT = "2026-06-28T00:00:00+00:00"  # 2 days before NOW


def _t(sid, mtime=OLD, size=200_000, msgs=50, decision=None):
    return {
        "sessionId": sid,
        "mtime": mtime,
        "sizeBytes": size,
        "messageCount": msgs,
        "decision": decision,
        "status": "blocked",
        "title": None,
        "suggestedName": f"{sid}-name",
    }


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


def test_tininess_is_message_count_driven_not_size():
    # Sources vary wildly in bytes-per-message (e.g. Codex embeds full request/
    # response payloads), so size never gates eligibility — only messageCount does.
    reg = _reg(
        [
            _t("small_size_many_msgs", size=10_000, msgs=999),  # tiny bytes, too many msgs
            _t("huge_size_few_msgs", size=5_000_000, msgs=3),  # huge bytes, few msgs
        ]
    )
    assert _ids(prune_candidates(reg, NOW)) == ["huge_size_few_msgs"]


def test_message_count_threshold_is_strict():
    # Strict `<`, not `<=`: a session with exactly max_turns messages must NOT be
    # pruned, even at a large size — widening this to inclusive would make the new
    # (size-dropped) eligibility set bigger than the old OR-based one, which used a
    # strict `<` on the message-count path. That would newly delete real transcripts.
    reg = _reg(
        [
            _t("at_turns", msgs=DEFAULT_MAX_TURNS, size=5_000_000),  # == threshold → not small
            _t("under_turns", msgs=DEFAULT_MAX_TURNS - 1, size=5_000_000),  # under → small
            _t("over_turns", msgs=DEFAULT_MAX_TURNS + 1),  # over → not small
        ]
    )
    assert _ids(prune_candidates(reg, NOW)) == ["under_turns"]


def test_keep_and_harvested_protected_other_decisions_eligible():
    reg = _reg(
        [
            _t("keep", size=10, msgs=1, decision="keep"),
            _t("harvested", size=10, msgs=1, decision="harvested"),
            _t("abandon", size=10, msgs=1, decision="abandon"),
            _t("none", size=10, msgs=1, decision=None),
        ]
    )
    assert _ids(prune_candidates(reg, NOW)) == ["abandon", "none"]


def test_custom_thresholds():
    reg = _reg([_t("a", msgs=15)])  # not tiny under the default max_turns=12
    assert prune_candidates(reg, NOW) == []
    got = prune_candidates(reg, NOW, max_turns=20)
    assert _ids(got) == ["a"]
