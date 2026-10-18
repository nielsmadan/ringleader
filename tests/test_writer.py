import threading
import time

from ringleader import decisions, prune, registry
from ringleader.decisions import (
    ArchiveOp,
    ClassifyOp,
    DeleteOp,
    KeepGoingOp,
    KeepOp,
    OpContext,
    Outcome,
    ReloadOp,
    SnoozeOp,
)
from ringleader.home import writer as writer_module
from ringleader.home.writer import (
    GROUP_LIMIT,
    Batch,
    Drained,
    Finished,
    OpProgress,
    Refused,
    Started,
    Writer,
)
from ringleader.revalidate import Seen
from ringleader.roundup import RoundupProgress

WAIT = 5.0


def _seen(sid):
    return Seen(("claude-code", sid), sid, "2026-06-01T00:00:00+00:00", None)


class Harness:
    def __init__(self, tmp_path, **options):
        self.cond = threading.Condition()
        self.events = []
        self.ran = []
        self.running = 0
        self.overlap = 0
        self.gates = {}
        self.entered = {}
        self.outcomes = {}
        self.raises = {}
        self.cancel_seen = {}
        self.progress_updates = {}
        self.failing_delivery = None
        self.groups = []
        self.registry_path = str(tmp_path / "registry.json")
        reg = registry.empty_registry()
        reg["threads"] = [{"sessionId": "on-disk", "source": "claude-code", "mtime": "x"}]
        registry.save_registry(self.registry_path, reg)
        self.writer = Writer(
            self.registry_path,
            executor=self,
            make_context=self.make_context,
            deliver=self.deliver,
            **{"delete_grace": 0.0, "error_log": tmp_path / "errors.log", **options},
        )
        self.writer.start()

    def make_context(self, cancelled, progress):
        return OpContext(
            registry_path=self.registry_path,
            archive_root_path="unused",
            now_iso=lambda: "now",
            now_epoch=time.time,
            presence=frozenset,
            unknown="refuse",
            cancelled=cancelled,
            progress=progress,
        )

    def gate(self, op):
        self.gates[op] = threading.Event()
        self.entered[op] = threading.Event()
        return self.gates[op]

    def execute(self, op, ctx):
        with self.cond:
            self.ran.append(op)
            self.running += 1
            self.overlap = max(self.overlap, self.running)
        if op in self.progress_updates:
            ctx.progress(self.progress_updates[op])
        if op in self.entered:
            self.entered[op].set()
            self.gates[op].wait(WAIT)
        self.cancel_seen[op] = ctx.cancelled()
        with self.cond:
            self.running -= 1
        if op in self.raises:
            raise self.raises[op]
        return self.outcomes.get(op, Outcome(ok=True, message="ok", wrote=True))

    def execute_group(self, ops, ctx, claim):
        with self.cond:
            self.groups.append(list(ops))
        results = []
        for position, op in enumerate(ops):
            if not claim(position):
                results.append(None)
                continue
            try:
                results.append(self.execute(op, ctx))
            except Exception as error:  # noqa: BLE001 -- reported per op, like the real executor
                results.append(error)
        return results

    def deliver(self, event):
        if self.failing_delivery is not None and isinstance(event, self.failing_delivery):
            self.failing_delivery = None
            raise RuntimeError("consumer failed")
        with self.cond:
            self.events.append((time.monotonic(), event))
            self.cond.notify_all()

    def wait_for(self, predicate):
        with self.cond:
            assert self.cond.wait_for(predicate, WAIT)

    def drained(self, count=1):
        self.wait_for(lambda: sum(isinstance(e, Drained) for _, e in self.events) >= count)

    def finished(self):
        return [e for _, e in self.events if isinstance(e, Finished)]

    def started_at(self, op):
        return next(t for t, e in self.events if isinstance(e, Started) and e.op == op)


def test_waiting_counts_only_ops_that_have_not_started(tmp_path):
    h = Harness(tmp_path)
    first, second, third = ArchiveOp(_seen("a")), KeepOp(_seen("b")), ArchiveOp(_seen("c"))
    gate = h.gate(first)
    try:
        h.writer.submit([first, second, third], label="3 sessions")
        assert h.entered[first].wait(WAIT)
        assert (h.writer.waiting, h.writer.unfinished) == (2, 3)
    finally:
        gate.set()
    h.drained()


