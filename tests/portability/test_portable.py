import json
import re
import sys
from dataclasses import replace
from uuid import uuid4

import pytest
from ringleader.portability import native
from ringleader.portability.capabilities import SourceRequest, TargetRequest
from ringleader.portability.claude import PROFILE as CLAUDE_PROFILE
from ringleader.portability.claude import ClaudeAdapter
from ringleader.portability.codex import PROFILE as CODEX_PROFILE
from ringleader.portability.codex import CodexAdapter
from ringleader.portability.compact import (
    MAX_PORTABLE_TOKENS,
    PORTABLE_TRANSFORM_VERSION,
    _duplicates,
    portable,
)
from ringleader.portability.compare import Normalization, compare, model_view
from ringleader.portability.model import (
    Attachment,
    AttachmentRef,
    Checkpoint,
    Conversation,
    Identity,
    Message,
    Opaque,
    PreservationError,
    Text,
    ToolCall,
    ToolOmission,
    ToolResult,
    canonical_json,
    validate,
)
from ringleader.portability.opencode import PROFILE as OPENCODE_PROFILE
from ringleader.portability.opencode import OpenCodeAdapter
from ringleader.portability.operations import decode, encode
from ringleader.portability.pi import PROFILE as PI_PROFILE
from ringleader.portability.pi import PiAdapter
from ringleader.portability.serialization import dumps, loads
from ringleader.portability.service import ConversionRequest
from ringleader.portability.snapshot import SourceSnapshot, TransformOptions
from tests.portability.fixtures import SOURCE, message
from tests.portability.test_codex import initialized
from tests.portability.test_native_adapters import roots
from tests.portability.test_opencode import roots as opencode_roots
from tests.portability.test_service import service, source

COMPACTION = Opaque("codex", "codex.compaction", canonical_json({"encrypted_content": "abc"}))
REFERENCE = Opaque(
    "claude-code",
    "claude.tool_reference",
    canonical_json({"tool_name": "search", "type": "tool_reference"}),
    (Text("[tool reference: search]"),),
)


def single(*messages: Message, **changes) -> Conversation:
    linked = native.linked(messages)
    return Conversation(SOURCE, linked[-1].id, linked, linked, **changes)


def tool_exchange() -> Conversation:
    return single(
        message("ask", "user", Text("Find it"), Opaque("codex", "codex.phase", '"commentary"')),
        message("call", "assistant", ToolCall("c1", "search", '{"q":"x"}', "json")),
        message("result", "tool", ToolResult("c1", (Text("found"), REFERENCE))),
        message("answer", "assistant", Text("Here"), REFERENCE),
    )


def opaque_checkpoint(retained=None, replaced=True) -> Conversation:
    history = native.linked(
        [
            message("u1", "user", Text("ask")),
            message("a1", "assistant", ToolCall("c1", "read", '{"path":"a"}', "json")),
            message("t1", "tool", ToolResult("c1", (Text("contents"),))),
            message("a2", "assistant", Text("done")),
            message("u2", "user", Text("next")),
        ]
    )
    summary = (message("s1", "user", COMPACTION),)
    retained = retained if retained is not None else native.linked([history[3]])
    checkpoint = Checkpoint(
        "cp",
        "a2",
        summary,
        retained_context=retained,
        replaced_context=history[:4] if replaced else None,
    )
    return Conversation(
        SOURCE,
        "u2",
        history,
        native.linked([*summary, *retained, history[4]]),
        (checkpoint,),
    )


def contents(messages):
    return [(item.id, item.content) for item in messages]


def test_opaque_hidden_and_replaced_context_roundtrip():
    exchange = tool_exchange()
    hidden = replace(
        exchange,
        context=native.linked(
            [*exchange.context, replace(message("meta", "user", Text("hidden")), hidden=True)]
        ),
    )
    for original in (hidden, opaque_checkpoint()):
        assert loads(dumps(original)) == original
    assert loads(dumps(hidden)).context[-1].hidden is True
    assert loads(dumps(hidden)).history[2].content[0].output[1] == REFERENCE
    assert (
        loads(dumps(opaque_checkpoint())).checkpoints[0].replaced_context
        == (opaque_checkpoint().history[:4])
    )


