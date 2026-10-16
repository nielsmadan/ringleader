from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import replace

from textual.message import Message

from .. import enrichment, roundup
from ..decisions import ClassifyOp, Op, RoundupPhasesOp, StampRoundupOp
from ..enrichment import Classified, EnrichOutcome
from ..models import Model
from ..revalidate import Identity
from ..roundup import EnrichScope, RoundupPlan, RoundupProgress
from ..tui.background import CancelToken, Done, Failed, Job, start_daemon
from .base import RoundupRun
from .writer import Batch, Finished

type Submit = Callable[[Sequence[Op], str], Batch | None]

ROUNDUP_ACTIVE = frozenset({"phases", "enrich", "stamp"})


class EnrichProgressMessage(Message):
    def __init__(self, progress: RoundupProgress) -> None:
        super().__init__()
        self.progress = progress


class EnrichDoneMessage(Message):
    def __init__(self, result: Done[EnrichOutcome] | Failed) -> None:
        super().__init__()
        self.result = result


def enrich_job(
    plan: RoundupPlan,
    ids: tuple[Identity, ...],
    model: Model,
    apply: Callable[[tuple[Classified, ...]], None],
) -> Job[EnrichOutcome, RoundupProgress]:
    def job(token: CancelToken, report: Callable[[RoundupProgress], None]) -> EnrichOutcome:
        return enrichment.enrich_sessions(
            plan.registry_path,
            ids,
            model=model,
            concurrency=plan.concurrency,
            progress=lambda done, total: report(RoundupProgress("enrich", done, total)),
            cancelled=token.is_cancelled,
            apply=apply,
        )

    return job


