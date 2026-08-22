from ringleader.usage.base import UsageRecord
from ringleader.usage.session import analyze


def _rec(i, inp=100, out=20, cr=0, cw=0):
    return UsageRecord(
        source="claude",
        session_id="s1",
        project="p",
        timestamp=f"2026-08-17T10:{i:02d}:00Z",
        provider=None,
        model="claude-opus-5",
        input=inp,
        output=out,
        cache_read=cr,
        cache_write=cw,
        reasoning=0,
        total=inp + out + cr + cw,
    )


def test_totals_and_turn_count():
    rep = analyze([_rec(0), _rec(1)])
    assert rep.turns == 2
    assert rep.totals.total == 240
    assert rep.session_id == "s1" and rep.source == "claude"


def test_cache_hit_rate():
    rep = analyze([_rec(0, inp=100, cr=300, cw=0)])
    assert rep.cache_hit_rate == 0.75


def test_cache_hit_rate_is_zero_without_input():
    rep = analyze([_rec(0, inp=0, out=0, cr=0, cw=0)])
    assert rep.cache_hit_rate == 0.0


def test_top_turns_are_ranked_by_total_desc():
    rep = analyze([_rec(0, out=10), _rec(1, out=900), _rec(2, out=50)])
    assert [i for i, _ in rep.top_turns][:2] == [1, 2]


def test_context_growth_tracks_input_plus_cache_read():
    rep = analyze([_rec(0, inp=10, cr=0), _rec(1, inp=10, cr=500)])
    assert rep.context_growth == [10, 510]


def test_empty_records_are_handled():
    rep = analyze([])
    assert rep.turns == 0 and rep.cache_hit_rate == 0.0 and rep.top_turns == []