def test_current_shape_documents_decode_and_reencode_byte_identically():
    identity = {"type": "Identity", "agent": "pi", "native_id": "session"}

    def row(key, parent, text):
        return {
            "type": "Message",
            "id": key,
            "role": "user",
            "content": [{"type": "Text", "text": text}],
            "provenance": {
                "type": "Provenance",
                "origin": "authored",
                "source": identity,
                "locator": key,
                "evidence": "",
            },
            "parent_id": parent,
            "timestamp": None,
        }

    document = {
        "type": "Conversation",
        "source": identity,
        "branch_id": "m2",
        "history": [row("m1", None, "one"), row("m2", "m1", "two")],
        "context": [row("s", None, "summary"), row("m2", "s", "two")],
        "checkpoints": [
            {
                "type": "Checkpoint",
                "id": "cp",
                "after_message_id": "m1",
                "summary": [row("s", None, "summary")],
                "retained_message_ids": [],
                "retained_context": None,
                "following_context": None,
            }
        ],
        "attachments": [],
        "title": None,
        "schema_version": 1,
        "source_created_at": None,
        "source_updated_at": None,
    }
    data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    decoded = loads(data)
    assert [item.hidden for item in decoded.context] == [False, False]
    assert decoded.checkpoints[0].replaced_context is None
    assert dumps(decoded) == data
    legacy = b'{"compact":false,"type":"TransformOptions","version":1}'
    assert decode(legacy, TransformOptions) == TransformOptions()
    assert encode(TransformOptions()) == legacy


def test_portable_version_joins_the_source_identity():
    snapshot = SourceSnapshot(tool_exchange(), (), "profile")
    assert PORTABLE_TRANSFORM_VERSION >= 1
    assert snapshot.identity(TransformOptions()) != snapshot.identity(
        TransformOptions(portable_version=PORTABLE_TRANSFORM_VERSION)
    )


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (Opaque("codex", "codex.phase", '{"b":1, "a":2}'), "not canonical JSON: codex.phase"),
        (Opaque("codex", "codex.phase", "not json"), "Invalid opaque payload: codex.phase"),
        (Opaque("", "codex.phase", "1"), "owning agent and kind"),
    ],
)
def test_opaque_payload_must_be_canonical_owned_json(item, reason):
    with pytest.raises(PreservationError, match=reason):
        validate(single(message("one", "user", item)))


def test_foreign_opaque_becomes_its_fallback_everywhere():
    result = portable(tool_exchange(), "pi")
    for stream in (result.history, result.context):
        assert contents(stream) == [
            ("ask", (Text("Find it"),)),
            ("call", (ToolCall("c1", "search", '{"q":"x"}', "json"),)),
            ("result", (ToolResult("c1", (Text("found"), Text("[tool reference: search]"))),)),
            ("answer", (Text("Here"), Text("[tool reference: search]"))),
        ]


def test_same_agent_opaque_is_kept_and_foreign_one_is_replaced():
    result = portable(tool_exchange(), "claude-code")
    assert contents(result.history) == [
        ("ask", (Text("Find it"),)),
        ("call", (ToolCall("c1", "search", '{"q":"x"}', "json"),)),
        ("result", (ToolResult("c1", (Text("found"), REFERENCE)),)),
        ("answer", (Text("Here"), REFERENCE)),
    ]
    assert (
        portable(tool_exchange(), "codex").history[0].content == tool_exchange().history[0].content
    )


def test_opaque_checkpoint_is_substituted_by_its_collapsed_replaced_history():
    original = opaque_checkpoint(
        retained=native.linked(
            [
                message("a2", "assistant", Text("done")),
                message("kept", "user", Text("retained only")),
            ]
        )
    )
    original = replace(
        original,
        context=native.linked(
            [
                *original.checkpoints[0].summary,
                *original.checkpoints[0].retained_context,
                original.history[4],
            ]
        ),
    )
    result = portable(original, "claude-code")
    assert result.checkpoints == ()
    assert result.history == original.history
    assert contents(result.context) == [
        ("u1", (Text("ask"),)),
        ("a1", (ToolOmission(("read",), 1, 1),)),
        ("a2", (Text("done"),)),
        ("kept", (Text("retained only"),)),
        ("u2", (Text("next"),)),
    ]
    assert [item.parent_id for item in result.context] == [None, "u1", "a1", "a2", "kept"]


