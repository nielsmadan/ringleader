from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from ringleader.home.localzone import local_timezone, zone_from

FIXED = timezone(timedelta(hours=5, minutes=30))


def test_the_first_known_zone_name_wins_with_or_without_a_leading_colon():
    assert zone_from([":Europe/Berlin", "Asia/Tokyo"], FIXED) == ZoneInfo("Europe/Berlin")
    assert zone_from(["America/New_York"], FIXED) == ZoneInfo("America/New_York")


def test_unknown_and_empty_names_fall_through_to_the_next():
    assert zone_from(["", "Not/AZone", "CET-1CEST", "Asia/Tokyo"], FIXED) == ZoneInfo("Asia/Tokyo")


def test_without_a_known_name_the_fallback_is_used():
    assert zone_from(["", "Not/AZone"], FIXED) is FIXED


def test_tz_from_the_environment_names_the_local_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    assert local_timezone() == ZoneInfo("Pacific/Auckland")
