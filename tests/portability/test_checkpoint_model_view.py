from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from ringleader.portability import native
from ringleader.portability.capabilities import TargetRequest
from ringleader.portability.claude import PROFILE as CLAUDE_PROFILE
from ringleader.portability.claude import ClaudeAdapter
from ringleader.portability.codex import PROFILE as CODEX_PROFILE
from ringleader.portability.codex import CodexAdapter
from ringleader.portability.compare import Normalization, compare, flattened, model_view
from ringleader.portability.droid import LLM_SUMMARY, DroidAdapter, summary_text
from ringleader.portability.droid import PROFILE as DROID_PROFILE
from ringleader.portability.model import (
    Checkpoint,
    Conversation,
    Identity,
    Message,
    PreservationError,
    Provenance,
    Text,
)
from ringleader.portability.opencode import PROFILE as OPENCODE_PROFILE
from ringleader.portability.opencode import OpenCodeAdapter
from ringleader.portability.pi import PROFILE as PI_PROFILE
from ringleader.portability.pi import SUMMARY_PREFIX, SUMMARY_SUFFIX, PiAdapter
from ringleader.portability.service import _check_stage
from ringleader.portability.verify.fixtures import native_conversation
from tests.portability.test_codex import initialized as codex_target
from tests.portability.test_droid import target as droid_target
from tests.portability.test_native_adapters import roots
from tests.portability.test_opencode import roots as opencode_roots

TITLE = "Imported"
PI_SUMMARY = SUMMARY_PREFIX + "pi summary" + SUMMARY_SUFFIX
DROID_SUMMARY = summary_text(LLM_SUMMARY, "droid summary")


def base() -> Conversation:
    original = native_conversation()
    later = replace(original.history[0], id="later", content=(Text("Continue"),))
    history = native.linked([*original.history, later])
    return replace(original, history=history, context=history, title=TITLE)


def user(key: str, text: str, *, hidden: bool = False) -> Message:
    source = Identity("pi", "source")
    provenance = (
        Provenance("harness", source, key, "synthetic hidden context")
        if hidden
        else Provenance("authored", source, key)
    )
    return Message(key, "user", (Text(text),), provenance, hidden=hidden)


def checkpointed(
    summary: tuple[Message, ...],
    *,
    after: str = "answer",
    retained_ids: tuple[str, ...] = (),
    retained: tuple[Message, ...] | None = None,
    following: tuple[Message, ...] | None = None,
) -> Conversation:
    conversation = base()
    by_id = {message.id: message for message in conversation.history}
    cut = [message.id for message in conversation.history].index(after) + 1
    kept = retained if retained is not None else tuple(by_id[key] for key in retained_ids)
    tail = following if following is not None else conversation.history[cut:]
    checkpoint = Checkpoint(
        "cp",
        after,
        summary,
        retained_ids,
        native.linked(retained) if retained is not None else None,
        native.linked(following) if following is not None else None,
    )
    return replace(
        conversation,
        checkpoints=(checkpoint,),
        context=native.linked([*summary, *kept, *tail]),
    )


def texts(messages: tuple[Message, ...]) -> list[tuple[str, bool, str]]:
    return [
        (message.role, message.hidden, message.content[0].text)
        for message in messages
        if isinstance(message.content[0], Text)
    ]


def model_texts(conversation: Conversation, hides: bool) -> list[tuple[str, bool, str]]:
    return texts(model_view(flattened(conversation), hides=hides).context)


def staged(adapter, profile, conversation, request):
    stage = adapter.stage(conversation, request)
    readback = adapter.read_stage(stage)
    compare(conversation, readback, stage.normalizations)
    assert readback.checkpoints == ()
    assert "flattened-checkpoint" in {item.kind for item in stage.normalizations}
    return stage, readback


def request_for(profile, tmp_path) -> TargetRequest:
    return TargetRequest(Identity(profile.agent, str(uuid4())), roots(tmp_path), profile, TITLE)


FOREIGN = (user("summary", "Another agent's summary"),)
NOTE = user("note", "hidden note", hidden=True)