def test_opaque_checkpoint_is_kept_for_its_own_agent_without_its_replaced_history():
    original = opaque_checkpoint()
    result = portable(original, "codex")
    assert (result.history, result.context) == (original.history, original.context)
    assert result.checkpoints == (replace(original.checkpoints[0], replaced_context=None),)


def test_attachments_only_the_dropped_streams_referenced_are_pruned():
    image = Attachment("img", "image/png", b"png")
    original = opaque_checkpoint()
    replaced = (
        message("u0", "user", AttachmentRef("img")),
        *original.checkpoints[0].replaced_context,
    )
    original = replace(
        original,
        checkpoints=(replace(original.checkpoints[0], replaced_context=replaced),),
        attachments=(image,),
    )
    assert portable(original, "codex").attachments == ()
    assert portable(original, "pi").attachments == (image,)


def test_only_the_newest_opaque_checkpoint_is_substituted_and_older_ones_are_removed():
    newest = opaque_checkpoint()
    older = Checkpoint("older", "u1", (message("s0", "user", COMPACTION),))
    original = replace(newest, checkpoints=(older, *newest.checkpoints))
    result = portable(original, "pi")
    assert result.checkpoints == ()
    assert [item.id for item in result.context] == ["u1", "a1", "a2", "u2"]


def test_newer_portable_checkpoint_survives_an_older_opaque_one():
    base = opaque_checkpoint()
    opaque = replace(base.checkpoints[0], after_message_id="u1", retained_context=())
    summary = (message("s2", "user", Text("portable summary")),)
    newest = Checkpoint("newest", "a2", summary)
    original = replace(
        base,
        checkpoints=(opaque, newest),
        context=native.linked([*summary, base.history[4]]),
    )
    result = portable(original, "pi")
    assert result.checkpoints == (newest,)
    assert result.context == original.context


def test_newer_oversized_portable_checkpoint_survives_an_older_opaque_one():
    base = opaque_checkpoint()
    opaque = replace(base.checkpoints[0], after_message_id="u1", retained_context=())
    huge_summary = (message("s2", "user", Text("x" * (MAX_PORTABLE_TOKENS * 4 + 8))),)
    newest = Checkpoint("newest", "a2", huge_summary)
    original = replace(
        base,
        checkpoints=(opaque, newest),
        context=native.linked([*huge_summary, base.history[4]]),
    )
    result = portable(original, "pi")
    assert result.checkpoints == (newest,)
    assert result.context == original.context


def test_superseded_opaque_checkpoint_without_replaced_history_is_dropped_not_refused():
    base = opaque_checkpoint(replaced=False)
    opaque = replace(base.checkpoints[0], after_message_id="u1", retained_context=())
    summary = (message("s2", "user", Text("portable summary")),)
    newest = Checkpoint("newest", "a2", summary)
    original = replace(
        base,
        checkpoints=(opaque, newest),
        context=native.linked([*summary, base.history[4]]),
    )
    result = portable(original, "pi")
    assert result.checkpoints == (newest,)
    assert result.context == original.context


def test_opaque_checkpoint_without_replaced_history_refuses():
    with pytest.raises(PreservationError, match="opaque checkpoint has no portable history"):
        portable(opaque_checkpoint(replaced=False), "claude-code")


def budget(monkeypatch, tokens: int) -> None:
    monkeypatch.setattr(sys.modules[portable.__module__], "MAX_PORTABLE_TOKENS", tokens)


def over_budget(*replaced: Message) -> Conversation:
    history = native.linked([*replaced, message("u9", "user", Text("next"))])
    summary = (message("s1", "user", COMPACTION),)
    retained = native.linked([history[1], message("kept", "user", Text("retained only"))])
    checkpoint = Checkpoint(
        "cp",
        replaced[-1].id,
        summary,
        retained_context=retained,
        replaced_context=history[:-1],
    )
    return Conversation(
        SOURCE,
        "u9",
        history,
        native.linked([*summary, *retained, history[-1]]),
        (checkpoint,),
    )