def test_ops_run_one_at_a_time_in_fifo_order(tmp_path):
    h = Harness(tmp_path)
    first, second, third = ArchiveOp(_seen("a")), KeepOp(_seen("b")), ArchiveOp(_seen("c"))
    gate = h.gate(first)
    try:
        h.writer.submit([first], label="archive a")
        h.writer.submit([second, third], label="2 sessions")
        assert h.entered[first].wait(WAIT)
        assert (h.ran, h.writer.unfinished, h.writer.idle) == ([first], 3, False)
    finally:
        gate.set()
    h.drained()
    assert (h.ran, h.overlap, h.writer.idle) == ([first, second, third], 1, True)


def test_a_delete_waits_its_grace_while_later_ops_run(tmp_path):
    now = [0.0]
    h = Harness(tmp_path, delete_grace=10.0, clock=lambda: now[0])
    delete, archive = DeleteOp(_seen("a")), ArchiveOp(_seen("b"))
    h.writer.submit([delete], label="delete a")
    h.writer.submit([archive], label="archive b")
    h.wait_for(lambda: len(h.finished()) == 1)
    assert h.ran == [archive]
    assert h.writer.unfinished == 1
    now[0] = 10.0
    h.writer.flush_grace()
    h.drained()
    assert h.ran == [archive, delete]


def test_undo_removes_only_the_newest_pending_batch(tmp_path):
    h = Harness(tmp_path)
    running, older, newer, newest = (ArchiveOp(_seen(s)) for s in ("a", "b", "c", "d"))
    gate = h.gate(running)
    try:
        h.writer.submit([running], label="archive a")
        assert h.entered[running].wait(WAIT)
        older_batch = h.writer.submit([older], label="archive b")
        newer_batch = h.writer.submit([newer, newest], label="archive 2 sessions")
        assert h.writer.undo_last() == newer_batch.tickets
        assert h.writer.undo_last() == older_batch.tickets
        assert h.writer.undo_last() == ()
    finally:
        gate.set()
    h.drained()
    assert h.ran == [running]


def test_undo_skips_batches_the_app_submitted_for_itself(tmp_path):
    h = Harness(tmp_path)
    running, decided = ArchiveOp(_seen("a")), KeepOp(_seen("b"))
    gate = h.gate(running)
    try:
        h.writer.submit([running], label="archive a")
        assert h.entered[running].wait(WAIT)
        decided_batch = h.writer.submit([decided], label="keep b")
        h.writer.submit([ClassifyOp(())], label="classify")
        h.writer.submit([ReloadOp()], label="reload")
        assert h.writer.undo_last() == decided_batch.tickets
    finally:
        gate.set()
    h.drained()
    assert h.ran == [running, ClassifyOp(()), ReloadOp()]


def test_flush_grace_releases_a_waiting_delete(tmp_path):
    h = Harness(tmp_path, delete_grace=600.0)
    delete = DeleteOp(_seen("a"))
    h.writer.submit([delete], label="delete a")
    assert (h.writer.unfinished, h.writer.idle) == (1, False)
    h.writer.flush_grace()
    h.drained()
    assert h.ran == [delete]


def test_discard_pending_leaves_the_running_op_running(tmp_path):
    h = Harness(tmp_path)
    running, queued, later = ArchiveOp(_seen("a")), KeepOp(_seen("b")), KeepOp(_seen("c"))
    gate = h.gate(running)
    try:
        first = h.writer.submit([running], label="archive a")
        assert h.entered[running].wait(WAIT)
        rest = h.writer.submit([queued, later], label="keep 2 sessions")
        assert h.writer.discard_pending() == rest.tickets
    finally:
        gate.set()
    h.drained()
    assert h.ran == [running]
    assert [f.ticket for f in h.finished()] == [first.tickets[0][0]]


def test_cancel_current_reaches_only_the_running_op(tmp_path):
    h = Harness(tmp_path)
    running, after = ArchiveOp(_seen("a")), KeepOp(_seen("b"))
    gate = h.gate(running)
    try:
        h.writer.submit([running, after], label="2 sessions")
        assert h.entered[running].wait(WAIT)
        h.writer.cancel_current()
    finally:
        gate.set()
    h.drained()
    assert h.cancel_seen == {running: True, after: False}


