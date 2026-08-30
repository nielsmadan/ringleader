import json
import subprocess
import sys


def test_triage_json_is_valid_and_shaped(tmp_path):
    reg = tmp_path / "r.json"
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": "2026-08-01T00:00:00+00:00",
                "threads": [
                    {
                        "sessionId": "s1",
                        "status": "unclassified",
                        "mtime": "2026-08-01T00:00:00+00:00",
                        "project": "/p",
                        "whatItIs": "a thing",
                        "snoozedAt": None,
                        "decision": None,
                        "reviewedAtMtime": None,
                        "gitProjectLabel": "repo",
                    }
                ],
                "tombstones": [],
            }
        )
    )
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "ringleader",
            "triage",
            "--no-scan",
            "--registry",
            str(reg),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    data = json.loads(out)
    assert isinstance(data, list) and data[0]["session_id"] == "s1"
    assert data[0]["status"] == "unclassified"


def _two_thread_registry(tmp_path):
    reg = tmp_path / "r.json"
    reg.write_text(
        json.dumps(
            {
                "version": 1,
                "lastScan": "2026-08-01T00:00:00+00:00",
                "threads": [
                    {
                        "sessionId": "s1-unclassified",
                        "status": "unclassified",
                        "mtime": "2026-08-01T00:00:00+00:00",
                        "project": "/p",
                        "whatItIs": "a thing",
                        "snoozedAt": None,
                        "decision": None,
                        "reviewedAtMtime": None,
                        "gitProjectLabel": "repo",
                    },
                    {
                        "sessionId": "s2-active",
                        "status": "active",
                        "mtime": "2026-08-01T00:00:00+00:00",
                        "project": "/p",
                        "whatItIs": "another thing",
                        "snoozedAt": None,
                        "decision": None,
                        "reviewedAtMtime": None,
                        "gitProjectLabel": "repo",
                    },
                ],
                "tombstones": [],
            }
        )
    )
    return reg


def test_triage_json_unclassified_only_matches_text_population(tmp_path):
    reg = _two_thread_registry(tmp_path)

    json_out = subprocess.run(
        [
            sys.executable,
            "-m",
            "ringleader",
            "triage",
            "--no-scan",
            "--registry",
            str(reg),
            "-u",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    data = json.loads(json_out)
    assert [d["session_id"] for d in data] == ["s1-unclassified"]

    text_out = subprocess.run(
        [sys.executable, "-m", "ringleader", "triage", "--no-scan", "--registry", str(reg), "-u"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    # no title/suggestedName in the fixture, so the text renderer falls back to
    # sessionId[:8] for the displayed name (same fallback jsonout's "name" uses)
    assert "s1-uncla" in text_out
    assert "s2-activ" not in text_out

    # unfiltered --json still returns both, confirming -u is what narrowed it
    full_out = subprocess.run(
        [
            sys.executable,
            "-m",
            "ringleader",
            "triage",
            "--no-scan",
            "--registry",
            str(reg),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    full_data = json.loads(full_out)
    assert {d["session_id"] for d in full_data} == {"s1-unclassified", "s2-active"}


def test_triage_memory_json_rejected(tmp_path):
    reg = _two_thread_registry(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ringleader",
            "triage",
            "--no-scan",
            "--registry",
            str(reg),
            "--memory",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--json" in result.stderr
    assert "--memory" in result.stderr


def test_filtered_json_reports_only_filtered_sessions_with_their_pattern():
    from ringleader import jsonout

    now = "2026-08-02T00:00:00+00:00"
    threads = [
        {
            "sessionId": "noise",
            "title": "[cadet-city] reviewer",
            "status": "unclassified",
            "mtime": "2026-08-01T00:00:00+00:00",
            "project": "/p",
            "filteredBy": "[cadet-city]*",
        },
        {
            "sessionId": "real",
            "title": "real work",
            "status": "unfinished",
            "mtime": "2026-08-01T00:00:00+00:00",
            "project": "/p",
            "filteredBy": None,
        },
    ]

    rows = jsonout.filtered_json(threads, now)

    assert [(r["session_id"], r["filtered_by"]) for r in rows] == [("noise", "[cadet-city]*")]
