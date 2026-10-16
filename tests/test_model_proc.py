import contextlib
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest
from ringleader.models import proc
from ringleader.models.base import ModelError
from ringleader.models.proc import kill_all_children, neutral_cwd, run_model_process

PY = sys.executable


def never() -> bool:
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, seconds: float = 15.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_round_trips_stdin_to_stdout():
    result = run_model_process(
        [PY, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        stdin_text="hello",
        timeout=10,
        cancelled=never,
    )
    assert (result.returncode, result.stdout) == (0, "HELLO")


def test_reports_nonzero_exit_and_stderr():
    result = run_model_process(
        [PY, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"],
        stdin_text=None,
        timeout=10,
        cancelled=never,
    )
    assert (result.returncode, result.stderr) == (3, "bad")


def test_runs_in_the_neutral_cwd_by_default():
    result = run_model_process(
        [PY, "-c", "import os; print(os.getcwd())"],
        stdin_text=None,
        timeout=10,
        cancelled=never,
    )
    assert Path(result.stdout.strip()).resolve() == neutral_cwd().resolve()


def test_spawn_failure_is_a_model_error():
    with pytest.raises(ModelError, match="invocation failed"):
        run_model_process(["/nonexistent/binary"], stdin_text=None, timeout=1, cancelled=never)


def test_cancel_kills_a_sleeping_child_promptly():
    started = time.monotonic()
    flag = threading.Event()
    threading.Timer(0.3, flag.set).start()
    with pytest.raises(ModelError, match=r"^cancelled$"):
        run_model_process(
            [PY, "-c", "import time; time.sleep(60)"],
            stdin_text=None,
            timeout=None,
            cancelled=flag.is_set,
        )
    assert time.monotonic() - started < 30


def test_timeout_raises_the_timeout_message():
    with pytest.raises(ModelError, match="timed out after 1s"):
        run_model_process(
            [PY, "-c", "import time; time.sleep(60)"],
            stdin_text=None,
            timeout=1,
            cancelled=never,
        )


def test_cancel_kills_the_whole_process_group(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid));"
        "time.sleep(60)"
    )
    flag = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            run_model_process(
                [PY, "-c", code], stdin_text=None, timeout=None, cancelled=flag.is_set
            )
        except ModelError as e:
            errors.append(e)

    thread = threading.Thread(target=run)
    thread.start()
    _wait_for_pid(pid_file)
    grandchild = int(pid_file.read_text())
    assert _alive(grandchild)
    flag.set()
    thread.join(30)
    assert [str(e) for e in errors] == ["cancelled"]
    assert _wait_gone(grandchild)


def test_kill_all_children_ends_a_blocked_call():
    errors: list[BaseException] = []
    results: list[int] = []

    def run() -> None:
        try:
            r = run_model_process(
                [PY, "-c", "import time; time.sleep(60)"],
                stdin_text=None,
                timeout=None,
                cancelled=never,
            )
            results.append(r.returncode)
        except ModelError as e:
            errors.append(e)

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 30
    while not proc._live_groups and time.monotonic() < deadline:
        time.sleep(0.05)
    assert kill_all_children() == 1
    thread.join(30)
    assert not thread.is_alive()
    assert (results, errors) == ([-15], [])


def _wait_for_pid(path: Path, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return
        time.sleep(0.05)


def test_exception_from_cancelled_kills_the_child(tmp_path):
    pid_file = tmp_path / "child.pid"
    code = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"

    def interrupt() -> bool:
        _wait_for_pid(pid_file)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_model_process([PY, "-c", code], stdin_text=None, timeout=None, cancelled=interrupt)
    child = int(pid_file.read_text())
    assert _wait_gone(child)
    assert proc._live_groups == set()


def test_timeout_escalates_to_sigkill_for_a_child_that_ignores_sigterm(tmp_path):
    pid_file = tmp_path / "child.pid"
    code = (
        "import os, signal, time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()));"
        "time.sleep(60)"
    )
    with pytest.raises(ModelError, match="timed out after 5s"):
        run_model_process([PY, "-c", code], stdin_text=None, timeout=5, cancelled=never)
    assert _wait_gone(int(pid_file.read_text()))


def test_timeout_returns_even_when_an_escaped_descendant_holds_the_pipes(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " start_new_session=True);"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid));"
        "time.sleep(60)"
    )
    started = time.monotonic()
    try:
        with pytest.raises(ModelError, match="timed out after 1s"):
            run_model_process([PY, "-c", code], stdin_text=None, timeout=1, cancelled=never)
        assert time.monotonic() - started < 30
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