def test_join_current_waits_for_the_running_op(tmp_path):
    h = Harness(tmp_path)
    running = ArchiveOp(_seen("a"))
    gate = h.gate(running)
    try:
        h.writer.submit([running], label="archive a")
        assert h.entered[running].wait(WAIT)
        assert h.writer.join_current(timeout=0.01) is False
    finally:
        gate.set()
    assert h.writer.join_current(timeout=WAIT) is True


def test_generation_counts_only_writes(tmp_path):
    h = Harness(tmp_path)
    wrote, idle, wrote_again = (KeepOp(_seen(s)) for s in ("a", "b", "c"))
    h.outcomes[idle] = Outcome(ok=False, message="refused", wrote=False)
    h.writer.submit([wrote, idle, wrote_again], label="3 sessions")
    h.drained()
    assert [f.generation for f in h.finished()] == [1, 1, 2]
    assert h.writer.generation == 2


def test_finished_carries_a_reload_snapshot_once_the_queue_is_empty(tmp_path):
    h = Harness(tmp_path)
    first, last = KeepOp(_seen("a")), KeepOp(_seen("b"))
    gate = h.gate(first)
    try:
        h.writer.submit([first, last], label="2 sessions")
    finally:
        gate.set()
    h.drained()
    early, final = h.finished()
    assert early.snapshot is None
    assert final.snapshot is not None
    assert (final.snapshot.generation, final.snapshot.scanned) == (2, True)
    assert [t["sessionId"] for t in final.snapshot.reg["threads"]] == ["on-disk"]


def test_a_reload_op_always_carries_a_snapshot(tmp_path):
    h = Harness(tmp_path)
    reload_op, after = ReloadOp(), KeepOp(_seen("a"))
    h.outcomes[reload_op] = Outcome(ok=True, message="", wrote=False)
    gate = h.gate(reload_op)
    try:
        h.writer.submit([reload_op], label="reload")
        assert h.entered[reload_op].wait(WAIT)
        h.writer.submit([after], label="keep a")
    finally:
        gate.set()
    h.drained()
    reload, _ = h.finished()
    assert (reload.op, reload.outcome.wrote, reload.snapshot.generation) == (reload_op, False, 0)


def test_read_only_refuses_everything_but_a_reload(tmp_path):
    h = Harness(tmp_path, read_only=True)
    refused = h.writer.submit([ArchiveOp(_seen("a"))], label="archive a")
    reload = h.writer.submit([ReloadOp()], label="reload")
    assert refused == Refused("read-only: archive a is disabled")
    assert isinstance(reload, Batch)
    h.drained()
    assert h.ran == [ReloadOp()]


def test_an_exception_becomes_an_internal_error_outcome_and_a_log_line(tmp_path):
    h = Harness(tmp_path)
    boom = ArchiveOp(_seen("a"))
    error = ValueError("boom")
    h.raises[boom] = error
    h.writer.submit([boom], label="archive a")
    h.drained()
    [finished] = h.finished()
    assert (finished.outcome.ok, finished.outcome.wrote, finished.outcome.message) == (
        False,
        False,
        "internal error: ValueError: boom",
    )
    assert finished.internal is error
    log = (tmp_path / "errors.log").read_text()
    assert "ArchiveOp (archive a)" in log
    assert "ValueError: boom" in log


def test_join_current_does_not_return_while_a_ready_op_is_queued(tmp_path):
    h = Harness(tmp_path)
    idle_writer = Writer(
        h.registry_path,
        executor=h,
        make_context=h.make_context,
        deliver=h.deliver,
        delete_grace=0.0,
        error_log=tmp_path / "errors.log",
    )
    idle_writer.submit([KeepOp(_seen("b"))], label="keep b")
    assert idle_writer.join_current(timeout=0.01) is False


def test_op_progress_reaches_deliver_with_the_running_ticket(tmp_path):
    h = Harness(tmp_path)
    op = ArchiveOp(_seen("a"))
    update = RoundupProgress("prune", 1, 3)
    h.progress_updates[op] = update
    batch = h.writer.submit([op], label="archive a")
    h.drained()
    [progress] = [e for _, e in h.events if isinstance(e, OpProgress)]
    assert progress == OpProgress(batch.tickets[0][0], update)


