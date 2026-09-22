from __future__ import annotations

import importlib.util
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from ringleader import cli, refresh, registry, scanner
from ringleader.inbox.app import InboxApp
from ringleader.portability import native
from ringleader.portability.capabilities import SourceRequest
from ringleader.portability.claude import read as read_claude
from ringleader.portability.codex import CodexAdapter
from ringleader.portability.model import Identity, PreservationError
from ringleader.portability.operations import Artifacts
from ringleader.portability.service import ConversionService
from tests.portability.fixtures import SESSION_ID, claude_records, codex_tool_records
from tests.portability.test_cli import environment as cli_environment
from tests.portability.test_codex import initialized
from tests.portability.test_native_adapters import roots
from tests.portability.test_opencode import ordinary_export, snapshot
from tests.portability.test_opencode import roots as opencode_roots
from tests.portability.test_opencode import seed as opencode_seed
from tests.portability.test_service import Crash, OfflineRunner, crash_at, request, service, tree
from tests.test_inbox import FakeModel, _setup
from tests.test_registry_concurrency import seed


@pytest.fixture
def environment(tmp_path, monkeypatch):
    return cli_environment.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "kind,subtype,diagnostic",
    [
        ("system", "compact_boundary", "missing its summary"),
        ("attachment", None, "attachment records are unsupported"),
        ("system", "api_error", "Unsupported Claude system content"),
    ],
)
def test_claude_default_selection_validates_terminal_records(tmp_path, kind, subtype, diagnostic):
    rows = claude_records(
        tmp_path,
        [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}],
    )
    trailing = {
        "type": kind,
        "uuid": "tail",
        "parentUuid": "entry-1",
        "sessionId": SESSION_ID,
        "subtype": subtype,
    }
    if subtype == "compact_boundary":
        trailing.update(
            parentUuid=None,
            logicalParentUuid="entry-1",
            compactMetadata={"trigger": "auto", "preTokens": 10},
        )
    rows.append(trailing)
    with pytest.raises(PreservationError, match=diagnostic):
        read_claude(native.jsonl(rows), Identity("claude-code", SESSION_ID))
    assert (
        len(read_claude(native.jsonl(rows), Identity("claude-code", SESSION_ID), "entry-1").history)
        == 2
    )


def test_claude_terminal_telemetry_stays_on_selected_branch(tmp_path):
    rows = claude_records(tmp_path, [{"role": "user", "content": "question"}])
    rows.append(
        {
            "type": "system",
            "subtype": "turn_duration",
            "uuid": "tail",
            "parentUuid": "entry-0",
            "sessionId": SESSION_ID,
        }
    )
    value = read_claude(native.jsonl(rows), Identity("claude-code", SESSION_ID))
    assert value.branch_id == "tail" and value.history[0].content[0].text == "question"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["activity", "relocation"])
async def test_inbox_delete_revalidates_original_candidate(tmp_path, monkeypatch, change):
    path = _setup(tmp_path, monkeypatch)
    original = tmp_path / "s0.jsonl"
    relocated = tmp_path / "new.jsonl"
    relocated.write_text("new work")
    app = InboxApp(registry_path=str(path))
    async with app.run_test() as pilot:
        await pilot.press("d")

        def update(reg):
            row = registry.find_session(reg, "s0")
            row.update(mtime="2999-01-01T00:00:00+00:00", messageCount=500)
            if change == "relocation":
                row.update(file=str(relocated), project=str(tmp_path / "new-project"))

        registry.mutate_registry(path, update)
        await pilot.press("q")
    assert original.read_text() and relocated.read_text() == "new work"
    assert registry.find_session(registry.load_registry(path), "s0")["messageCount"] == 500


def test_codex_snapshot_native_names_and_rename_identity(tmp_path):
    target = roots(tmp_path)
    target.store.mkdir(parents=True)
    file = target.store / "source.jsonl"
    file.write_bytes(native.jsonl(codex_tool_records(target.cwd)))
    source = SourceRequest(Identity("codex", SESSION_ID), target, str(file))
    index = target.store / "session_index.jsonl"
    index.write_text(json.dumps({"id": SESSION_ID, "thread_name": "Native title"}) + "\n")
    first = CodexAdapter().snapshot(source)
    assert first.conversation.title == "Native title"
    with index.open("a") as stream:
        stream.write(json.dumps({"id": SESSION_ID, "thread_name": "Renamed"}) + "\n")
    second = CodexAdapter().snapshot(source)
    assert second.conversation.title == "Renamed" and second.identity() != first.identity()
    with index.open("a") as stream:
        stream.write(json.dumps({"id": "other", "thread_name": "Unrelated"}) + "\n")
    assert CodexAdapter().snapshot(source).identity() == second.identity()


def test_renamed_imported_codex_uses_native_name(environment):
    conversion = request(environment, "codex", target=initialized(environment / "target"))
    worker = service(environment)
    result = worker.convert(conversion)
    stage = worker.store.artifacts(worker.transactions()[0]).stage.native()
    source = SourceRequest(result.target, stage.roots, stage.source_ref)
    scanner.source_for_thread(
        {"source": "codex", "nativeRoots": {"store": str(stage.roots.store)}}
    ).apply_name(
        result.target.native_id, str(stage.roots.store / stage.source_ref), "Native rename"
    )
    assert CodexAdapter().snapshot(source).conversation.title == "Native rename"