@pytest.mark.parametrize(
    "conversation",
    [
        checkpointed(FOREIGN),
        checkpointed((user("summary", PI_SUMMARY),), following=(NOTE, base().history[-1])),
        checkpointed((user("summary", PI_SUMMARY),), retained=(NOTE, user("kept", "kept"))),
    ],
    ids=["foreign-summary", "hidden-after", "independent-retained"],
)
def test_pi_writes_an_inexpressible_checkpoint_as_its_model_view(tmp_path, conversation):
    stage, readback = staged(
        PiAdapter(), PI_PROFILE, conversation, request_for(PI_PROFILE, tmp_path)
    )
    assert texts(readback.history) == model_texts(conversation, hides=False)
    assert all(row["type"] != "compaction" for row in native.records(stage.files[0].data))


def test_pi_keeps_writing_its_native_compaction(tmp_path):
    conversation = checkpointed((user("summary", PI_SUMMARY),), retained_ids=("answer",))
    stage = PiAdapter().stage(conversation, request_for(PI_PROFILE, tmp_path))
    readback = PiAdapter().read_stage(stage)
    compare(conversation, readback, PiAdapter().normalizations(PI_PROFILE))
    assert len(readback.checkpoints) == 1
    rows = native.records(stage.files[0].data)
    entries = [row for row in rows if row["type"] in {"message", "compaction", "session_info"}]
    assert [row["parentId"] for row in entries] == [None, *(row["id"] for row in entries[:-1])]
    compaction_index = next(i for i, row in enumerate(entries) if row["type"] == "compaction")
    assert entries[compaction_index]["firstKeptEntryId"] == entries[compaction_index - 1]["id"]
    assert entries[compaction_index + 1]["message"]["content"] == [
        {"type": "text", "text": "Continue"}
    ]


@pytest.mark.parametrize(
    "conversation",
    [
        checkpointed(FOREIGN),
        checkpointed((user("summary", DROID_SUMMARY),), following=(NOTE,)),
        checkpointed((user("summary", DROID_SUMMARY),), after="request", retained_ids=("request",)),
    ],
    ids=["foreign-summary", "hidden-after-without-visible-tail", "nothing-before-the-anchor"],
)
def test_droid_writes_an_inexpressible_checkpoint_as_its_model_view(tmp_path, conversation):
    adapter = DroidAdapter()
    stage, readback = staged(
        adapter, DROID_PROFILE, conversation, droid_target(tmp_path, conversation)
    )
    rows = native.records(stage.files[0].data)
    assert texts(readback.context) == model_texts(conversation, hides=True)
    assert [row["type"] for row in rows[1:]] == ["message"] * len(readback.context)
    hidden = [row["message"].get("visibility") == "llm_only" for row in rows[1:]]
    assert hidden == [message.hidden for message in readback.context]


def test_droid_flattens_when_an_earlier_checkpoint_owns_its_retained_context(tmp_path):
    conversation = base()
    first = Checkpoint(
        "first",
        "call",
        (user("first-summary", DROID_SUMMARY),),
        retained_context=native.linked([NOTE]),
    )
    second = Checkpoint("second", "answer", (user("second-summary", DROID_SUMMARY),), ("answer",))
    by_id = {message.id: message for message in conversation.history}
    two = replace(
        conversation,
        checkpoints=(first, second),
        context=native.linked([second.summary[0], by_id["answer"], by_id["later"]]),
    )
    adapter = DroidAdapter()
    _, readback = staged(adapter, DROID_PROFILE, two, droid_target(tmp_path, two))
    assert texts(readback.context)[0] == ("user", False, DROID_SUMMARY)


@pytest.mark.parametrize(
    "conversation",
    [
        checkpointed((user("question", "q"), replace(user("summary", "s"), role="assistant"))),
        checkpointed((user("summary", "s"),), following=(NOTE,)),
    ],
    ids=["assistant-summary", "following-hides-visible-history"],
)
def test_claude_writes_an_inexpressible_checkpoint_as_its_model_view(tmp_path, conversation):
    stage, readback = staged(
        ClaudeAdapter(), CLAUDE_PROFILE, conversation, request_for(CLAUDE_PROFILE, tmp_path)
    )
    rows = native.records(stage.files[0].data)
    assert texts(readback.context) == model_texts(conversation, hides=True)
    assert all(row.get("subtype") != "compact_boundary" for row in rows)
    assert [row.get("isMeta", False) for row in rows if row["type"] in ("user", "assistant")] == [
        message.hidden for message in readback.context
    ]


