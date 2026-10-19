from datetime import timedelta

import pytest
from ringleader.analysis.episodes import episodes_for
from ringleader.analysis.health import (
    PENALTIES,
    RECENCY_GUARD,
    HealthScore,
    Penalty,
    classify_outcome,
    fingerprint_material,
    health,
    thread_stats,
)
from ringleader.analysis.signals import SIGNAL_KINDS, detect

from .factories import START, calls, edit, facts, fail, request, stamp

LATER = START + timedelta(hours=2)


def test_running_session_is_open_regardless_of_last_event():
    assert classify_outcome("api_error", stamp(0), LATER, True) == "open"


def test_recency_guard_applies_only_without_presence():
    now = START + timedelta(minutes=9)
    assert classify_outcome("turn_complete", stamp(0), now, None) == "open"
    assert classify_outcome("turn_complete", stamp(0), now, False) == "completed"
    eleven = START + timedelta(minutes=11)
    assert classify_outcome("turn_complete", stamp(0), eleven, None) == "completed"


def test_a_session_quiet_for_exactly_the_recency_guard_is_no_longer_open():
    assert classify_outcome("turn_complete", stamp(0), START + RECENCY_GUARD, None) == "completed"


def test_missing_last_timestamp_is_unknown():
    assert classify_outcome("turn_complete", None, LATER, None) == "unknown"


@pytest.mark.parametrize(
    ("event", "outcome"),
    [
        ("api_error", "errored"),
        ("interrupt", "abandoned"),
        ("turn_aborted", "abandoned"),
        ("tool_pending", "abandoned"),
        ("user", "abandoned"),
        ("assistant_text", "completed"),
        ("turn_complete", "completed"),
        ("tool_result", "unknown"),
        ("none", "unknown"),
    ],
)
def test_outcome_follows_the_last_event(event, outcome):
    assert classify_outcome(event, stamp(0), LATER, False) == outcome


def test_health_caps_penalties_and_lists_the_breakdown():
    score = health(
        {"retry_run": 5, "rewind": 1, "tool_failure": 4, "permission_denial": 1},
        tool_calls=40,
        tool_errors=4,
        outcome="errored",
    )
    assert score == HealthScore(
        score=100 - 32,
        points=32,
        per_100_calls=32 * 100 / 40,
        penalties=(
            Penalty("retry_run", 5, 15),
            Penalty("outcome_errored", 1, 10),
            Penalty("rewind", 1, 3),
            Penalty("tool_error_rate", 4, 3),
            Penalty("permission_denial", 1, 1),
        ),
    )


def test_error_rate_needs_five_calls_and_is_capped():
    capped = health({}, tool_calls=5, tool_errors=5, outcome="abandoned")
    assert capped.penalties == (
        Penalty("tool_error_rate", 5, 15),
        Penalty("outcome_abandoned", 1, 5),
    )
    few = health({}, tool_calls=4, tool_errors=4, outcome="open")
    assert few == HealthScore(score=100, points=0, per_100_calls=0.0, penalties=())


def test_score_floors_at_zero():
    counts = {
        "runaway_window": 9,
        "retry_run": 9,
        "failure_streak": 9,
        "edit_churn": 9,
        "search_loop": 9,
        "unsupported_completion": 1,
        "user_interruption": 9,
    }
    score = health(counts, tool_calls=0, tool_errors=0, outcome="errored")
    assert (score.score, score.points, score.per_100_calls) == (0, 100, 10000.0)


def test_thread_stats_count_live_work_and_episode_tokens():
    same = fail("make", input_hash="same")
    transcript = facts(
        calls(edit("a.py"), same, same, same, edit("b.py", on_main_branch=False)),
        requests=(request(4, 1000),),
    )
    episodes = episodes_for(transcript, detect(transcript))
    stats = thread_stats(transcript, episodes)
    assert (stats.calls, stats.live_calls, stats.errors, stats.writes) == (5, 4, 3, 1)
    assert stats.episode_tokens == 1010


def test_thread_stats_errors_leave_out_user_caused_failures():
    same = fail("make", input_hash="same")
    rejected = fail("rm -rf x", category="user_rejected")
    denied = fail("cat /etc/shadow", category="permission_denied")
    transcript = facts(calls(edit("a.py"), same, same, rejected, denied))
    stats = thread_stats(transcript, episodes_for(transcript, detect(transcript)))
    assert (stats.calls, stats.errors) == (5, 2)


def test_every_signal_kind_has_a_penalty():
    assert set(SIGNAL_KINDS) <= set(PENALTIES)


def test_fingerprint_material_names_penalties():
    assert '"rewind": [3, 9]' in fingerprint_material()