@pytest.mark.parametrize("scan_command", ["refresh", "auto_refresh", "enrich", "prune"])
def test_persisted_roots_scan_and_lifecycle(environment, scan_command, monkeypatch):
    alternate = initialized(environment / "alternate")
    current = initialized(environment / "current")
    monkeypatch.setenv("CODEX_HOME", str(current.store))
    other_worker = service(environment / "unrelated")
    other_result = other_worker.convert(request(environment / "unrelated", "codex", target=current))
    worker = service(environment)
    conversion = request(environment, "codex", target=alternate)
    result = worker.convert(conversion)
    identifier = result.target.native_id
    path = worker.registry_path

    def state():
        with closing(sqlite3.connect(alternate.store / "state_5.sqlite")) as db, db:
            return db.execute(
                "SELECT archived,cwd FROM threads WHERE id=?", (identifier,)
            ).fetchone()

    if scan_command == "refresh":
        assert cli.main(["session", "refresh", "--registry", str(path)]) == 0
    elif scan_command == "auto_refresh":
        refresh.auto_refresh(path, "2026-09-22T00:00:00+00:00")
    elif scan_command == "enrich":
        from ringleader.commands import enrich

        monkeypatch.setattr(enrich, "active_model", lambda _: FakeModel())
        assert cli.main(["session", "enrich", "--registry", str(path), "--limit", "0"]) == 0

    else:
        assert cli.main(["prune", "--registry", str(path)]) == 0
    record = registry.find_session(registry.load_registry(path), identifier)
    assert record is not None and Path(record["file"]).is_file()
    assert state()[0] == 0
    assert (
        cli.main(
            [
                "session",
                "archive",
                identifier,
                "--registry",
                str(path),
                "--archive-root",
                str(environment / "archive"),
            ]
        )
        == 0
    )
    assert state()[0] == 1
    assert cli.main(["session", "unarchive", identifier, "--registry", str(path)]) == 0
    assert state()[0] == 0
    record = registry.find_session(registry.load_registry(path), identifier)
    scanner.rebind_workspace(record, record["file"], str(environment / "relocated"))
    assert state()[1] == str(environment / "relocated")
    assert cli.main(["session", "delete", identifier, "--registry", str(path)]) == 0
    assert state()[0] == 1
    with closing(sqlite3.connect(current.store / "state_5.sqlite")) as db, db:
        assert db.execute("SELECT id,archived FROM threads").fetchall() == [
            (other_result.target.native_id, 0)
        ]


@pytest.mark.parametrize("exists", [False, True])
def test_opencode_tool_arguments_never_read_file_shaped_objects(tmp_path, exists):
    target = opencode_roots.__wrapped__(tmp_path)
    unrelated = tmp_path / "private.txt"
    if exists:
        unrelated.write_bytes(b"private marker")
    export = ordinary_export(target)
    export["messages"][1]["parts"][1]["state"]["input"]["example"] = {
        "type": "file",
        "url": unrelated.as_uri(),
    }
    opencode_seed(target, export)
    value = snapshot(target)
    assert value.conversation.attachments == ()
    assert [file.name for file in value.files] == ["graph.json"]
    assert (
        json.loads(value.conversation.history[1].content[1].arguments)["example"]["url"]
        == unrelated.as_uri()
    )


def test_conversion_reuse_key_computed_once_for_unrelated_journals(tmp_path, monkeypatch):
    conversion = request(tmp_path)
    for _ in range(5):
        worker = service(tmp_path, fault=crash_at("after:create"))
        try:
            worker.convert(replace(conversion, fork=True))
        except Crash as crash:
            service(tmp_path).rollback(str(crash))
    original = Artifacts.key
    calls = []

    def counted(artifacts):
        calls.append(artifacts)
        return original(artifacts)

    monkeypatch.setattr(Artifacts, "key", counted)
    assert service(tmp_path).convert(conversion).lifecycle == "active"
    assert len(calls) == 1


@pytest.mark.parametrize("verb", ["keep", "archive"])
def test_legacy_live_row_wins_over_old_gc_tombstone(environment, verb):
    path = seed(environment)
    value = json.loads(path.read_text())
    value["deleted"] = [
        {
            "sessionId": "same",
            "source": "claude-code",
            "reason": "gc",
            "thread": deepcopy(value["threads"][0]),
        }
    ]
    path.write_text(json.dumps(value))
    args = ["session", verb, "same", "--registry", str(path)]
    if verb == "archive":
        args += ["--archive-root", str(environment / "archive")]
    assert cli.main(args) == 0
    saved = registry.load_registry(path)
    if verb == "keep":
        assert saved["threads"][0]["decision"] == "keep"
    else:
        assert Path(saved["archived"][0]["archivePath"]).is_file() and saved["threads"] == []
    assert saved["deleted"] == []


