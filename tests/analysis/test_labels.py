import re
import threading
import time
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from ringleader import enrichment
from ringleader.analysis import issues, labels, render, rollups, store
from ringleader.models.base import ModelError

from .factories import bash, fail, message
from .test_rollups import A, B, put, scope, specs

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
LABEL = {"category": "loop", "friction": "wrong_approach", "cause": "re-ran make unchanged"}


@pytest.fixture
def index(tmp_path):
    opened, _cold = store.open_index_for_write(tmp_path / "idx", NOW.isoformat())
    with closing(opened):
        yield opened


@pytest.fixture
def store_(tmp_path):
    with closing(store.open_labels(tmp_path / "labels.sqlite")) as opened:
        yield opened


class FakeModel:
    def __init__(self, respond, name="claude", model="haiku"):
        self.name = name
        self.model = model
        self.respond = respond
        self.prompts = []
        self._lock = threading.Lock()

    def available(self):
        return True

    def complete_json(self, prompt, schema):
        with self._lock:
            self.prompts.append(prompt)
        return self.respond(prompt, schema)


def sent_ids(prompt):
    return re.search(r"no others: ([^\n]*)", prompt).group(1).split(", ")


def label_everything(prompt, schema):
    if schema is labels.ISSUE_NAME_SCHEMA:
        return {"name": "make keeps failing", "summary": "The build fails the same way."}
    if schema is labels.MESSAGE_LABEL_SCHEMA:
        return {"labels": [{"id": i, "kind": "correction"} for i in sent_ids(prompt)]}
    return {"labels": [{"id": i, **LABEL} for i in sent_ids(prompt)]}


def failing(name, count=3, **extra):
    return specs(name, *(fail("make", **extra) for _ in range(count)))


def plan(index, store_, choice="recent", found=(), **options):
    chosen = {"engine": "claude", "model_name": "haiku", "concurrency": 4, **options}
    return labels.plan_labels(
        index, store_, scope(), choice=choice, issues=found, now=NOW, **chosen
    )


def run(model, planned, store_):
    return labels.run_labels(model, planned, store_, NOW, lambda: False)


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"stdin_tty": True, "stdout_tty": True}, ("recent", None)),
        (
            {"stdin_tty": True, "stdout_tty": True, "current_session": ("claude-code", "x")},
            ("none", labels.NON_INTERACTIVE_NOTE),
        ),
        ({"stdin_tty": False, "stdout_tty": False}, ("none", labels.NON_INTERACTIVE_NOTE)),
        ({"stdin_tty": True, "stdout_tty": True, "json_output": True}, ("none", labels.JSON_NOTE)),
        ({"stdin_tty": False, "stdout_tty": False, "explicit": "recent"}, ("recent", None)),
        ({"stdin_tty": True, "stdout_tty": True, "explicit": "none"}, ("none", None)),
        (
            {"stdin_tty": True, "stdout_tty": True, "stderr_tty": False},
            ("none", labels.NON_INTERACTIVE_NOTE),
        ),
    ],
)
def test_default_label_scope(options, expected):
    base = {"explicit": None, "json_output": False, "current_session": None, "stderr_tty": True}
    assert labels.default_label_scope(**{**base, **options}) == expected


def test_the_notes_name_the_flag_that_labels():
    assert labels.NON_INTERACTIVE_NOTE.endswith("pass --label recent to label")


def test_parse_batch_keeps_sent_ids_with_allowed_values():
    raw = {
        "labels": [
            {"id": "a", **LABEL},
            {"id": "b", **LABEL, "category": "chaos"},
            {"id": "stranger", **LABEL},
            {"id": "c", **LABEL, "friction": "none"},
            "junk",
        ]
    }
    allowed = {"category": labels.EPISODE_CATEGORIES, "friction": labels.FRICTION_TAGS}
    parsed = labels.parse_batch(raw, {"a", "b", "c"}, allowed)
    assert parsed == {"a": LABEL, "c": {**LABEL, "friction": "none"}}


def test_every_enum_value_is_defined_in_its_prompt():
    episode = labels.episode_prompt([])
    message_ = labels.message_prompt([])
    assert all(f"- {v}: " in episode for v in labels.EPISODE_CATEGORIES + labels.FRICTION_TAGS)
    assert all(f"- {v}: " in message_ for v in labels.MESSAGE_KINDS)


