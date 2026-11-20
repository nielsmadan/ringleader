from __future__ import annotations

import errno
import io
import os
import shutil
import subprocess

import pytest
from ringleader import cli, shell
from ringleader.commands import command_paths
from ringleader.commands.shell import cmd_init


def _stub_rl(tmp_path, body):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "rl"
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    return bin_dir


def _run_wrapped(tmp_path, sh, bin_dir, start, invocation='rl resume "a b"'):
    if shutil.which(sh) is None:
        pytest.skip(f"{sh} is not installed")
    script = shell.init_script(sh) + f'{invocation}; rc=$?; printf "%s\\n%s\\n" "$rc" "$PWD"'
    result = subprocess.run(
        [sh, "-c", script],
        cwd=start,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "TMPDIR": str(tmp_path), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    *printed, status, pwd = result.stdout.strip().splitlines()
    return int(status), pwd, printed


@pytest.mark.parametrize("sh", shell.SHELLS)
def test_the_wrapper_follows_the_directory_rl_leaves_and_keeps_its_status(tmp_path, sh):
    target = tmp_path / "session dir"
    target.mkdir()
    bin_dir = _stub_rl(tmp_path, f"printf '%s' '{target}' > \"$RINGLEADER_CWD_FILE\"\nexit 3")

    assert _run_wrapped(tmp_path, sh, bin_dir, tmp_path)[:2] == (3, str(target))
    assert list(tmp_path.glob("rl-cwd.*")) == []


@pytest.mark.parametrize("sh", shell.SHELLS)
@pytest.mark.parametrize("invocation", ["rl", "rl inbox", "rl resume x"])
def test_every_command_that_can_launch_an_agent_gets_a_cwd_file(tmp_path, sh, invocation):
    bin_dir = _stub_rl(tmp_path, '[ -f "$RINGLEADER_CWD_FILE" ] && echo file || echo none')

    assert _run_wrapped(tmp_path, sh, bin_dir, tmp_path, invocation)[2] == ["file"]


@pytest.mark.parametrize("sh", shell.SHELLS)
def test_the_wrapper_stays_put_when_rl_leaves_no_directory(tmp_path, sh):
    start = tmp_path / "start"
    start.mkdir()
    bin_dir = _stub_rl(tmp_path, '[ "$1" = resume ] && [ "$2" = "a b" ] || exit 9')

    assert _run_wrapped(tmp_path, sh, bin_dir, start)[:2] == (0, str(start))


@pytest.mark.parametrize("sh", shell.SHELLS)
def test_a_command_that_never_launches_an_agent_runs_without_a_cwd_file(tmp_path, sh):
    bin_dir = _stub_rl(tmp_path, 'echo "file=${RINGLEADER_CWD_FILE-none} args=$*"')

    status, pwd, printed = _run_wrapped(tmp_path, sh, bin_dir, tmp_path, "rl overview --json")

    assert (status, pwd, printed) == (0, str(tmp_path), ["file=none args=overview --json"])


def test_taking_the_cwd_file_removes_it_from_the_environment(tmp_path):
    made = tmp_path / "rl-cwd.x"
    made.write_text("")
    environ = {shell.CWD_FILE_VAR: str(made), "PATH": "/bin"}

    assert shell.take_cwd_file(environ) == made
    assert environ == {"PATH": "/bin"}


def test_a_stray_variable_naming_a_used_file_does_not_count_as_the_shell_function(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_text("keep me")
    environ = {shell.CWD_FILE_VAR: str(notes)}

    assert shell.take_cwd_file(environ) is None
    assert environ == {}


def test_recording_writes_the_directory_into_the_wrappers_empty_file(tmp_path):
    made = tmp_path / "rl-cwd.x"
    made.write_text("")

    shell.record_launch_dir(made, "/w/api")

    assert made.read_text() == "/w/api"


def test_recording_refuses_a_symlink_and_leaves_its_target_alone(tmp_path):
    victim = tmp_path / "empty-but-precious"
    victim.write_text("")
    link = tmp_path / "rl-cwd.x"
    link.symlink_to(victim)

    with pytest.raises(OSError) as refused:
        shell.record_launch_dir(link, "/w/api")
    assert refused.value.errno == errno.ELOOP
    assert victim.read_text() == ""


def test_recording_refuses_a_fifo_without_waiting_for_a_reader(tmp_path):
    fifo = tmp_path / "rl-cwd.x"
    os.mkfifo(fifo)

    with pytest.raises(OSError) as refused:
        shell.record_launch_dir(fifo, "/w/api")
    assert refused.value.errno == errno.ENXIO


def test_recording_refuses_a_second_name_for_another_file(tmp_path):
    original = tmp_path / "original"
    original.write_text("")
    os.link(original, tmp_path / "rl-cwd.x")

    with pytest.raises(OSError, match="not an empty file"):
        shell.record_launch_dir(tmp_path / "rl-cwd.x", "/w/api")
    assert original.read_text() == ""


def test_a_directory_name_that_is_not_utf8_is_written_as_its_bytes(tmp_path):
    made = tmp_path / "rl-cwd.x"
    made.write_text("")

    shell.record_launch_dir(made, os.fsdecode(b"/w/caf\xe9"))

    assert made.read_bytes() == b"/w/caf\xe9"


def test_recording_refuses_a_file_that_already_holds_something(tmp_path):
    stale = tmp_path / "notes.txt"
    stale.write_text("keep me")

    with pytest.raises(OSError, match="not an empty file"):
        shell.record_launch_dir(stale, "/w/api")
    assert stale.read_text() == "keep me"


def test_shell_init_prints_the_wrapper(capsys):
    assert cmd_init(["zsh"]) == 0
    assert capsys.readouterr().out == shell.init_script("zsh")


def test_shell_init_is_a_registered_command():
    assert "shell init" in command_paths()


def test_rl_usage_lists_the_shell_group():
    stream = io.StringIO()
    cli._usage(stream)
    assert "  shell: init" in stream.getvalue().splitlines()


def test_shell_init_refuses_a_shell_it_has_no_wrapper_for(capsys):
    with pytest.raises(SystemExit) as exited:
        cmd_init(["fish"])
    assert exited.value.code == 2
    error = capsys.readouterr().err
    assert "invalid choice" in error
    assert "fish" in error
    assert "zsh" in error
    assert "bash" in error
