import threading
from datetime import UTC, datetime

from ringleader import registry
from ringleader.home.scan import ScanService
from ringleader.presence import PresenceUnavailable
from ringleader.refresh import Refreshed

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


class GatedRefresher:
    def __init__(self):
        self.gate = threading.Event()
        self.calls = 0
        self.active = 0
        self.most_active = 0
        self.lock = threading.Lock()
        self.on_call = lambda: None

    def __call__(self, path, now_iso):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.most_active = max(self.most_active, self.active)
        self.on_call()
        try:
            self.gate.wait(5)
            return {**registry.empty_registry(), "scan": self.calls}, f"note {self.calls}"
        finally:
            with self.lock:
                self.active -= 1


def _service(refresher, *, presence=frozenset, generation=lambda: 0, delivered=None):
    landed = delivered if delivered is not None else []
    return ScanService(
        "/nowhere/registry.json",
        refresher=refresher,
        presence=presence,
        models=lambda task: None,
        clock=lambda: NOW,
        generation=generation,
        deliver=landed.append,
    )


def test_a_request_during_a_scan_queues_exactly_one_rerun():
    refresher = GatedRefresher()
    service = _service(refresher)
    try:
        assert service.request() is True
        assert service.request() is False
        assert service.request() is False
        assert service.running is True
    finally:
        refresher.gate.set()
    landed = service.wait(5)
    assert (refresher.calls, refresher.most_active, service.running) == (2, 1, False)
    assert landed is not None
    assert landed.note == "note 2"


def test_wait_returns_the_scan_that_landed():
    refresher = GatedRefresher()
    delivered = []
    service = _service(refresher, delivered=delivered)
    refresher.gate.set()
    service.request()
    landed = service.wait(5)
    assert landed is not None
    assert delivered == [landed]
    assert service.latest() is landed
    assert (landed.snapshot.reg["scan"], landed.snapshot.scanned, landed.warnings) == (1, True, ())
    assert landed.live_ids == frozenset()


def test_latest_honours_max_age():
    refresher = GatedRefresher()
    refresher.gate.set()
    service = _service(refresher)
    service.request()
    landed = service.wait(5)
    assert service.latest(max_age=60) is landed
    assert service.latest(max_age=-1) is None


def test_latest_ages_out_by_the_injected_monotonic_clock():
    refresher = GatedRefresher()
    refresher.gate.set()
    now = [100.0]
    service = ScanService(
        "/nowhere/registry.json",
        refresher=refresher,
        presence=frozenset,
        models=lambda task: None,
        clock=lambda: NOW,
        generation=lambda: 0,
        deliver=lambda landed: None,
        monotonic=lambda: now[0],
    )
    service.request()
    landed = service.wait(5)
    assert landed is not None
    assert landed.snapshot.taken_at == 100.0
    now[0] = 150.0
    assert service.latest(max_age=60) is landed
    now[0] = 161.0
    assert service.latest(max_age=60) is None


def test_unavailable_presence_lands_as_unknown():
    def presence():
        raise PresenceUnavailable("no hooklinesinker")

    refresher = GatedRefresher()
    refresher.gate.set()
    service = _service(refresher, presence=presence)
    service.request()
    landed = service.wait(5)
    assert landed is not None
    assert landed.live_ids is None


def test_a_crashing_presence_lands_as_unknown_with_a_warning():
    def presence():
        raise RuntimeError("boom")

    refresher = GatedRefresher()
    refresher.gate.set()
    service = _service(refresher, presence=presence)
    service.request()
    landed = service.wait(5)
    assert landed is not None
    assert (landed.live_ids, landed.warnings) == (None, ("presence check failed: boom",))


def test_the_generation_is_the_one_read_before_the_refresher_ran():
    generation = {"value": 4}
    refresher = GatedRefresher()
    refresher.gate.set()
    refresher.on_call = lambda: generation.update(value=5)
    service = _service(refresher, generation=lambda: generation["value"])
    service.request()
    landed = service.wait(5)
    assert landed is not None
    assert landed.snapshot.generation == 4


def test_a_failed_refresh_falls_back_to_the_registry_on_disk(tmp_path):
    path = tmp_path / "registry.json"
    registry.save_registry(path, {**registry.empty_registry(), "threads": []})

    def refresher(path, now_iso):
        raise OSError("disk on fire")

    service = ScanService(
        str(path),
        refresher=refresher,
        presence=frozenset,
        models=lambda task: None,
        clock=lambda: NOW,
        generation=lambda: 0,
        deliver=lambda landed: None,
    )
    service.request()
    landed = service.wait(5)
    assert landed is not None
    assert (landed.snapshot.scanned, landed.warnings) == (False, ("scan failed: disk on fire",))


def test_a_scan_that_raises_outright_is_reported_as_failed(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{not json")
    failures = []

    def refresher(path, now_iso):
        raise OSError("disk on fire")

    service = ScanService(
        str(path),
        refresher=refresher,
        presence=frozenset,
        models=lambda task: None,
        clock=lambda: NOW,
        generation=lambda: 0,
        deliver=lambda landed: None,
        fail=failures.append,
    )
    service.request()
    assert service.wait(5) is None
    assert len(failures) == 1


def test_the_full_refresher_reports_which_sources_were_unavailable():
    def refresher(path, now_iso):
        raise AssertionError("the plain refresher must not run")

    def full(path, now_iso):
        return Refreshed({**registry.empty_registry(), "scan": 1}, "note", frozenset({"droid"}))

    service = ScanService(
        "/nowhere/registry.json",
        refresher=refresher,
        full_refresher=full,
        presence=frozenset,
        models=lambda task: None,
        clock=lambda: NOW,
        generation=lambda: 0,
        deliver=lambda landed: None,
    )
    service.request()
    landed = service.wait(5)
    assert landed is not None
    assert (landed.note, landed.unavailable_sources, landed.snapshot.scanned) == (
        "note",
        frozenset({"droid"}),
        True,
    )
