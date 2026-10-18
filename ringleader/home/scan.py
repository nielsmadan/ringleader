from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import harvests, registry, revalidate
from ..harvests import HarvestSnapshot
from ..models import Model, ModelTask, describe
from ..refresh import Refreshed
from ..revalidate import Identity
from ..tui.background import CancelToken, Done, Failed, start_daemon
from ..tui.types import Clock, Presence, Refresher
from .writer import Snapshot


@dataclass(frozen=True)
class ScanLanded:
    snapshot: Snapshot
    note: str | None
    live_ids: frozenset[Identity] | None
    harvests: HarvestSnapshot | None
    enrich_model: str | None
    harvest_model: str | None
    warnings: tuple[str, ...]
    unavailable_sources: frozenset[str] = frozenset()


type FullRefresher = Callable[[str, str], Refreshed]


def _ignore(_progress: object) -> None:
    return None


class ScanService:
    def __init__(  # noqa: PLR0913 -- injection points the suite drives
        self,
        registry_path: str,
        *,
        refresher: Refresher,
        full_refresher: FullRefresher | None = None,
        presence: Presence,
        models: Callable[[ModelTask], Model | None],
        clock: Clock,
        generation: Callable[[], int],
        deliver: Callable[[ScanLanded], None],
        fail: Callable[[str], None] = _ignore,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry_path = registry_path
        self._refresher = refresher
        self._full_refresher = full_refresher
        self._presence = presence
        self._models = models
        self._clock = clock
        self._generation = generation
        self._deliver = deliver
        self._fail = fail
        self._monotonic = monotonic
        self._cond = threading.Condition()
        self._running = False
        self._rerun = False
        self._latest: ScanLanded | None = None
        self._harvests: HarvestSnapshot | None = None

    def request(self) -> bool:
        with self._cond:
            if self._running:
                self._rerun = True
                return False
            self._running = True
        self._start()
        return True

    @property
    def running(self) -> bool:
        with self._cond:
            return self._running

    def latest(self, max_age: float | None = None) -> ScanLanded | None:
        with self._cond:
            landed = self._latest
        if landed is None or max_age is None:
            return landed
        return landed if self._monotonic() - landed.snapshot.taken_at <= max_age else None

    def wait(self, timeout: float | None = None) -> ScanLanded | None:
        with self._cond:
            self._cond.wait_for(lambda: not self._running, timeout)
            return self._latest

    def _start(self) -> None:
        start_daemon(self._scan, on_progress=_ignore, on_result=self._landed, name="rl-scan")

    def _scan(self, _token: CancelToken, _progress: Callable[[object], None]) -> ScanLanded:
        generation = self._generation()
        warnings: list[str] = []
        reg, note, scanned, unavailable = self._refreshed(warnings)
        live_ids = self._live_ids(warnings)
        harvest_snapshot = _guarded(
            warnings, "harvests", lambda: harvests.snapshot_harvests(self._harvests)
        )
        self._harvests = harvest_snapshot
        return ScanLanded(
            snapshot=Snapshot(reg, generation, scanned=scanned, taken_at=self._monotonic()),
            note=note,
            live_ids=live_ids,
            harvests=harvest_snapshot,
            enrich_model=self._model_label(warnings, "enrich"),
            harvest_model=self._model_label(warnings, "harvest"),
            warnings=tuple(warnings),
            unavailable_sources=unavailable,
        )

    def _refreshed(
        self, warnings: list[str]
    ) -> tuple[dict[str, Any], str | None, bool, frozenset[str]]:
        now_iso = self._clock().isoformat()
        try:
            if self._full_refresher is not None:
                refreshed = self._full_refresher(self._registry_path, now_iso)
                return refreshed.reg, refreshed.note, True, refreshed.unavailable_sources
            reg, note = self._refresher(self._registry_path, now_iso)
        except Exception as error:  # noqa: BLE001 -- a failed scan still shows the disk
            warnings.append(f"scan failed: {error}")
            return registry.load_registry(self._registry_path), None, False, frozenset()
        return reg, note, True, frozenset()

    def _live_ids(self, warnings: list[str]) -> frozenset[Identity] | None:
        try:
            return revalidate.probe(self._presence)()
        except Exception as error:  # noqa: BLE001 -- unknown presence is a state, not a crash
            warnings.append(f"presence check failed: {error}")
            return None

    def _model_label(self, warnings: list[str], task: ModelTask) -> str | None:
        def label() -> str | None:
            model = self._models(task)
            return describe(model) if model is not None else None

        return _guarded(warnings, f"{task} model", label)

    def _landed(self, result: Done[ScanLanded] | Failed) -> None:
        with self._cond:
            if isinstance(result, Done):
                self._latest = result.value
            rerun, self._rerun = self._rerun, False
            self._running = rerun
            self._cond.notify_all()
        if isinstance(result, Done):
            self._deliver(result.value)
        else:
            self._fail(str(result.error))
        if rerun:
            self._start()


def _guarded[T](warnings: list[str], what: str, read: Callable[[], T]) -> T | None:
    try:
        return read()
    except Exception as error:  # noqa: BLE001 -- one failing part must not sink the scan
        warnings.append(f"{what} unavailable: {error}")
        return None