def test_a_planned_episode_is_labelled_and_joined_into_the_session(index, store_):
    put(index, "t", tool_calls=failing("t"))
    planned = plan(index, store_)
    outcome = run(FakeModel(label_everything), planned, store_)
    assert (outcome.episodes, outcome.failed, outcome.tripped) == (1, 0, None)
    detail = render.session_detail_json(
        rollups.session_detail(index, "claude-code", A, {}, NOW, None, store_)
    )
    assert detail["episodes"][0]["label"] == {**LABEL, "model": "haiku"}
    assert plan(index, store_).calls == 0


def test_an_omitted_id_is_retried_alone_and_both_labels_are_stored(index, store_):
    put(index, "a", tool_calls=failing("a"))
    put(index, "b", tool_calls=failing("b"))
    planned = plan(index, store_)
    dropped = planned.episodes[1].episode_id

    def respond(prompt, schema):
        ids = sent_ids(prompt)
        keep = [i for i in ids if i != dropped] if len(ids) > 1 else ids
        return {"labels": [{"id": i, **LABEL} for i in keep]}

    model = FakeModel(respond)
    outcome = run(model, planned, store_)
    assert [sent_ids(p) for p in model.prompts][1] == [dropped]
    assert (outcome.episodes, outcome.failed) == (2, 0)
    keys = [(e.episode_id, e.content_hash) for e in planned.episodes]
    assert set(labels.episode_labels(store_, keys)) == {e.episode_id for e in planned.episodes}


def test_an_id_the_model_never_returns_fails_after_two_attempts(index, store_):
    put(index, "a", tool_calls=failing("a"))
    model = FakeModel(lambda prompt, schema: {"labels": []})
    outcome = run(model, plan(index, store_), store_)
    assert (outcome.failed, len(model.prompts), outcome.episodes) == (1, labels.MAX_ATTEMPTS, 0)


def test_identical_model_errors_trip_the_breaker_and_cancel_queued_batches(index, store_):
    for i in range(40):
        put(index, f"e{i}", tool_calls=failing(f"e{i}"))
    planned = plan(index, store_, choice="all")

    def respond(prompt, schema):
        raise ModelError("quota exhausted")

    model = FakeModel(respond, name="llm")
    outcome = run(model, planned, store_)
    assert len(model.prompts) == enrichment.BREAKER_FAILURES
    assert outcome.tripped == "5 calls failed in a row: quota exhausted"
    assert outcome.failed == enrichment.BREAKER_FAILURES * labels.EPISODE_BATCH


