import hashlib
from dataclasses import replace

from ringleader.analysis.episodes import (
    EXCERPT_CHARS,
    episode_anchor,
    episode_hash,
    episode_id,
    episodes_for,
)
from ringleader.analysis.signals import SignalHit, detect

from .factories import SID, bash, calls, edit, facts, fail, message, request, stamp

RETRY = fail("make", input_hash="same")


def retry_transcript(*extra):
    return facts(calls(bash("ls"), RETRY, RETRY, RETRY, *extra))


def test_overlapping_retry_run_and_failure_streak_merge_into_one_episode():
    transcript = retry_transcript(bash("pwd"))
    [episode] = episodes_for(transcript, detect(transcript))
    assert episode.kinds == ("failure_streak", "retry_run")
    assert (episode.start_seq, episode.end_seq, episode.calls) == (1, 3, 3)
    assert (episode.start_line, episode.end_line) == (4, 8)
    assert episode.severity == 5 + 5 + 3
    assert (episode.session_id, episode.thread_key, episode.agent) == (SID, SID, "claude-code")


def test_touching_stretches_merge_and_point_hits_do_not_make_episodes():
    transcript = facts(calls(*(bash(f"c{i}") for i in range(8))))
    hits = (
        SignalHit("retry_run", 0, 2, ""),
        SignalHit("search_loop", 3, 5, ""),
        SignalHit("tool_failure", 7, 7, ""),
    )
    [episode] = episodes_for(transcript, hits)
    assert (episode.start_seq, episode.end_seq, episode.kinds) == (
        0,
        5,
        ("retry_run", "search_loop"),
    )


def test_episode_id_is_stable_after_appending_calls():
    short = retry_transcript()
    longer = retry_transcript(bash("pwd"), bash("ls -la"), fail("other"))
    [before] = episodes_for(short, detect(short))
    [after] = episodes_for(longer, detect(longer))
    assert after.episode_id == before.episode_id
    assert before.anchor == "t1"
    expected = hashlib.sha256(f"claude-code|{SID}|t1".encode()).hexdigest()[:16]
    assert before.episode_id == expected == episode_id("claude-code", SID, "t1")


def test_hash_changes_when_a_call_inside_changes_status():
    original = retry_transcript()
    changed_calls = list(original.tool_calls)
    changed_calls[2] = replace(changed_calls[2], status="ok", error_category=None)
    changed = replace(original, tool_calls=tuple(changed_calls))
    [before] = episodes_for(original, detect(original))
    [after] = episodes_for(changed, detect(changed))
    assert after.episode_id == before.episode_id
    assert after.content_hash != before.content_hash
    assert after.content_hash == episode_hash(changed.tool_calls[1:4])


def test_shared_or_empty_call_id_anchors_on_seq():
    tool_calls = calls(bash("a", call_id="fc"), bash("b", call_id="fc"), bash("c", call_id=""))
    assert episode_anchor(tool_calls, tool_calls[1]) == "seq:1"
    assert episode_anchor(tool_calls, tool_calls[2]) == "seq:2"
    unique = calls(bash("a"), bash("b"))
    assert episode_anchor(unique, unique[1]) == "t1"


def test_episode_tokens_sum_requests_inside_the_line_span():
    transcript = retry_transcript()
    transcript = replace(
        transcript,
        requests=(request(2, 100), request(4, 200), request(8, 300), request(9, 400)),
    )
    [episode] = episodes_for(transcript, detect(transcript))
    assert episode.tokens == (200 + 10) + (300 + 10)


def test_excerpt_leads_with_the_last_human_message_and_lists_calls():
    transcript = replace(
        retry_transcript(),
        user_messages=(
            message(0, "first ask"),
            message(1, "build it please"),
            message(1, "agent noise", origin="agent"),
        ),
    )
    [episode] = episodes_for(transcript, detect(transcript))
    assert episode.excerpt.splitlines() == [
        "user: build it please",
        "#1 Bash shell make -> error [exit_code] Exit code 1",
        "#2 Bash shell make -> error [exit_code] Exit code 1",
        "#3 Bash shell make -> error [exit_code] Exit code 1",
    ]


def test_excerpt_is_capped():
    long = fail("make " + "x" * 400, input_hash="same", error_text="boom " * 100)
    transcript = facts(calls(*([long] * 40)))
    [episode] = episodes_for(transcript, detect(transcript))
    assert len(episode.excerpt) == EXCERPT_CHARS
    assert episode.calls == 40


def test_episode_id_changes_when_a_later_hit_extends_the_stretch_backwards():
    streak = [edit("a.py"), edit("a.py"), fail("x"), fail("y"), fail("z")]
    short = facts(calls(*streak))
    longer = facts(calls(*streak, edit("a.py")))
    [before] = episodes_for(short, detect(short))
    [after] = episodes_for(longer, detect(longer))
    assert (before.anchor, after.anchor) == ("t2", "t0")
    assert after.episode_id != before.episode_id


def test_episode_tokens_skip_abandoned_branch_requests():
    transcript = replace(
        retry_transcript(),
        requests=(request(5, 100), replace(request(6, 1000), on_main_branch=False)),
    )
    [episode] = episodes_for(transcript, detect(transcript))
    assert episode.tokens == 110


def plain(size, **timestamps):
    specs = (bash(f"c{i}", timestamp=timestamps.get(f"t{i}", stamp(i * 10))) for i in range(size))
    return facts(calls(*specs))


def test_touching_hits_more_than_thirty_calls_apart_split_into_two_episodes():
    transcript = plain(60)
    hits = (SignalHit("search_loop", 0, 40, ""), SignalHit("failure_streak", 31, 33, ""))
    first, second = episodes_for(transcript, hits)
    assert (first.start_seq, first.end_seq, first.kinds) == (0, 30, ("search_loop",))
    assert (second.start_seq, second.end_seq) == (31, 40)
    assert second.kinds == ("failure_streak", "search_loop")
    assert (first.anchor, second.anchor) == ("t0", "t31")


def test_hits_thirty_calls_apart_stay_one_episode():
    transcript = plain(60)
    hits = (SignalHit("search_loop", 0, 40, ""), SignalHit("failure_streak", 30, 32, ""))
    [episode] = episodes_for(transcript, hits)
    assert (episode.start_seq, episode.end_seq) == (0, 40)


def test_hits_more_than_thirty_minutes_apart_split():
    hits = (SignalHit("retry_run", 0, 2, ""), SignalHit("edit_churn", 2, 4, ""))
    late = plain(6, t2=stamp(1801), t3=stamp(1811), t4=stamp(1821), t5=stamp(1831))
    assert [(e.start_seq, e.end_seq) for e in episodes_for(late, hits)] == [(0, 1), (2, 4)]
    on_time = plain(6, t2=stamp(1800), t3=stamp(1811), t4=stamp(1821), t5=stamp(1831))
    assert [(e.start_seq, e.end_seq) for e in episodes_for(on_time, hits)] == [(0, 4)]


def test_split_episode_ids_are_deterministic_and_survive_appended_calls():
    hits = (SignalHit("search_loop", 0, 40, ""), SignalHit("failure_streak", 31, 33, ""))
    once = [e.episode_id for e in episodes_for(plain(45), hits)]
    again = [e.episode_id for e in episodes_for(plain(80), hits)]
    assert once == again
    assert once == [episode_id("claude-code", SID, "t0"), episode_id("claude-code", SID, "t31")]
