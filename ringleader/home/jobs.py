from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from textual.message import Message

from .. import decisions, revalidate
from ..models import Model, with_cancel
from ..revalidate import PresenceProbe, Seen
from ..tui.background import CancelToken, Done, Failed, Job, start_daemon


@dataclass(frozen=True)
class HarvestReady:
    markdown: str | None


@dataclass(frozen=True)
class HarvestEmpty:
    pass


@dataclass(frozen=True)
class HarvestBlocked:
    reason: str


type HarvestResult = HarvestReady | HarvestEmpty | HarvestBlocked


class HarvestMessage(Message):
    def __init__(self, ticket: int, seen: Seen, result: Done[HarvestResult] | Failed) -> None:
        super().__init__()
        self.ticket = ticket
        self.seen = seen
        self.result = result


class JobMessage(Message):
    def __init__(self, deliver: Callable[[], None]) -> None:
        super().__init__()
        self.deliver = deliver


def harvest_job(
    thread: dict[str, Any], model: Model, presence: PresenceProbe
) -> Job[HarvestResult, None]:
    def job(token: CancelToken, _progress: Callable[[None], None]) -> HarvestResult:
        running = revalidate.running_reason(thread, presence(), "refuse")
        if running is not None:
            return HarvestBlocked(running)
        if thread.get("decision") == "harvested":
            return HarvestReady(None)
        markdown = decisions.harvest_markdown(thread, with_cancel(model, token.is_cancelled))
        return HarvestEmpty() if markdown is None else HarvestReady(markdown)

    return job


class JobBook:
    def __init__(self, post: Callable[[Message], None]) -> None:
        self._post = post
        self._harvests: dict[int, CancelToken] = {}
        self._labels: dict[int, str] = {}
        self._next_ticket = 0
        self._side_jobs = 0
        self._side_tokens: set[CancelToken] = set()

    @property
    def harvesting(self) -> tuple[str, ...]:
        return tuple(self._labels.values())

    @property
    def harvest_count(self) -> int:
        return len(self._harvests)

    @property
    def busy(self) -> bool:
        return bool(self._harvests) or self._side_jobs != 0

    def start_harvest(self, label: str, seen: Seen, job: Job[HarvestResult, None]) -> int:
        self._next_ticket -= 1
        ticket = self._next_ticket
        token = CancelToken()
        self._harvests[ticket] = token
        self._labels[ticket] = label
        start_daemon(
            job,
            on_progress=lambda _update: None,
            on_result=lambda result: self._post(HarvestMessage(ticket, seen, result)),
            name="rl-harvest",
            token=token,
        )
        return ticket

    def harvest_landed(self, ticket: int) -> CancelToken | None:
        self._labels.pop(ticket, None)
        return self._harvests.pop(ticket, None)

    def run_side[T](
        self,
        job: Job[T, None],
        *,
        name: str,
        on_result: Callable[[Done[T] | Failed], None],
    ) -> CancelToken:
        self._side_jobs += 1

        def landed(result: Done[T] | Failed) -> None:
            def deliver() -> None:
                self._side_tokens.discard(token)
                on_result(result)

            self._post(JobMessage(deliver))

        token = start_daemon(job, on_progress=lambda _update: None, on_result=landed, name=name)
        self._side_tokens.add(token)
        return token

    def side_landed(self) -> None:
        self._side_jobs -= 1

    def cancel_side(self) -> None:
        for token in self._side_tokens:
            token.cancel()

    def cancel_harvests(self) -> None:
        for token in self._harvests.values():
            token.cancel()
