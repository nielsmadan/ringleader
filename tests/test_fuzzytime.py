import datetime as dt

import pytest
from ringleader.fuzzytime import parse_duration

NOW = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)  # a Thursday


@pytest.mark.parametrize(
    "text,expected_prefix",
    [
        ("2h", "2026-01-01T14:00"),
        ("3d", "2026-01-04T12:00"),
        ("2w", "2026-01-15T12:00"),
        ("7", "2026-01-08T12:00"),
        ("tomorrow", "2026-01-02"),
    ],
)
def test_parse_duration(text, expected_prefix):
    assert parse_duration(text, NOW).startswith(expected_prefix)


def test_next_monday_is_in_the_future():
    out = parse_duration("next monday", NOW)
    assert out > NOW.isoformat()


def test_invalid_raises():
    with pytest.raises(ValueError):
        parse_duration("banana", NOW)