def test_drained_arrives_once_per_emptying(tmp_path):
    h = Harness(tmp_path)
    first, second = KeepOp(_seen("a")), KeepOp(_seen("b"))
    h.writer.submit([first, second], label="2 sessions")
    h.drained()
    h.writer.submit([KeepOp(_seen("c"))], label="keep c")
    h.drained(2)
    assert sum(isinstance(e, Drained) for _, e in h.events) == 2


def test_discard_pending_delivers_drained_when_nothing_runs(tmp_path):
    h = Harness(tmp_path, delete_grace=600.0)
    h.writer.submit([DeleteOp(_seen("a"))], label="delete a")
    assert sum(isinstance(e, Drained) for _, e in h.events) == 0
    h.writer.discard_pending()
    assert sum(isinstance(e, Drained) for _, e in h.events) == 1
    assert h.writer.idle


def test_undo_delivers_drained_when_it_empties_an_idle_queue(tmp_path):
    h = Harness(tmp_path, delete_grace=600.0)
    h.writer.submit([DeleteOp(_seen("a"))], label="delete a")
    h.writer.undo_last()
    assert sum(isinstance(e, Drained) for _, e in h.events) == 1


def test_discard_while_an_op_runs_leaves_drained_to_the_op(tmp_path):
    h = Harness(tmp_path)
    running, queued = ArchiveOp(_seen("a")), KeepOp(_seen("b"))
    gate = h.gate(running)
    try:
        h.writer.submit([running], label="archive a")
        assert h.entered[running].wait(WAIT)
        h.writer.submit([queued], label="keep b")
        h.writer.discard_pending()
        assert sum(isinstance(e, Drained) for _, e in h.events) == 0
    finally:
        gate.set()
    h.drained()
    assert sum(isinstance(e, Drained) for _, e in h.events) == 1


def test_the_writer_survives_a_failing_consumer(tmp_path):
    h = Harness(tmp_path)
    h.failing_delivery = Started
    first, second = KeepOp(_seen("a")), KeepOp(_seen("b"))
    h.writer.submit([first], label="keep a")
    h.drained()
    h.writer.submit([second], label="keep b")
    h.drained(2)
    assert (h.ran, h.writer.idle, h.writer.unfinished) == ([second], True, 0)
    assert "consumer failed" in (tmp_path / "errors.log").read_text()


def _grouped(tmp_path, **options):
    h = Harness(tmp_path)
    h.writer = Writer(
        h.registry_path,
        executor=h,
        make_context=h.make_context,
        deliver=h.deliver,
        error_log=tmp_path / "grouped.log",
        **{"delete_grace": 0.0, **options},
    )
    return h


def test_consecutive_ready_ops_of_one_kind_run_as_one_group(tmp_path):
    h = _grouped(tmp_path)
    keep, snooze, going = KeepOp(_seen("a")), SnoozeOp(_seen("b"), "x"), KeepGoingOp(_seen("c"))
    archive, last = ArchiveOp(_seen("d")), KeepOp(_seen("e"))
    h.writer.submit([keep, snooze, going, archive, last], label="5 sessions")
    h.writer.start()
    h.drained()
    assert h.groups == [[keep, snooze, going], [archive], [last]]
    assert [f.op for f in h.finished()] == [keep, snooze, going, archive, last]


def test_a_group_reports_one_finished_per_op_with_rising_generations(tmp_path):
    h = _grouped(tmp_path)
    ops = [KeepOp(_seen(s)) for s in ("a", "b", "c")]
    h.outcomes[ops[1]] = Outcome(ok=False, message="refused", wrote=False)
    batch = h.writer.submit(ops, label="3 sessions")
    h.writer.start()
    h.drained()
    finished = h.finished()
    assert [(f.ticket, f.outcome.message, f.generation) for f in finished] == [
        (batch.tickets[0][0], "ok", 1),
        (batch.tickets[1][0], "refused", 1),
        (batch.tickets[2][0], "ok", 2),
    ]
    assert [f.snapshot is not None for f in finished] == [False, False, True]
    started = [e.ticket for _, e in h.events if isinstance(e, Started)]
    assert started == [ticket for ticket, _ in batch.tickets]