class RecordingConnection:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.functions = []

    def create_function(self, name, *args, **kwargs):
        self.functions.append(name)
        return self.wrapped.create_function(name, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


def test_each_message_query_registers_its_own_owner_function(index):
    put(index, "a", session=A, user_messages=(message(1, "no, use pytest"),))
    put(index, "b", session=B, user_messages=(message(1, "stop"),))
    recording = RecordingConnection(index)
    first = labels._messages(recording, {("claude-code", A)}, (), None)
    second = labels._messages(recording, {("claude-code", B)}, (), None)
    assert [m[2] for m in first + second] == ["no, use pytest", "stop"]
    assert len(set(recording.functions)) == 2


def test_a_changed_call_status_replans_the_episode(index, store_):
    put(index, "t", tool_calls=specs("t", *([fail("make")] * 3), bash("make")))
    first = plan(index, store_)
    run(FakeModel(label_everything), first, store_)
    assert plan(index, store_).episodes == ()
    put(index, "t", tool_calls=failing("t", 4))
    again = plan(index, store_)
    assert [e.episode_id for e in again.episodes] == [first.episodes[0].episode_id]
    assert again.episodes[0].content_hash != first.episodes[0].content_hash


def test_a_bumped_prompt_version_replans_everything(index, store_, monkeypatch):
    put(index, "a", tool_calls=failing("a"))
    put(index, "b", tool_calls=failing("b"))
    run(FakeModel(label_everything), plan(index, store_), store_)
    assert plan(index, store_).episodes == ()
    monkeypatch.setattr(labels, "LABEL_PROMPT_VERSION", labels.LABEL_PROMPT_VERSION + 1)
    assert len(plan(index, store_).episodes) == 2


def test_recent_caps_at_fifty_by_severity_and_all_takes_everything(index, store_):
    for i in range(60):
        put(index, f"e{i}", tool_calls=failing(f"e{i}", 3 + (i == 59)))
    recent = plan(index, store_)
    assert len(recent.episodes) == labels.DEFAULT_LABEL_LIMIT
    assert recent.episodes[0].thread_key.endswith("/e59")
    assert recent.calls == labels.DEFAULT_LABEL_LIMIT // labels.EPISODE_BATCH + 1
    assert len(plan(index, store_, choice="all").episodes) == 60


def test_recent_keeps_the_last_seven_days_of_the_scope(index, store_):
    put(index, "old", tool_calls=failing("old"))
    put(index, "new", tool_calls=failing("new", timestamp="2026-10-15T10:00:00Z"))
    recent = plan(index, store_)
    assert [e.thread_key for e in recent.episodes] == [f"{A}/new"]


def test_none_plans_nothing(index, store_):
    put(index, "a", tool_calls=failing("a"))
    assert plan(index, store_, choice="none").calls == 0


def test_human_messages_without_a_rule_kind_are_planned_for_episode_sessions(index, store_):
    put(
        index,
        "a",
        tool_calls=failing("a"),
        user_messages=(message(1, "no, use pytest"), message(2, "x", is_interrupt=True)),
    )
    put(index, "quiet", session=B, tool_calls=specs("q", bash("ls")))
    planned = plan(index, store_)
    assert planned.messages == ((f"claude-code:{A}:u1", "c", "no, use pytest"),)
    outcome = run(FakeModel(label_everything), planned, store_)
    assert outcome.messages == 1
    detail = render.session_detail_json(
        rollups.session_detail(index, "claude-code", A, {}, NOW, None, store_)
    )
    assert detail["user_messages"]["counts"] == {"correction": 1}
    assert [m["kind"] for m in detail["user_messages"]["notable"]] == ["correction", "interrupt"]


def test_a_session_scope_labels_only_that_session_without_caps(index, store_):
    for i in range(55):
        put(index, f"e{i}", tool_calls=failing(f"e{i}"))
    put(index, "other", session=B, tool_calls=failing("other"))
    planned = plan(index, store_, session=("claude-code", A))
    assert len(planned.episodes) == 55
    assert {e.session_id for e in planned.episodes} == {A}


def test_all_lifts_the_message_and_issue_name_caps(index, store_, monkeypatch):
    monkeypatch.setattr(labels, "MESSAGE_LABEL_LIMIT", 1)
    monkeypatch.setattr(labels, "ISSUE_NAME_LIMIT", 1)
    asks = (message(0, "no, use pytest"), message(1, "and run it again"))
    put(index, "a", tool_calls=failing("a", signature="make|exit_code|boom"), user_messages=asks)
    put(index, "b", session=B, tool_calls=failing("b", signature="make|exit_code|boom"))
    put(index, "c", tool_calls=failing("c", signature="ls|exit_code|gone"))
    put(index, "d", session=B, tool_calls=failing("d", signature="ls|exit_code|gone"))
    found = issues.recurring_issues(
        index, scope(), embedder=None, names={}, min_sessions=2, limit=20
    )
    recent = plan(index, store_, found=found)
    everything = plan(index, store_, choice="all", found=found)
    assert (len(recent.messages), len(recent.issues)) == (1, 1)
    assert (len(everything.messages), len(everything.issues)) == (2, 2)


def test_unnamed_issues_are_named_once(index, store_):
    put(index, "a", tool_calls=failing("a", signature="make|exit_code|boom"))
    put(index, "b", session=B, tool_calls=failing("b", signature="make|exit_code|boom"))
    found = issues.recurring_issues(
        index, scope(), embedder=None, names={}, min_sessions=2, limit=20
    )
    planned = plan(index, store_, found=found)
    assert [i.issue_id for i in planned.issues] == [found[0].issue_id]
    outcome = run(FakeModel(label_everything), planned, store_)
    assert outcome.issues == 1
    assert labels.issue_names(store_) == {
        found[0].issue_id: ("make keeps failing", "The build fails the same way.")
    }
    assert plan(index, store_, found=found).issues == ()


def one_issue(index):
    put(index, "a", tool_calls=failing("a", signature="make|exit_code|boom"))
    put(index, "b", session=B, tool_calls=failing("b", signature="make|exit_code|boom"))
    return issues.recurring_issues(
        index, scope(), embedder=None, names={}, min_sessions=2, limit=20
    )


def name_issue(reply):
    def respond(prompt, schema):
        return reply if schema is labels.ISSUE_NAME_SCHEMA else label_everything(prompt, schema)

    return FakeModel(respond)


def test_an_issue_name_and_summary_are_cut_to_their_caps(index, store_):
    found = one_issue(index)
    model = name_issue({"name": "n" * 200, "summary": "s" * 500})
    run(model, plan(index, store_, found=found), store_)
    assert labels.issue_names(store_) == {
        found[0].issue_id: ("n" * labels.NAME_CHARS, "s" * labels.SUMMARY_CHARS)
    }


@pytest.mark.parametrize(
    "reply",
    [["make keeps failing"], {"name": "", "summary": "x"}, {"name": "x", "summary": ""}],
)
def test_an_issue_reply_without_both_texts_is_a_failure(index, store_, reply):
    found = one_issue(index)
    outcome = run(name_issue(reply), plan(index, store_, found=found), store_)
    assert (outcome.issues, outcome.failed) == (0, 1)
    assert outcome.errors == ("issue: no valid label for 1",)


def test_estimates_use_the_enrich_timing_and_haiku_prices(index, store_):
    for i in range(5):
        put(index, f"e{i}", tool_calls=failing(f"e{i}"))
    planned = plan(index, store_)
    assert planned.calls == 2
    assert planned.est_seconds == enrichment.estimate_seconds(2, "claude", 4)
    assert planned.est_cost == pytest.approx(2 * 3000 * 1e-6)
    unpriced = plan(index, store_, engine="codex", model_name=None)
    assert unpriced.est_cost is None


def test_rows_record_the_engine_model_and_prompt_version(index, store_):
    put(index, "a", tool_calls=failing("a"))
    run(FakeModel(label_everything, name="claude", model=None), plan(index, store_), store_)
    row = store_.execute("SELECT engine, model, prompt_version FROM episode_labels").fetchone()
    assert tuple(row) == ("claude", None, labels.LABEL_PROMPT_VERSION)


def test_a_halt_with_every_worker_busy_returns_instead_of_hanging(index, store_, tmp_path):
    for i in range(40):
        put(index, f"e{i}", tool_calls=failing(f"e{i}"))
    planned = plan(index, store_, choice="all")
    stopped = threading.Event()
    seen = []

    def respond(prompt, schema):
        seen.append(prompt)
        if len(seen) > 1:
            time.sleep(0.3)
        return label_everything(prompt, schema)

    outcomes = []

    def go():
        with closing(store.open_labels(tmp_path / "threaded.sqlite")) as conn:
            outcomes.append(
                labels.run_labels(
                    FakeModel(respond),
                    planned,
                    conn,
                    NOW,
                    stopped.is_set,
                    lambda kind, done, total: stopped.set(),
                )
            )

    worker = threading.Thread(target=go, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive()
    assert outcomes[0].episodes > 0


def test_a_non_object_reply_fails_only_that_batch(index, store_):
    put(index, "a", tool_calls=failing("a"))
    model = FakeModel(lambda prompt, schema: [{"id": "a"}])
    outcome = run(model, plan(index, store_), store_)
    assert (outcome.failed, outcome.episodes, len(model.prompts)) == (1, 0, labels.MAX_ATTEMPTS)


def test_model_text_is_flattened_and_capped(index, store_):
    put(index, "a", tool_calls=failing("a"))

    def respond(prompt, schema):
        cause = "\x1b[31mred\nline" + "x" * 500
        return {"labels": [{"id": i, **LABEL, "cause": cause} for i in sent_ids(prompt)]}

    planned = plan(index, store_)
    run(FakeModel(respond), planned, store_)
    key = [(e.episode_id, e.content_hash) for e in planned.episodes]
    cause = labels.episode_labels(store_, key)[planned.episodes[0].episode_id].cause
    assert cause == "[31mred line" + "x" * (labels.CAUSE_CHARS - len("[31mred line"))


def test_a_named_issue_is_not_renamed_when_its_content_hash_shifts(index, store_):
    put(index, "a", tool_calls=failing("a", signature="make|exit_code|boom"))
    put(index, "b", session=B, tool_calls=failing("b", signature="make|exit_code|boom"))
    found = issues.recurring_issues(
        index, scope(), embedder=None, names={}, min_sessions=2, limit=20
    )
    run(FakeModel(label_everything), plan(index, store_, found=found), store_)
    shifted = [replace(i, content_hash="shifted") for i in found]
    assert plan(index, store_, found=shifted).issues == ()
