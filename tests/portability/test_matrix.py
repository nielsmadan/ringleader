from dataclasses import replace

import pytest
from ringleader.portability import native
from ringleader.portability.capabilities import SourceRequest
from ringleader.portability.compact import compact as transform
from ringleader.portability.compact import portable
from ringleader.portability.compare import compare, preservation_projection
from ringleader.portability.model import Identity
from ringleader.portability.service import ConversionRequest
from tests.portability.fixtures import SESSION_ID, claude_records, codex_tool_records, pi_records
from tests.portability.test_codex import initialized
from tests.portability.test_native_adapters import roots
from tests.portability.test_opencode import ordinary_export, seed
from tests.portability.test_opencode import roots as opencode_roots
from tests.portability.test_service import service, source

AGENTS = ("claude-code", "codex", "opencode", "pi")


def target_roots(base, agent):
    return (
        initialized(base)
        if agent == "codex"
        else opencode_roots.__wrapped__(base)
        if agent == "opencode"
        else roots(base)
    )


@pytest.mark.parametrize("source_agent", AGENTS)
@pytest.mark.parametrize("target_agent", AGENTS)
@pytest.mark.parametrize("compact", [False, True])
def test_shared_supported_matrix_publication_readback_and_fork(
    tmp_path, source_agent, target_agent, compact
):
    worker = service(tmp_path)
    seed_request = ConversionRequest(
        source(tmp_path), source_agent, target_roots(tmp_path / "source-agent", source_agent)
    )
    seed_result = worker.convert(seed_request)
    seed_operation = next(op for op in worker.transactions() if op.id == seed_result.operation_id)
    stage = worker.store.artifacts(seed_operation).stage.native()
    request = ConversionRequest(
        SourceRequest(seed_result.target, stage.roots, stage.source_ref),
        target_agent,
        target_roots(tmp_path / "target-agent", target_agent),
        compact=compact,
    )
    snapshot = worker.adapters.reader(source_agent).snapshot(request.source)
    result = worker.convert(request)
    operation = next(op for op in worker.transactions() if op.id == result.operation_id)
    output = worker.store.artifacts(operation).stage.native()
    readback = worker.adapters.reader(target_agent).snapshot(
        SourceRequest(result.target, output.roots, output.source_ref)
    )
    expected = transform(snapshot.conversation).conversation if compact else snapshot.conversation
    compare(
        expected,
        readback.conversation,
        worker.adapters.writer(target_agent).normalizations(output.profile),
    )
    assert service(tmp_path).convert(request).target == result.target
    if source_agent == target_agent:
        forked = worker.convert(replace(request, fork=True))
        assert len({seed_result.target, result.target, forked.target}) == 3
        assert worker.lineage(forked.target)[0]["kind"] == "fork"


def independent_source(base, agent):
    source_roots = target_roots(base, agent)
    source_roots.cwd.mkdir(parents=True, exist_ok=True)
    if agent == "opencode":
        exported = ordinary_export(source_roots)
        seed(source_roots, exported)
        return SourceRequest(Identity(agent, exported["info"]["id"]), source_roots, "opencode.db")
    if agent == "codex":
        rows = codex_tool_records(source_roots.cwd)
        rows[0]["payload"]["base_instructions"] = {"text": "Keep the authored project policy"}
    elif agent == "claude-code":
        rows = claude_records(
            source_roots.cwd,
            [
                {"role": "user", "content": "Read the file"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "call", "name": "read", "input": {"path": "a"}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call",
                            "is_error": True,
                            "content": "permission denied",
                        }
                    ],
                },
                {"role": "assistant", "content": [{"type": "text", "text": "Could not read it"}]},
            ],
        )
    else:
        rows = pi_records(
            source_roots.cwd,
            [
                {"role": "user", "content": "Read the file"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call",
                            "name": "read",
                            "arguments": {"path": "a"},
                        }
                    ],
                    "stopReason": "toolUse",
                },
                {
                    "role": "toolResult",
                    "toolCallId": "call",
                    "toolName": "read",
                    "isError": True,
                    "content": [{"type": "text", "text": "permission denied"}],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Could not read it"}],
                    "stopReason": "stop",
                },
            ],
        )
    source_roots.store.mkdir(parents=True, exist_ok=True)
    (source_roots.store / "independent.jsonl").write_bytes(native.jsonl(rows))
    return SourceRequest(Identity(agent, SESSION_ID), source_roots, "independent.jsonl")


@pytest.mark.parametrize("source_agent", AGENTS)
@pytest.mark.parametrize("target_agent", AGENTS)
@pytest.mark.parametrize("compact", [False, True])
def test_independent_native_sources_convert_in_the_target_model_view(
    tmp_path, source_agent, target_agent, compact
):
    worker = service(tmp_path)
    request = ConversionRequest(
        independent_source(tmp_path / "native-source", source_agent),
        target_agent,
        target_roots(tmp_path / "target", target_agent),
        compact=compact,
    )
    result = worker.convert(request)
    assert result.lifecycle == "active"
    original = worker.adapters.reader(source_agent).snapshot(request.source).conversation
    operation = worker.transactions()[0]
    stage = worker.store.artifacts(operation).stage.native()
    readback = (
        worker.adapters.reader(target_agent)
        .snapshot(SourceRequest(result.target, stage.roots, stage.source_ref))
        .conversation
    )
    carried = portable(original, target_agent)
    expected = replace(
        transform(carried).conversation if compact else carried,
        title=original.title or "Imported session",
    )
    normalizations = worker.adapters.writer(target_agent).normalizations(stage.profile)
    compare(expected, readback, normalizations)
    if source_agent == "codex" and target_agent != "codex":
        streams = tuple(item for item in normalizations if item.kind != "model-view-history")
        assert len(original.history) != len(original.context)
        assert (
            preservation_projection(readback, streams)["history"]
            == preservation_projection(expected, streams)["context"]
        )
