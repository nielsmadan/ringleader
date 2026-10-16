import subprocess
import sys
import textwrap
import threading

from ringleader.tui.background import CancelToken, Done, Failed, start_daemon


class Recorder:
    def __init__(self):
        self.events = []
        self.done = threading.Event()
        self.progressed = threading.Event()

    def progress(self, value):
        self.events.append(("progress", value))
        self.progressed.set()

    def result(self, value):
        self.events.append(("result", value))
        self.done.set()


def _run(job, *, interval=0.1, token=None):
    recorder = Recorder()
    returned = start_daemon(
        job,
        on_progress=recorder.progress,
        on_result=recorder.result,
        name="test-job",
        token=token,
        progress_interval=interval,
    )
    return recorder, returned


def test_a_value_arrives_as_done():
    recorder, _ = _run(lambda token, report: 42)
    assert recorder.done.wait(5)
    assert recorder.events == [("result", Done(42))]


def test_a_raising_job_arrives_as_failed_with_its_traceback():
    def job(token, report):
        raise ValueError("boom")

    recorder, _ = _run(job)
    assert recorder.done.wait(5)
    [(kind, failed)] = recorder.events
    assert (kind, type(failed), str(failed.error)) == ("result", Failed, "boom")
    assert "ValueError: boom" in failed.trace


def test_a_base_exception_still_arrives_as_failed():
    def job(token, report):
        raise SystemExit(3)

    recorder, _ = _run(job)
    assert recorder.done.wait(5)
    [(_, failed)] = recorder.events
    assert isinstance(failed.error, SystemExit)


def test_progress_keeps_the_latest_within_the_interval_and_flushes_it_before_the_result():
    def job(token, report):
        for step in (1, 2, 3):
            report(step)
        return "done"

    recorder, _ = _run(job, interval=600)
    assert recorder.done.wait(5)
    assert recorder.events == [("progress", 1), ("progress", 3), ("result", Done("done"))]


def test_a_held_progress_update_is_delivered_once_the_interval_passes():
    delivered_two = threading.Event()

    def job(token, report):
        report(1)
        report(2)
        return delivered_two.wait(5)

    recorder = Recorder()

    def progress(value):
        recorder.progress(value)
        if value == 2:
            delivered_two.set()

    start_daemon(
        job,
        on_progress=progress,
        on_result=recorder.result,
        name="test-job",
        progress_interval=0.05,
    )
    assert recorder.done.wait(5)
    assert recorder.events == [("progress", 1), ("progress", 2), ("result", Done(True))]


def test_the_job_sees_the_token_it_was_given():
    token = CancelToken()
    token.cancel()
    recorder, returned = _run(lambda t, report: t.is_cancelled(), token=token)
    assert recorder.done.wait(5)
    assert (returned is token, recorder.events) == (True, [("result", Done(True))])


def test_a_blocked_daemon_job_does_not_delay_interpreter_exit():
    script = textwrap.dedent("""
        import threading
        from ringleader.tui.background import start_daemon

        started = threading.Event()
        gate = threading.Event()

        def job(token, report):
            started.set()
            gate.wait()

        start_daemon(job, on_progress=print, on_result=print, name="blocked")
        started.wait(5)
        print("returned")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "returned"