def three_turns() -> Conversation:
    return over_budget(
        message("u1", "user", Text("x" * 400)),
        message("a1", "assistant", Text("one")),
        message("u2", "user", Text("y" * 400)),
        message("a2", "assistant", ToolCall("c2", "read", '{"path":"b"}', "json")),
        message("t2", "tool", ToolResult("c2", (Text("contents"),))),
        message("a3", "assistant", Text("two")),
        message("u3", "user", Text("third")),
        message("a4", "assistant", Text("three")),
    )


def test_substituted_context_within_budget_holds_each_retained_message_once():
    result = portable(three_turns(), "pi")
    assert [item.id for item in result.context] == [
        "u1",
        "a1",
        "u2",
        "a2",
        "a3",
        "u3",
        "a4",
        "kept",
        "u9",
    ]


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (150, ["u2", "a2", "a3", "u3", "a4", "a1", "kept", "u9"]),
        (60, ["u3", "a4", "a1", "kept", "u9"]),
    ],
)
def test_over_budget_substitution_drops_the_oldest_collapsed_turns_behind_one_marker(
    monkeypatch, tokens, expected
):
    budget(monkeypatch, tokens)
    result = portable(three_turns(), "pi")
    marker, *rest = result.context
    omitted = 1 if tokens == 150 else 4
    noun = "message" if omitted == 1 else "messages"
    assert (marker.role, marker.content, marker.provenance.origin) == (
        "user",
        (Text(f"[{omitted} earlier {noun} omitted to fit the converted context]"),),
        "harness",
    )
    assert [item.id for item in rest] == expected
    assert [item.content for item in rest[-3:]] == [
        (Text("one"),),
        (Text("retained only"),),
        (Text("next"),),
    ]
    assert [item.parent_id for item in result.context] == [
        None,
        *(item.id for item in result.context[:-1]),
    ]


def test_substitution_refuses_when_the_live_context_alone_is_over_budget(monkeypatch):
    budget(monkeypatch, 4)
    with pytest.raises(PreservationError, match=r"^converted context too large: ~5 tokens$"):
        portable(three_turns(), "pi")


def test_over_budget_substitution_never_splits_a_tool_call_from_its_result(monkeypatch):
    original = over_budget(
        message("u1", "user", Text("x" * 400)),
        message("a1", "assistant", Text("one")),
        message("c1", "assistant", ToolCall("c1", "read", '{"path":"a"}', "json")),
        message("steer", "user", Text("also check b")),
        message("t1", "tool", ToolResult("c1", (Text("contents"),))),
        message("u2", "user", Text("later")),
        message("a2", "assistant", Text("two")),
    )
    budget(monkeypatch, 40)
    result = portable(original, "pi")
    assert [item.id for item in result.context] == ["cp:omitted", "u2", "a2", "a1", "kept", "u9"]
    assert result.context[0].content == (
        Text("[4 earlier messages omitted to fit the converted context]"),
    )


def copied_retained(replaced: tuple[Message, ...], retained: tuple[Message, ...]) -> Conversation:
    history = native.linked([*replaced, message("u9", "user", Text("next"))])
    summary = (message("s1", "user", COMPACTION),)
    checkpoint = Checkpoint(
        "cp",
        replaced[-1].id,
        summary,
        retained_context=native.linked(list(retained)),
        replaced_context=history[:-1],
    )
    return Conversation(
        SOURCE,
        "u9",
        history,
        native.linked([*summary, *checkpoint.retained_context, history[-1]]),
        (checkpoint,),
    )


def twice_yes() -> tuple[Message, ...]:
    return (
        message("u1", "user", Text("yes")),
        message("a1", "assistant", Text("x" * 400)),
        message("u2", "user", Text("yes")),
        message("a2", "assistant", Text("done")),
    )


def test_an_id_less_retained_copy_pairs_with_the_latest_equivalent_replaced_message():
    replaced = twice_yes()
    retained = (message("k", "user", Text("yes")),)
    assert _duplicates(retained, replaced) == {"k": "u2"}
    within = portable(copied_retained(replaced, retained), "pi")
    assert contents(within.context) == [*contents(replaced), ("u9", (Text("next"),))]