def test_a_group_stops_at_the_group_limit(tmp_path):
    h = _grouped(tmp_path)
    ops = [KeepOp(_seen(f"s{i}")) for i in range(GROUP_LIMIT + 2)]
    h.writer.submit(ops, label="many")
    h.writer.start()
    h.drained()
    assert GROUP_LIMIT == prune.DELETE_BATCH
    assert [len(group) for group in h.groups] == [GROUP_LIMIT, 2]


def test_an_exception_for_one_op_in_a_group_is_that_ops_internal_error(tmp_path):
    h = _grouped(tmp_path)
    boom, fine = ArchiveOp(_seen("a")), ArchiveOp(_seen("b"))
    error = ZeroDivisionError("bug")
    h.raises[boom] = error
    h.writer.submit([boom, fine], label="2 sessions")
    h.writer.start()
    h.drained()
    first, second = h.finished()
    assert (first.internal, first.outcome.message) == (
        error,
        "internal error: ZeroDivisionError: bug",
    )
    assert (second.internal, second.outcome.message) == (None, "ok")
    assert "ZeroDivisionError: bug" in (tmp_path / "grouped.log").read_text()


def test_a_failed_group_logs_its_traceback_once(tmp_path):
    h = _grouped(tmp_path)

    def broken(ops, ctx, claim):
        raise ZeroDivisionError("whole group")

    h.execute_group = broken
    h.writer.submit([KeepOp(_seen("a")), KeepOp(_seen("b"))], label="2 sessions")
    h.writer.start()
    h.drained()
    assert [f.outcome.message for f in h.finished()] == [
        "internal error: ZeroDivisionError: whole group",
        "internal error: ZeroDivisionError: whole group",
    ]
    assert (tmp_path / "grouped.log").read_text().count("Traceback") == 1


def _gated_first(h):
    gate = threading.Event()
    entered = threading.Event()
    execute = h.execute

    def first_waits(op, ctx):
        if not entered.is_set():
            entered.set()
            gate.wait(WAIT)
        return execute(op, ctx)

    h.execute = first_waits
    return gate, entered


def test_ops_of_a_running_group_that_have_not_started_can_be_undone(tmp_path):
    h = _grouped(tmp_path)
    gate, entered = _gated_first(h)
    first = h.writer.submit([KeepOp(_seen("a"))], label="keep a")
    rest = h.writer.submit([KeepOp(_seen(s)) for s in ("b", "c")], label="keep 2 sessions")
    h.writer.start()
    try:
        assert entered.wait(WAIT)
        assert h.writer.waiting == 2
        assert h.writer.undo_last() == rest.tickets
        assert h.writer.cancel(first.tickets[0][0]) == "running"
    finally:
        gate.set()
    h.drained()
    assert h.groups == [[KeepOp(_seen(s)) for s in ("a", "b", "c")]]
    assert h.ran == [KeepOp(_seen("a"))]
    assert [f.ticket for f in h.finished()] == [first.tickets[0][0]]


def test_cancelling_an_op_of_a_running_group_drops_it_before_its_turn(tmp_path):
    h = _grouped(tmp_path)
    gate, entered = _gated_first(h)
    ops = [KeepOp(_seen(s)) for s in ("a", "b", "c")]
    batch = h.writer.submit(ops, label="3 sessions")
    h.writer.start()
    try:
        assert entered.wait(WAIT)
        assert h.writer.cancel(batch.tickets[1][0]) == "dropped"
        assert h.writer.cancel(batch.tickets[0][0]) == "running"
    finally:
        gate.set()
    h.drained()
    assert h.ran == [ops[0], ops[2]]
    assert [f.op for f in h.finished()] == [ops[0], ops[2]]
    assert h.cancel_seen == {ops[0]: True, ops[2]: False}


def test_discarding_while_a_group_runs_leaves_only_the_started_op(tmp_path):
    h = _grouped(tmp_path)
    gate, entered = _gated_first(h)
    ops = [DeleteOp(_seen(s)) for s in ("a", "b", "c")]
    batch = h.writer.submit(ops, label="delete 3 sessions")
    h.writer.flush_grace()
    h.writer.start()
    try:
        assert entered.wait(WAIT)
        assert h.writer.discard_pending() == batch.tickets[1:]
    finally:
        gate.set()
    h.drained()
    assert h.ran == [ops[0]]
    assert sum(isinstance(e, Drained) for _, e in h.events) == 1


