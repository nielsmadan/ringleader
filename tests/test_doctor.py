import json

from ringleader.commands import doctor


def test_reports_low_retention(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"cleanupPeriodDays": 7}))
    r = doctor.check_claude_retention(p)
    assert r["value"] == 7 and r["ok"] is False and "cleanupPeriodDays" in r["advice"]


def test_reports_ok_retention(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"cleanupPeriodDays": 90}))
    assert doctor.check_claude_retention(p)["ok"] is True


def test_missing_file_is_not_ok(tmp_path):
    r = doctor.check_claude_retention(tmp_path / "nope.json")
    assert r["value"] is None and r["ok"] is False