def test_dropping_an_older_equal_turn_over_budget_keeps_one_copy_of_the_retained_message(
    monkeypatch,
):
    budget(monkeypatch, 40)
    result = portable(copied_retained(twice_yes(), (message("k", "user", Text("yes")),)), "pi")
    assert contents(result.context) == [
        ("cp:omitted", (Text("[2 earlier messages omitted to fit the converted context]"),)),
        ("u2", (Text("yes"),)),
        ("a2", (Text("done"),)),
        ("u9", (Text("next"),)),
    ]


def test_retained_copies_pair_with_replaced_messages_in_their_own_order():
    replaced = (
        message("u1", "user", Text("yes")),
        message("a1", "assistant", Text("one")),
        message("u2", "user", Text("go on")),
        message("a2", "assistant", Text("two")),
        message("u3", "user", Text("yes")),
        message("a3", "assistant", Text("three")),
    )
    retained = (message("k1", "user", Text("yes")), message("k2", "user", Text("go on")))
    assert _duplicates(retained, replaced) == {"k1": "u1", "k2": "u2"}
    result = portable(copied_retained(replaced, retained), "pi")
    assert [item.id for item in result.context] == ["u1", "a1", "u2", "a2", "u3", "a3", "u9"]


def test_a_retained_copy_and_a_new_equal_message_never_share_one_replaced_message():
    replaced = twice_yes()
    retained = (message("u2", "user", Text("yes")), message("new", "user", Text("yes")))
    assert _duplicates(retained, replaced) == {"u2": "u2"}
    result = portable(copied_retained(replaced, retained), "pi")
    assert [item.id for item in result.context] == ["u1", "a1", "u2", "a2", "new", "u9"]


def test_a_retained_message_without_an_equivalent_survives_beside_the_copies():
    truncated = message("t", "user", Text("yes…1 tokens truncated…"))
    retained = (truncated, message("k", "user", Text("yes")))
    result = portable(copied_retained(twice_yes(), retained), "pi")
    assert contents(result.context) == [
        *contents(twice_yes()),
        ("t", (Text("yes…1 tokens truncated…"),)),
        ("u9", (Text("next"),)),
    ]


def two_streams() -> Conversation:
    history = native.linked(
        [
            message("question", "user", Text("visible question")),
            message("status", "assistant", Text("visible only")),
            message("answer", "assistant", Text("answer")),
        ]
    )
    context = native.linked(
        [
            message("m-question", "user", Text("visible question")),
            replace(message("meta", "user", Text("hidden context")), hidden=True),
            message("m-answer", "assistant", Text("answer")),
        ]
    )
    return Conversation(SOURCE, "answer", history, context, title="Two streams")


def staged(adapter, profile, target_roots, conversation):
    identity = (
        adapter.allocate_identity()
        if isinstance(adapter, OpenCodeAdapter)
        else Identity(profile.agent, str(uuid4()))
    )
    return adapter.stage(
        conversation, TargetRequest(identity, target_roots, profile, conversation.title)
    )


def texts(messages):
    return [(item.role, item.content[0].text) for item in messages]


def test_claude_stages_the_model_view_with_hidden_rows_as_meta(tmp_path):
    adapter = ClaudeAdapter()
    stage = staged(adapter, CLAUDE_PROFILE, roots(tmp_path), two_streams())
    rows = [row for row in native.records(stage.files[0].data) if "message" in row]
    assert [(row["message"]["content"][0]["text"], row.get("isMeta")) for row in rows] == [
        ("visible question", None),
        ("hidden context", True),
        ("answer", None),
    ]
    readback = adapter.read_stage(stage)
    assert texts(readback.history) == [("user", "visible question"), ("assistant", "answer")]
    assert [item.hidden for item in readback.context] == [False, True, False]
    compare(two_streams(), readback, adapter.normalizations(CLAUDE_PROFILE))


@pytest.mark.parametrize(
    ("adapter", "profile", "make_roots"),
    [
        (PiAdapter(), PI_PROFILE, roots),
        (OpenCodeAdapter(), OPENCODE_PROFILE, opencode_roots.__wrapped__),
    ],
)
def test_single_stream_targets_stage_the_model_view_as_ordinary_messages(
    adapter, profile, make_roots, tmp_path
):
    readback = adapter.read_stage(staged(adapter, profile, make_roots(tmp_path), two_streams()))
    expected = [
        ("user", "visible question"),
        ("user", "hidden context"),
        ("assistant", "answer"),
    ]
    assert texts(readback.history) == expected
    assert texts(readback.context) == expected
    compare(two_streams(), readback, adapter.normalizations(profile))


