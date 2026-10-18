from datetime import date

from ringleader.usage.aggregate import Bucket, in_window, rollup
from ringleader.usage.base import UsageRecord, record_day
from ringleader.usage.pricing import estimate, model_family


def _rec(  # noqa: PLR0913, PLR0917 -- test data-builder helper, one arg per fixture field
    source="claude",
    session="s1",
    project="p",
    ts="2026-08-17T10:00:00Z",
    model="claude-opus-5",
    inp=100,
    out=20,
    cr=500,
    cw=50,
    reasoning=0,
    cost=None,
    provider=None,
):
    return UsageRecord(
        source=source,
        session_id=session,
        project=project,
        timestamp=ts,
        provider=provider,
        model=model,
        input=inp,
        output=out,
        cache_read=cr,
        cache_write=cw,
        reasoning=reasoning,
        total=inp + out + cr + cw,
        cost=cost,
    )


def test_model_family_maps_claude_models():
    assert model_family("claude-opus-5") == "opus"
    assert model_family("claude-sonnet-5") == "sonnet"
    assert model_family("gpt-5.6") is None


def test_estimate_prefers_on_disk_cost():
    assert estimate(_rec(source="pi", model="gpt-5.6", cost=0.25)) == 0.25


def test_estimate_computes_claude_price():
    got = estimate(_rec(inp=1_000_000, out=0, cr=0, cw=0))
    assert got == 5.0  # opus input list price is $5 / 1M tokens


def test_a_routed_provider_is_never_priced_at_the_model_vendors_rates():
    """droid routes to whatever model it likes and bills Factory credits, so Anthropic's list
    price for claude-opus-5 says nothing about what the session actually cost."""
    routed = _rec(source="droid", provider="factory", inp=1_000_000, out=0, cr=0, cw=0)
    assert estimate(routed) is None
    assert estimate(_rec(inp=1_000_000, out=0, cr=0, cw=0)) == 5.0


def test_a_routed_provider_still_reports_a_cost_it_recorded_itself():
    priced = _rec(source="droid", provider="factory", cost=0.5)
    assert estimate(priced) == 0.5


def test_estimate_is_none_for_unknown_model():
    assert estimate(_rec(source="codex", model=None, cost=None)) is None


def test_bucket_accumulates_every_split():
    b = Bucket()
    b.add(_rec())
    b.add(_rec(inp=1, out=2, cr=3, cw=4, reasoning=1))
    assert b.records == 2
    assert b.input == 101 and b.output == 22 and b.cache_read == 503
    assert b.cache_write == 54 and b.reasoning == 1
    assert b.total == 670 + 10


def test_rollup_groups_by_key():
    recs = [_rec(project="a"), _rec(project="b"), _rec(project="a")]
    out = rollup(recs, key=lambda r: r.project)
    assert sorted(out) == ["a", "b"]
    assert out["a"].records == 2


def test_record_day_and_in_window():
    r = _rec(ts="2026-08-17T10:00:00Z")
    assert record_day(r) == "2026-08-17"
    assert in_window(r, date(2026, 8, 10), None) is True
    assert in_window(r, date(2026, 8, 17), date(2026, 8, 17)) is True
    assert in_window(r, date(2026, 8, 18), None) is False
    assert in_window(r, None, date(2026, 8, 16)) is False


def test_model_family_keeps_the_mythos_alias_for_fable():
    assert model_family("claude-mythos-1") == "fable"
    assert model_family("claude-fable-2") == "fable"


def test_bucket_tracks_priced_and_unpriced_records():
    b = Bucket()
    b.add(_rec())
    b.add(_rec(source="codex", model=None))
    assert b.priced_records == 1
    assert b.unpriced_records == 1
    assert b.cost_partial is True
    assert b.cost_or_none == b.cost


def test_bucket_with_no_priced_records_has_no_cost():
    b = Bucket()
    b.add(_rec(source="codex", model=None))
    assert b.cost_or_none is None
    assert b.cost_partial is False