def test_bulk_registry_merge_preserves_order_with_linear_comparisons():
    class Row(dict):
        __hash__ = None
        comparisons = 0

        def __eq__(self, other):
            Row.comparisons += 1
            return super().__eq__(other)

        def __ne__(self, other):
            return not self == other

    before = registry.empty_registry()
    before["threads"] = [Row(sessionId=str(n), source="pi", title="old") for n in range(1000)]
    after = deepcopy(before)
    current = deepcopy(before)
    for row in after["threads"]:
        row["title"] = "updated"
    registry._merge_sessions(before, after, current, scan=True)
    assert [(r["sessionId"], r["title"]) for r in current["threads"]] == [
        (str(n), "updated") for n in range(1000)
    ]
    assert Row.comparisons < 20000


def test_public_recovery_and_reuse_refuse_different_registry(environment, capsys):
    conversion = request(environment)
    worker = service(environment, fault=crash_at("after:create"))
    with pytest.raises(Crash):
        worker.convert(conversion)
    operation = worker.transactions()[0]
    wrong = environment / "wrong-registry.json"
    before = tree(environment)
    assert (
        cli.main(
            [
                "data",
                "transaction",
                "resume",
                operation.id,
                "--registry",
                str(wrong),
                "--operations",
                str(worker.store.root),
                "--json",
            ]
        )
        == 1
    )
    assert str(worker.registry_path.resolve()) in json.loads(capsys.readouterr().out)["error"]
    assert tree(environment) == before
    assert (
        cli.main(
            [
                "data",
                "transaction",
                "resume",
                operation.id,
                "--registry",
                str(worker.registry_path),
                "--operations",
                str(worker.store.root),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["lifecycle"] == "active"
    other = ConversionService(
        worker.adapters, OfflineRunner(), registry_path=wrong, operations_path=worker.store.root
    )
    with pytest.raises(PreservationError, match="belongs to registry"):
        other.convert(conversion)
    assert service(environment).convert(conversion).reused
    assert len(worker.transactions()) == 1 and not wrong.exists()


@pytest.fixture
def codex_oracle():
    spec = importlib.util.spec_from_file_location(
        "codex_native_smoke", Path("scripts/smoke-portability-codex.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def oracle_conversation():
    import base64

    from tests.portability.fixtures import conversation

    original = conversation()
    return replace(
        original,
        attachments=(
            replace(
                original.attachments[0],
                data=base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                ),
            ),
        ),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "tool_image",
        "question",
        "prose_order",
        "user_image",
        "instruction",
        "argument",
        "status",
        "pair",
        "role",
        "extra_history",
    ],
)
def test_native_model_oracle_rejects_corrupted_preservation(codex_oracle, mutation):
    body = json.loads(Path("tests/portability/codex-smoke-payload.json").read_text())
    original = oracle_conversation()
    codex_oracle.assert_model(body, original, Path("/native-probe/cwd"))
    before = deepcopy(body)
    items = body["input"]
    if mutation == "tool_image":
        items[6]["output"][2]["image_url"] = "data:image/png;base64,Y29ycnVwdA=="
    elif mutation == "question":
        del items[1]
    elif mutation == "prose_order":
        items[2]["content"], items[7]["content"] = items[7]["content"], items[2]["content"]
    elif mutation == "user_image":
        items[1]["content"].pop()
    elif mutation == "instruction":
        items[0]["content"][0]["text"] = items[0]["content"][0]["text"].replace(
            "Preserve my formatting.", "Changed instruction"
        )
    elif mutation == "argument":
        items[3]["arguments"] = '{"file":"wrong"}'
    elif mutation == "status":
        items[6]["output"][0]["text"] = "[Historical tool result: succeeded]"
    elif mutation == "pair":
        items[5]["call_id"] = "wrong"
    elif mutation == "role":
        items[2]["role"] = "user"
    else:
        items.insert(0, deepcopy(items[0]))
    assert body != before
    with pytest.raises(AssertionError):
        codex_oracle.assert_model(body, original, Path("/native-probe/cwd"))


@pytest.mark.parametrize(
    "mutation", ["user_image", "tool_image", "instruction", "prose_order", "argument", "status"]
)
def test_native_visible_oracle_rejects_corruption(codex_oracle, mutation):
    thread = json.loads(Path("tests/portability/codex-smoke-visible.json").read_text())
    original = oracle_conversation()
    codex_oracle.assert_visible(thread, original)
    before = deepcopy(thread)
    items = [item for turn in thread["turns"] for item in turn["items"]]
    if mutation == "user_image":
        items[1]["content"][1]["url"] = "data:image/png;base64,Y29ycnVwdA=="
    elif mutation == "tool_image":
        items[4]["result"]["content"][1]["data"] = "Y29ycnVwdA=="
    elif mutation == "instruction":
        items[0]["content"][0]["text"] = "[Historical user instruction] altered"
    elif mutation == "prose_order":
        items[2]["text"], items[5]["text"] = items[5]["text"], items[2]["text"]
    elif mutation == "argument":
        items[3]["arguments"] = {"file": "wrong"}
    else:
        items[4]["status"] = "completed"
    assert thread != before
    with pytest.raises(AssertionError):
        codex_oracle.assert_visible(thread, original)
