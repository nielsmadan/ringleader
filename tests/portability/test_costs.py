from __future__ import annotations

import json
from typing import Any

from ringleader.portability import claude_chain, codex_paginated
from ringleader.portability import operations as operations_module
from ringleader.portability import service as service_module
from ringleader.portability.claude import ClaudeAdapter
from ringleader.portability.model import Identity
from ringleader.portability.operations import (
    OPERATION_VERSION,
    Artifacts,
    Operation,
    OperationStore,
    encode,
)
from tests.portability.test_claude_graph import assistant, text, user
from tests.portability.test_codex_paginated import (
    NEXT,
    SEALED,
    event,
    meta,
    model,
    read,
    visible,
)
from tests.portability.test_service import OfflineRunner, request, service


class CountingList(list):
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def _chain_scans(responses: int) -> int:
    rows = [user("u0", None, "start")]
    for index in range(responses):
        rows.append(assistant(f"a{index}", rows[-1]["uuid"], f"msg_{index}", text(f"a{index}")))
        rows.append(user(f"u{index + 1}", f"a{index}", f"q{index}"))
    entries = claude_chain.index(rows)
    path = CountingList(claude_chain.walk(entries, rows[-1]["uuid"]))

    recovered = claude_chain.recover(entries, path)

    assert [row["uuid"] for row in recovered] == [row["uuid"] for row in path]
    return path.iterations


def test_claude_chain_recovery_scans_the_path_a_constant_number_of_times():
    assert _chain_scans(5) == _chain_scans(40)


def _rollout(questions: int) -> list[dict[str, Any]]:
    asked = [
        model("user", ["user.text"], f"Question {index}", id=f"msg_{index}")
        for index in range(questions)
    ]
    rows = [meta(), event("task_started", started_at=1)]
    for index, question in enumerate(asked):
        rows += [
            question,
            visible(
                {"type": "UserMessage", "content": [{"type": "text", "text": f"Question {index}"}]}
            ),
            model("assistant", ["unknown"], f"Answer {index}.", phase="final_answer"),
        ]
    rows += [
        event("task_complete", completed_at=2),
        event("task_started", NEXT, started_at=3),
        {
            "timestamp": rows[0]["timestamp"],
            "ordinal": 0,
            "type": "compacted",
            "payload": {
                "message": "",
                "replacement_history": [*(item["payload"] for item in asked), {**SEALED}],
                "replacement_history_metadata": [
                    *({"client_authored": False} for _ in asked),
                    {"client_authored": False, "compaction_model_hash": "synthetic"},
                ],
                "window_number": 1,
                "first_window_id": "window-1",
                "previous_window_id": None,
                "window_id": "window-2",
                "compaction_response_id": "resp-synthetic",
                "guardian_history": {"items": []},
                "latest_token_usage_record": None,
                "retained_context": {"user_messages": [], "incomplete": False},
                "resume_metadata": {"last_started_turn_id": NEXT},
            },
        },
        event("task_complete", NEXT, completed_at=4),
    ]
    return rows


def _rollout_scans(questions: int, monkeypatch) -> int:
    created: list[CountingList] = []
    original = codex_paginated._Paginated

    def counted(*args: Any, **kwargs: Any) -> codex_paginated._Paginated:
        state = original(*args, **kwargs)
        state.full = CountingList(state.full)
        created.append(state.full)
        return state

    monkeypatch.setattr(codex_paginated, "_Paginated", counted)
    result = read(_rollout(questions))

    (checkpoint,) = result.checkpoints
    replaced = {item.id for item in checkpoint.replaced_context}
    assert len(checkpoint.retained_context) == questions
    assert all(item.id in replaced for item in checkpoint.retained_context)
    (full,) = created
    return full.iterations


def test_codex_compaction_resolves_retained_items_without_rescanning_the_rollout(monkeypatch):
    assert _rollout_scans(3, monkeypatch) == _rollout_scans(30, monkeypatch)


