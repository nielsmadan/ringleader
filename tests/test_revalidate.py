import os
import time

from ringleader import revalidate
from ringleader.presence import PresenceUnavailable
from ringleader.sources.base import stat_fingerprint

MTIME = "2026-06-01T00:00:00+00:00"


def _thread(tmp_path, sid="s1", *, age_seconds=600.0):
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text('{"type":"user"}\n')
    when = time.time() - age_seconds
    os.utime(transcript, (when, when))
    return {
        "sessionId": sid,
        "source": "claude-code",
        "file": str(transcript),
        "mtime": MTIME,
        "title": "fix the parser",
        "fileFingerprint": stat_fingerprint(transcript.stat()),
    }


def test_content_stamp_reads_size_and_mtime_ns():
    assert revalidate.content_stamp("1:2:300:400:5") == (300, 400)


def test_content_stamp_is_none_for_non_stat_fingerprints():
    assert [
        revalidate.content_stamp(value) for value in ("opencode:abc", "1:2:x:4:5", "", None)
    ] == [None, None, None, None]


def test_seen_captures_what_the_user_saw(tmp_path):
    thread = _thread(tmp_path)
    assert revalidate.seen(thread) == revalidate.Seen(
        ("claude-code", "s1"), "fix the parser", MTIME, thread["fileFingerprint"]
    )


def test_changed_reason_is_none_for_an_unchanged_thread(tmp_path):
    thread = _thread(tmp_path)
    assert revalidate.changed_reason(thread, revalidate.seen(thread)) is None


def test_changed_reason_names_a_gone_thread(tmp_path):
    expected = revalidate.seen(_thread(tmp_path))
    assert revalidate.changed_reason(None, expected) == "no longer in the registry"


def test_changed_reason_flags_a_new_registry_mtime(tmp_path):
    thread = _thread(tmp_path)
    expected = revalidate.seen(thread)
    moved = {**thread, "mtime": "2026-06-02T00:00:00+00:00"}
    assert revalidate.changed_reason(moved, expected) == "changed since you looked"


def test_changed_reason_flags_an_appended_transcript(tmp_path):
    thread = _thread(tmp_path)
    expected = revalidate.seen(thread)
    with open(thread["file"], "a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant"}\n')
    assert revalidate.changed_reason(thread, expected) == "changed since you looked"


def test_changed_reason_flags_a_missing_transcript(tmp_path):
    thread = _thread(tmp_path)
    expected = revalidate.seen(thread)
    os.unlink(thread["file"])
    assert revalidate.changed_reason(thread, expected) == "transcript is missing"


def test_running_reason_points_a_running_session_at_finish(tmp_path):
    thread = _thread(tmp_path)
    reason = revalidate.running_reason(thread, frozenset({("claude", "s1")}), "allow")
    assert reason == (
        "fix the parser is running — finish it from inside with the ringleader-finish skill"
    )


def test_running_reason_refuses_unknown_presence_when_asked(tmp_path):
    reason = revalidate.running_reason(_thread(tmp_path), None, "refuse")
    assert reason == (
        "can't tell whether fix the parser is running — "
        "install presence with rl data presence install"
    )


def test_running_reason_allows_unknown_presence_when_asked(tmp_path):
    assert revalidate.running_reason(_thread(tmp_path), None, "allow") is None


def test_running_reason_allows_an_idle_session(tmp_path):
    idle = frozenset({("claude", "other")})
    assert revalidate.running_reason(_thread(tmp_path), idle, "refuse") is None


def test_quiet_reason_flags_a_freshly_written_transcript(tmp_path):
    thread = _thread(tmp_path, age_seconds=5)
    assert revalidate.quiet_reason(thread, time.time(), 120) == "still being written"


def test_quiet_reason_passes_a_quiet_or_missing_transcript(tmp_path):
    quiet = _thread(tmp_path, "quiet", age_seconds=600)
    gone = _thread(tmp_path, "gone", age_seconds=5)
    os.unlink(gone["file"])
    now = time.time()
    assert [revalidate.quiet_reason(t, now, 120) for t in (quiet, gone)] == [None, None]


def test_probe_turns_an_unavailable_presence_into_none():
    def unavailable() -> frozenset[tuple[str, str]]:
        raise PresenceUnavailable("not installed")

    def broken() -> frozenset[tuple[str, str]]:
        raise OSError("boom")

    live = frozenset({("claude", "s1")})
    assert [revalidate.probe(p)() for p in (unavailable, broken, lambda: live)] == [
        None,
        None,
        live,
    ]