def test_claude_keeps_writing_a_checkpoint_it_can_express(tmp_path):
    conversation = checkpointed(FOREIGN, retained=(NOTE, user("kept", "kept")))
    stage = ClaudeAdapter().stage(conversation, request_for(CLAUDE_PROFILE, tmp_path))
    readback = ClaudeAdapter().read_stage(stage)
    compare(conversation, readback, ClaudeAdapter().normalizations(CLAUDE_PROFILE))
    assert len(readback.checkpoints) == 1


@pytest.mark.parametrize(
    "conversation",
    [checkpointed(FOREIGN), checkpointed(FOREIGN, retained=(user("kept", "kept"),))],
    ids=["foreign-summary", "independent-retained"],
)
def test_opencode_writes_an_inexpressible_checkpoint_as_its_model_view(tmp_path, conversation):
    adapter = OpenCodeAdapter()
    target_roots = opencode_roots.__wrapped__(tmp_path)
    _, readback = staged(
        adapter,
        OPENCODE_PROFILE,
        conversation,
        TargetRequest(adapter.allocate_identity(), target_roots, OPENCODE_PROFILE, TITLE),
    )
    assert texts(readback.history) == model_texts(conversation, hides=False)


def test_codex_writes_two_checkpoints_at_one_boundary_as_its_model_view(tmp_path):
    conversation = base()
    first = Checkpoint("first", "answer", (user("first-summary", "s1"),), ())
    second = Checkpoint("second", "answer", (user("second-summary", "s2"),), ("answer",))
    by_id = {message.id: message for message in conversation.history}
    two = replace(
        conversation,
        checkpoints=(first, second),
        context=native.linked([second.summary[0], by_id["answer"], by_id["later"]]),
    )
    adapter = CodexAdapter()
    request = TargetRequest(
        Identity("codex", str(uuid4())), codex_target(tmp_path), CODEX_PROFILE, TITLE
    )
    stage, readback = staged(adapter, CODEX_PROFILE, two, request)
    assert texts(readback.history) == model_texts(two, hides=False)
    assert all(row["type"] != "compacted" for row in native.records(stage.files[0].data))


def test_a_pre_wave_one_codex_stage_with_no_recorded_normalizations_still_resumes(tmp_path):
    conversation = base()
    first = Checkpoint("first", "answer", (user("first-summary", "s1"),), ())
    second = Checkpoint("second", "answer", (user("second-summary", "s2"),), ("answer",))
    by_id = {message.id: message for message in conversation.history}
    two = replace(
        conversation,
        checkpoints=(first, second),
        context=native.linked([second.summary[0], by_id["answer"], by_id["later"]]),
    )
    adapter = CodexAdapter()
    request = TargetRequest(
        Identity("codex", str(uuid4())), codex_target(tmp_path), CODEX_PROFILE, TITLE
    )
    stage = adapter.stage(two, request)
    assert "flattened-checkpoint" in {item.kind for item in stage.normalizations}

    legacy = replace(stage, normalizations=None)
    _check_stage(two, adapter, legacy)


def test_compare_checks_a_flattened_read_back_against_the_model_view():
    conversation = checkpointed(FOREIGN, following=(NOTE, base().history[-1]))
    flat = model_view(flattened(conversation), hides=True)
    view = Normalization("model-view-history", "profile", "one stream", hides=True)
    compare(conversation, flat, (view,))
    with pytest.raises(
        PreservationError, match="Preservation mismatch: history, checkpoints, attachments"
    ):
        compare(conversation, flat)
    assert flat.attachments == ()


def test_a_stage_that_drops_checkpoints_must_record_the_flattening(tmp_path):
    conversation = checkpointed(FOREIGN)
    adapter = PiAdapter()
    stage = adapter.stage(conversation, request_for(PI_PROFILE, tmp_path))
    _check_stage(conversation, adapter, stage)
    silent = replace(stage, normalizations=adapter.normalizations(PI_PROFILE))
    with pytest.raises(PreservationError, match="dropped checkpoints without recording"):
        _check_stage(conversation, adapter, silent)