class RoundupDriver:
    def __init__(  # noqa: PLR0913 -- the app's seams, wired once
        self,
        *,
        read_only: bool,
        submit: Submit,
        classify: Callable[[tuple[Classified, ...]], None],
        cancel_op: Callable[[int], bool],
        enrich_model: Callable[[], Model | None],
        leaving: Callable[[], bool],
        post: Callable[[Message], None],
        toast: Callable[[str, bool], None],
        ended: Callable[[], None],
    ) -> None:
        self._read_only = read_only
        self._submit = submit
        self._classify = classify
        self._cancel_op = cancel_op
        self._enrich_model = enrich_model
        self._leaving = leaving
        self._post = post
        self._toast = toast
        self._ended = ended
        self._run: RoundupRun | None = None
        self._ticket: int | None = None
        self._error: str | None = None
        self._stopping = False
        self._stale = 0
        self._lock: ExitStack | None = None
        self._enrich_job: CancelToken | None = None

    @property
    def run(self) -> RoundupRun | None:
        return self._run

    @property
    def active(self) -> bool:
        return self._run is not None and self._run.stage in ROUNDUP_ACTIVE

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def stopping(self) -> bool:
        return self._stopping

    @property
    def enriching(self) -> bool:
        return self._enrich_job is not None

    def start(self, plan: RoundupPlan, scope: EnrichScope) -> bool:
        if self._read_only:
            self._toast("read-only: roundup is disabled", True)
            return False
        if self.active:
            self._toast("a roundup is already running", True)
            return False
        batch = self._submit([RoundupPhasesOp(plan)], "roundup")
        if batch is None:
            return False
        self._run = RoundupRun(plan, scope, "phases", None, None, None)
        self._ticket = batch.tickets[0][0]
        self._error = None
        self._stopping = False
        self._stale = 0
        return True

    def cancel(self) -> bool:
        run = self._run
        if run is None or run.stage not in ("phases", "enrich"):
            return False
        self._stopping = True
        if run.stage == "phases":
            if self._ticket is not None:
                self._cancel_op(self._ticket)
        elif self._enrich_job is not None:
            self._enrich_job.cancel()
        return True

    def cancel_enrich(self) -> None:
        if self._enrich_job is not None:
            self._enrich_job.cancel()

    def op_progress(self, ticket: int, progress: RoundupProgress) -> bool:
        if ticket != self._ticket:
            return False
        self._progressed(progress)
        return True

    def enrich_progress(self, progress: RoundupProgress) -> bool:
        if self._run is None or self._run.stage != "enrich":
            return False
        self._progressed(progress)
        return True

    def _progressed(self, progress: RoundupProgress) -> None:
        if self._run is not None:
            self._run = replace(self._run, progress=progress)

    def finished(self, event: Finished) -> None:
        if isinstance(event.op, ClassifyOp):
            self._stale += event.outcome.stale
        if event.ticket == self._ticket:
            self._step_finished(event)

    def dropped(self, tickets: Sequence[int]) -> None:
        run = self._run
        if run is not None and self._ticket in tickets:
            self._end(replace(run, stage="cancelled"), None)

    def _step_finished(self, event: Finished) -> None:
        run = self._run
        if run is None:
            return
        outcome = event.outcome
        if run.stage == "stamp":
            failed = not outcome.ok or event.internal is not None
            enrich = run.enrich
            if enrich is not None:
                run = replace(run, enrich=replace(enrich, stale=self._stale))
            self._end(
                replace(run, stage="failed" if failed else "done"),
                outcome.message if failed else None,
            )
            return
        phases = outcome.phases
        if phases is None or event.internal is not None:
            self._end(replace(run, stage="failed"), outcome.message)
            return
        run = replace(run, phases=phases)
        gone = phases.cancelled or self._stopping or self._leaving()
        if roundup.next_step("phases", cancelled=gone) == "cancelled":
            self._end(replace(run, stage="cancelled"), None)
            return
        if not self._hold_lock():
            self._end(replace(run, stage="failed"), roundup.ROUNDUP_BUSY)
            return
        model = self._enrich_model()
        ids = run.plan.enrich_ids(run.scope)
        step = roundup.next_step(
            "phases", cancelled=False, can_enrich=model is not None and bool(ids)
        )
        if step == "stamp" or model is None:
            self._stamp(run)
            return
        self._run = replace(run, stage="enrich", progress=RoundupProgress("enrich", 0, len(ids)))
        self._enrich_job = start_daemon(
            enrich_job(run.plan, ids, model, self._classify),
            on_progress=lambda progress: self._post(EnrichProgressMessage(progress)),
            on_result=lambda result: self._post(EnrichDoneMessage(result)),
            name="rl-enrich",
        )

    def enrich_done(self, result: Done[EnrichOutcome] | Failed) -> None:
        token, self._enrich_job = self._enrich_job, None
        run = self._run
        if run is None or run.stage != "enrich":
            return
        match result:
            case Done(value=outcome):
                run = replace(run, enrich=outcome)
                stopped = token is not None and token.is_cancelled()
                step = roundup.next_step(
                    "enrich", cancelled=outcome.cancelled or stopped or self._leaving()
                )
                if step == "cancelled":
                    self._end(replace(run, stage="cancelled"), None)
                else:
                    self._stamp(run)
            case Failed(error=error):
                self._end(replace(run, stage="failed"), f"enrich failed: {error}")

    def _stamp(self, run: RoundupRun) -> None:
        batch = self._submit([StampRoundupOp(run.plan.now_iso)], "roundup stamp")
        if batch is None:
            self._end(replace(run, stage="cancelled"), None)
            return
        self._run = replace(run, stage="stamp", progress=RoundupProgress("stamp", 0, 1))
        self._ticket = batch.tickets[0][0]

    def _end(self, run: RoundupRun, error: str | None) -> None:
        self.release_lock()
        self._run = run
        self._ticket = None
        self._error = error
        self._stopping = False
        match run.stage:
            case "done":
                self._toast("roundup finished", False)
            case "cancelled":
                self._toast("roundup cancelled — not stamped; run it again to finish", False)
            case _:
                self._toast(f"roundup failed: {error}", True)
        self._ended()

    def _hold_lock(self) -> bool:
        if self._lock is None:
            self._lock = roundup.hold_lock()
        return self._lock is not None

    def release_lock(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.close()