def test_cancelling_a_queued_op_before_its_group_forms_leaves_it_out(tmp_path):
    h = _grouped(tmp_path)
    ops = [KeepOp(_seen(s)) for s in ("a", "b", "c")]
    batch = h.writer.submit(ops, label="3 sessions")
    assert h.writer.cancel(batch.tickets[1][0]) == "dropped"
    h.writer.start()
    h.drained()
    assert h.groups == [[ops[0], ops[2]]]


def test_a_ready_mark_overtakes_an_earlier_delete_still_in_its_grace(tmp_path):
    now = [0.0]
    h = _grouped(tmp_path, delete_grace=10.0, clock=lambda: now[0])
    delete, keep = DeleteOp(_seen("a")), KeepOp(_seen("b"))
    h.writer.submit([delete], label="delete a")
    h.writer.submit([keep], label="keep b")
    h.writer.start()
    h.wait_for(lambda: len(h.finished()) == 1)
    assert (h.ran, h.writer.waiting) == ([keep], 1)
    now[0] = 10.0
    h.writer.flush_grace()
    h.drained()
    assert h.groups == [[keep], [delete]]
    assert [f.op for f in h.finished()] == [keep, delete]


def test_a_group_result_for_an_op_that_never_started_still_finishes_it(tmp_path):
    h = _grouped(tmp_path)

    def refuses_before_starting(ops, ctx, claim):
        return [Outcome(ok=False, message="registry locked", wrote=False) for _ in ops]

    h.execute_group = refuses_before_starting
    batch = h.writer.submit([KeepOp(_seen("a")), KeepOp(_seen("b"))], label="2 sessions")
    h.writer.start()
    h.drained()
    started = [e.ticket for _, e in h.events if isinstance(e, Started)]
    assert started == [ticket for ticket, _ in batch.tickets]
    assert [f.outcome.message for f in h.finished()] == ["registry locked", "registry locked"]
    assert h.writer.idle


def test_the_real_executor_groups_by_default(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    write = registry.atomic_write_text
    writes = []

    def counted(target, text, **options):
        writes.append(target)
        write(target, text, **options)

    monkeypatch.setattr(registry, "atomic_write_text", counted)
    real = Writer(
        h.registry_path,
        executor=decisions.as_executor(None),
        make_context=h.make_context,
        deliver=h.deliver,
        error_log=tmp_path / "errors.log",
    )
    seen = Seen(("claude-code", "on-disk"), "on-disk", "x", None)
    real.submit([KeepOp(seen), SnoozeOp(seen, "later")], label="2 decisions")
    real.start()
    h.drained()
    assert [f.outcome.message for f in h.finished()] == [
        "kept long-term: on-disk",
        "snoozed on-disk until later",
    ]
    assert len(writes) == 1


def test_an_injected_executor_object_is_used_as_is_and_a_callable_runs_one_by_one(tmp_path):
    h = Harness(tmp_path)
    assert decisions.as_executor(h) is h
    calls = []
    one_by_one = decisions.as_executor(lambda op, ctx: calls.append(op) or Outcome(True, "", True))
    ops = [KeepOp(_seen("a")), KeepOp(_seen("b"))]
    results = one_by_one.execute_group(ops, h.make_context(lambda: False, None), lambda i: i == 1)
    assert (calls, results) == ([ops[1]], [None, Outcome(True, "", True)])


def test_a_full_error_log_rotates_to_one_previous_file(tmp_path, monkeypatch):
    monkeypatch.setattr(writer_module, "ERROR_LOG_LIMIT", 10)
    log = tmp_path / "errors.log"
    log.write_text("older entries past the limit\n")
    (tmp_path / "errors.log.1").write_text("oldest\n")
    h = Harness(tmp_path)
    boom = ArchiveOp(_seen("a"))
    h.raises[boom] = ValueError("boom")
    h.writer.submit([boom], label="archive a")
    h.drained()
    assert (tmp_path / "errors.log.1").read_text() == "older entries past the limit\n"
    assert log.read_text().splitlines()[0].endswith("ArchiveOp (archive a)")