def test_a_first_conversion_probes_and_reads_back_the_target_once(tmp_path, monkeypatch):
    probes: list[tuple[str, ...]] = []
    reads: list[object] = []

    class CountingRunner(OfflineRunner):
        def run(self, request):
            probes.append(request.argv)
            return super().run(request)

    read_stage = ClaudeAdapter.read_stage

    def counting_read(self, stage):
        reads.append(stage.identity)
        return read_stage(self, stage)

    monkeypatch.setattr(ClaudeAdapter, "read_stage", counting_read)
    worker = service(tmp_path)
    worker.runner = CountingRunner()

    result = worker.convert(request(tmp_path, "claude"))

    assert result.lifecycle == "active"
    assert probes == [("claude", "--version")]
    assert reads == [result.target]


def test_a_publish_serializes_its_artifacts_once(tmp_path, monkeypatch):
    encoded: list[object] = []
    original = service_module.encode

    def counting(value: object) -> bytes:
        if isinstance(value, Artifacts):
            encoded.append(value)
        return original(value)

    monkeypatch.setattr(service_module, "encode", counting)
    monkeypatch.setattr(operations_module, "encode", counting)

    service(tmp_path).convert(request(tmp_path, "claude"))

    assert len(encoded) == 1


def _operation(index: int, source: str) -> Operation:
    return Operation(
        OPERATION_VERSION,
        f"{index:032x}",
        f"key-{source}",
        "digest",
        Identity("pi", source),
        Identity("claude-code", f"target-{index}"),
        f"2026-10-09T00:00:{index:02d}+00:00",
        "complete",
        "/registry.json",
    )


def _counting_loads(store: OperationStore, monkeypatch) -> list[str]:
    loads: list[str] = []
    load = store.load

    def counted(operation_id: str) -> Operation:
        loads.append(operation_id)
        return load(operation_id)

    monkeypatch.setattr(store, "load", counted)
    return loads


def test_operation_lookups_hit_the_index_instead_of_decoding_every_journal(tmp_path, monkeypatch):
    store = OperationStore(tmp_path / "operations")
    for index in range(12):
        store.create(_operation(index, "wanted" if index == 7 else f"other-{index}"), b"{}")
    loads = _counting_loads(store, monkeypatch)

    assert store.matching(key="key-wanted") == (f"{7:032x}",)
    assert store.matching(source=Identity("pi", "wanted"), target_agent="claude-code") == (
        f"{7:032x}",
    )
    assert loads == []


def test_a_missing_corrupt_or_stale_index_is_rebuilt_from_the_journals(tmp_path, monkeypatch):
    store = OperationStore(tmp_path / "operations")
    for index in range(3):
        store.create(_operation(index, f"source-{index}"), b"{}")
    index_file = store.root / "index.json"
    expected = store.matching(key="key-source-1")

    index_file.unlink()
    assert store.matching(key="key-source-1") == expected
    index_file.write_text("{not json")
    assert store.matching(key="key-source-1") == expected

    store.create(_operation(3, "source-3"), b"{}")
    stale = json.loads(index_file.read_text())
    del stale["operations"][f"{3:032x}"]
    index_file.write_text(json.dumps(stale))
    loads = _counting_loads(store, monkeypatch)
    assert store.matching(key="key-source-3") == (f"{3:032x}",)
    assert loads == [f"{3:032x}"]


def test_an_unreadable_journal_does_not_stop_create_or_reuse_lookups(tmp_path):
    store = OperationStore(tmp_path / "operations")
    store.create(_operation(0, "source-0"), b"{}")

    bad_id = "9" * 32
    bad_dir = store.root / bad_id
    bad_dir.mkdir(mode=0o700)
    payload = json.loads(encode(_operation(1, "source-1")).decode())
    payload["id"] = bad_id
    payload["version"] = 99
    (bad_dir / "journal.json").write_bytes(json.dumps(payload).encode())

    store.create(_operation(2, "source-2"), b"{}")

    assert store.matching(key="key-source-0") == (f"{0:032x}",)
    assert store.matching(key="key-source-2") == (f"{2:032x}",)
