import os
import stat
import time
from pathlib import Path

from ringleader.tui.runner import run_rl


def _script(tmp_path, body):
    path = tmp_path / "fake-python"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid, timeout=3.0):
    deadline = time.monotonic() + timeout
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not _alive(pid)


def test_help_runs_the_real_cli():
    run = run_rl(["--help"], cancelled=lambda: False, columns=80)
    assert run.returncode == 0
    assert "usage: rl" in run.output
    assert run.argv == ("--help",)


def test_a_ringleader_package_in_the_cwd_does_not_shadow_the_real_cli(tmp_path, monkeypatch):
    planted = tmp_path / "ringleader"
    planted.mkdir()
    (planted / "__init__.py").write_text("")
    (planted / "__main__.py").write_text("print('planted code ran')\n")
    monkeypatch.chdir(tmp_path)
    run = run_rl(["--help"], cancelled=lambda: False, columns=80)
    assert run.returncode == 0
    assert "usage: rl" in run.output


def test_columns_and_plain_terminal_reach_the_child(tmp_path):
    python = _script(tmp_path, 'echo "$COLUMNS $TERM $NO_COLOR"\n')
    run = run_rl(["x"], cancelled=lambda: False, columns=123, python=python)
    assert run.output.strip() == "123 dumb 1"


def test_an_explicit_env_replaces_the_inherited_one(tmp_path):
    python = _script(tmp_path, 'echo "$MARKER"\n')
    run = run_rl(
        ["x"], cancelled=lambda: False, columns=80, python=python, env={"MARKER": "isolated"}
    )
    assert run.output.strip() == "isolated"


def test_nonzero_exit_and_stderr_are_captured(tmp_path):
    python = _script(tmp_path, "echo oops >&2\nexit 3\n")
    run = run_rl(["x"], cancelled=lambda: False, columns=80, python=python)
    assert (run.returncode, run.output.strip()) == (3, "oops")


def test_cancelling_kills_the_child_and_its_grandchild(tmp_path):
    pids = tmp_path / "pids"
    python = _script(tmp_path, f"sleep 60 &\necho $! > {pids}\necho $$ >> {pids}\nwait\n")
    started = time.monotonic()

    def cancelled():
        return pids.exists() and len(pids.read_text().split()) == 2

    run = run_rl(["x"], cancelled=cancelled, columns=80, python=python)
    grandchild, child = (int(p) for p in Path(pids).read_text().split())
    assert run.returncode is None
    assert time.monotonic() - started < 3.0
    assert _wait_gone(child)
    assert _wait_gone(grandchild)


def test_output_beyond_the_cap_is_truncated_with_a_marker(tmp_path, monkeypatch):
    monkeypatch.setattr("ringleader.tui.runner.MAX_OUTPUT_CHARS", 50)
    python = _script(tmp_path, "yes xxxxxxxxx | head -n 1000\n")
    run = run_rl(["x"], cancelled=lambda: False, columns=80, python=python)
    assert run.output.endswith("\n[output truncated]")
    assert run.output.count("x") <= 50


def test_a_child_past_the_deadline_is_stopped_and_marked(tmp_path):
    python = _script(tmp_path, "echo started\nexec sleep 60\n")
    run = run_rl(["x"], cancelled=lambda: False, columns=80, python=python, timeout=0.3)
    assert run.returncode is None
    assert run.output.startswith("started")
    assert run.output.endswith("[timed out after 0s]")


def test_non_utf8_bytes_are_replaced_not_fatal(tmp_path):
    python = _script(tmp_path, "printf 'a\\377b\\n'\n")
    run = run_rl(["x"], cancelled=lambda: False, columns=80, python=python)
    assert run.output.strip() == "a\ufffdb"
