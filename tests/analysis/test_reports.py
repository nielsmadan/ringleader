import os
import stat
from datetime import UTC, datetime, timedelta

import pytest
from ringleader import paths
from ringleader.analysis import reports

NOW = datetime(2026, 10, 18, 14, 32, 5, tzinfo=UTC)
BODY = "# Tool retry loops in juggler\n\nmake was re-run unchanged.\n"


def save(body=BODY, now=NOW, kind="findings", **kw):
    return reports.save_report(
        kind,
        body,
        now,
        title=kw.get("title"),
        window=kw.get("window"),
        based_on=kw.get("based_on", ()),
    )


def test_filename_format():
    assert (
        reports.report_filename("findings", NOW, "Tool retry loops in juggler", 1)
        == "2026-10-18T1432-findings-tool-retry-loops-in-juggler.md"
    )


def test_filename_suffixes_later_attempts_and_truncates_the_slug():
    name = reports.report_filename("recommendations", NOW, "x" * 80, 3)
    assert name == f"2026-10-18T1432-recommendations-{'x' * 40}-3.md"


def test_filename_falls_back_to_the_kind_for_an_unsluggable_title():
    assert (
        reports.report_filename("findings", NOW, "???", 1) == "2026-10-18T1432-findings-findings.md"
    )


def test_save_round_trips_the_frontmatter():
    first = save(window="2026-10-11..2026-10-18")
    second = save(
        "# Fix\n\nedit CLAUDE.md\n",
        now=NOW + timedelta(minutes=5),
        kind="recommendations",
        based_on=[first.report_id],
    )
    text = second.path.read_text(encoding="utf-8")
    meta, body = reports.parse_frontmatter(text)
    assert meta == {
        "kind": "recommendations",
        "id": second.report_id,
        "created": "2026-10-18T14:37:05+00:00",
        "title": "Fix",
        "based_on": first.report_id,
    }
    assert body.strip() == "# Fix\n\nedit CLAUDE.md"
    assert first.window == "2026-10-11..2026-10-18"
    assert first.report_id == "2026-10-18T1432-findings-tool-retry-loops-in-juggler"


def test_save_strips_existing_frontmatter():
    meta = save("---\ntitle: old\n---\n# New heading\n\nbody\n")
    text = meta.path.read_text(encoding="utf-8")
    assert (text.count("---"), meta.title) == (2, "New heading")


def test_title_precedence_is_flag_then_heading_then_kind():
    assert save(title="Given").title == "Given"
    assert save("# Heading\n", now=NOW + timedelta(hours=1)).title == "Heading"
    assert save("no heading\n", now=NOW + timedelta(hours=2)).title == "findings"


def test_a_preexisting_file_gets_the_two_suffix():
    directory = paths.analysis_reports_dir("findings")
    directory.mkdir(parents=True)
    taken = directory / "2026-10-18T1432-findings-tool-retry-loops-in-juggler.md"
    taken.write_text("squatter", encoding="utf-8")
    meta = save()
    assert meta.path.name == "2026-10-18T1432-findings-tool-retry-loops-in-juggler-2.md"
    assert taken.read_text(encoding="utf-8") == "squatter"


def test_two_claims_in_a_row_are_distinct():
    directory = paths.analysis_reports_dir("findings")
    a = reports.claim_report_path(directory, "findings", NOW, "t")
    b = reports.claim_report_path(directory, "findings", NOW, "t")
    assert (a.name, b.name) == (
        "2026-10-18T1432-findings-t.md",
        "2026-10-18T1432-findings-t-2.md",
    )


def test_reports_are_private_and_land_under_the_state_dir():
    meta = save()
    directory = meta.path.parent
    assert directory == paths.analysis_dir() / "findings"
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(meta.path).st_mode) == 0o600


def test_unknown_based_on_raises_and_creates_nothing():
    with pytest.raises(ValueError, match="nope-1, nope-2"):
        save(kind="recommendations", based_on=["nope-1", "nope-2"])
    assert not paths.analysis_reports_dir("recommendations").exists()


def test_list_is_newest_first_and_kind_filters():
    old = save(now=NOW)
    mid = save("# Rec\n", now=NOW + timedelta(days=1), kind="recommendations")
    new = save("# Later\n", now=NOW + timedelta(days=2))
    assert [m.report_id for m in reports.list_reports(None, None)] == [
        new.report_id,
        mid.report_id,
        old.report_id,
    ]
    assert [m.report_id for m in reports.list_reports("findings", None)] == [
        new.report_id,
        old.report_id,
    ]
    assert [m.report_id for m in reports.list_reports(None, 1)] == [new.report_id]


def test_list_of_nothing_is_empty():
    assert reports.list_reports(None, None) == []


def test_list_skips_a_file_without_frontmatter():
    save()
    (paths.analysis_reports_dir("findings") / "stray.md").write_text("hi", encoding="utf-8")
    assert len(reports.list_reports(None, None)) == 1


def test_a_failed_write_leaves_no_claimed_file(monkeypatch):
    def boom(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(reports.stateio, "atomic_write_text", boom)
    with pytest.raises(OSError, match="disk full"):
        save()
    assert list(paths.analysis_reports_dir("findings").iterdir()) == []


def test_a_body_opening_with_a_rule_keeps_its_content():
    meta = save("---\nbody text\n---\nmore\n", title="T")
    _, body = reports.parse_frontmatter(meta.path.read_text(encoding="utf-8"))
    assert body.strip() == "---\nbody text\n---\nmore"


def test_a_blank_title_falls_back_to_the_heading():
    assert save("# Real\n", title="  ").title == "Real"


def test_a_body_of_only_frontmatter_is_refused():
    with pytest.raises(ValueError, match="report body is empty"):
        save("---\ntitle: x\n---\n")


def test_title_and_window_are_flattened_to_one_line():
    meta = save(title="a\n---\nkind: evil", window="w1\nid: x")
    parsed, _ = reports.parse_frontmatter(meta.path.read_text(encoding="utf-8"))
    assert (parsed["title"], parsed["window"], parsed["id"]) == (
        "a --- kind: evil",
        "w1 id: x",
        meta.report_id,
    )


def test_claiming_tightens_an_existing_directory_to_0700():
    directory = paths.analysis_reports_dir("findings")
    directory.mkdir(parents=True)
    directory.chmod(0o755)
    reports.claim_report_path(directory, "findings", NOW, "t")
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