def test_codex_keeps_distinct_visible_and_model_streams(tmp_path):
    adapter = CodexAdapter()
    readback = adapter.read_stage(
        staged(adapter, CODEX_PROFILE, initialized(tmp_path), two_streams())
    )
    assert texts(readback.history) == [
        ("user", "visible question"),
        ("assistant", "visible only"),
        ("assistant", "answer"),
    ]
    assert texts(readback.context) == [
        ("user", "visible question"),
        ("user", "hidden context"),
        ("assistant", "answer"),
    ]
    compare(two_streams(), readback, adapter.normalizations(CODEX_PROFILE))


def test_codex_read_back_checks_its_visible_history_when_nothing_was_flattened(tmp_path):
    adapter = CodexAdapter()
    stage = staged(adapter, CODEX_PROFILE, initialized(tmp_path), two_streams())
    assert [item.kind for item in stage.normalizations] == ["json-argument-whitespace"]
    readback = adapter.read_stage(stage)
    truncated = replace(readback, history=readback.history[:1])
    with pytest.raises(PreservationError, match="Preservation mismatch: history"):
        compare(two_streams(), truncated, stage.normalizations)
    hidden = next(item for item in readback.context if item.content[0].text == "hidden context")
    exposed = replace(readback, history=native.linked([*readback.history[:1], hidden]))
    with pytest.raises(PreservationError, match="Preservation mismatch: history"):
        compare(two_streams(), exposed, stage.normalizations)


@pytest.mark.parametrize(
    ("adapter", "profile", "make_roots", "foreign"),
    [
        (ClaudeAdapter(), CLAUDE_PROFILE, roots, "codex.phase"),
        (PiAdapter(), PI_PROFILE, roots, "codex.phase"),
        (CodexAdapter(), CODEX_PROFILE, initialized, "claude.tool_reference"),
        (OpenCodeAdapter(), OPENCODE_PROFILE, opencode_roots.__wrapped__, "codex.phase"),
    ],
)
def test_writers_refuse_any_foreign_opaque_that_reaches_them(
    adapter, profile, make_roots, foreign, tmp_path
):
    with pytest.raises(PreservationError, match=f"Unsupported opaque kind: {re.escape(foreign)}"):
        staged(adapter, profile, make_roots(tmp_path), replace(tool_exchange(), title="Opaque"))


def test_model_view_projection_hides_only_for_targets_with_a_hidden_form():
    hiding = model_view(two_streams(), hides=True)
    plain = model_view(two_streams(), hides=False)
    assert texts(hiding.history) == [("user", "visible question"), ("assistant", "answer")]
    assert [item.hidden for item in plain.context] == [False, False, False]
    assert texts(plain.history) == texts(plain.context)
    with pytest.raises(PreservationError, match="documented supported profile"):
        Normalization("adjacent-text", "profile", "reason", hides=True)


def test_service_applies_portable_before_staging(tmp_path, monkeypatch):
    worker = service(tmp_path)
    reader = worker.adapters.reader("pi")
    original = reader.snapshot

    def with_opaque(request):
        snapshot = original(request)
        history = tuple(
            replace(item, content=(*item.content, REFERENCE)) if item.role == "assistant" else item
            for item in snapshot.conversation.history
        )
        return replace(
            snapshot,
            conversation=replace(snapshot.conversation, history=history, context=history),
        )

    monkeypatch.setattr(reader, "snapshot", with_opaque)
    request = ConversionRequest(source(tmp_path), "pi", roots(tmp_path / "target"))
    result = worker.convert(request)
    stage = worker.store.artifacts(worker.transactions()[0]).stage.native()
    readback = original(SourceRequest(result.target, stage.roots, stage.source_ref))
    assert all(
        item.content[-1] == Text("[tool reference: search]")
        for item in readback.conversation.history
        if item.role == "assistant"
    )
    assert worker.store.artifacts(worker.transactions()[0]).options.portable_version == (
        PORTABLE_TRANSFORM_VERSION
    )
