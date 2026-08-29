import os
import stat

from ringleader.commands import hooks


def test_the_shell_hook_ships_and_is_executable():
    p = hooks.bundled_dir() / "session_end.sh"
    assert p.exists()
    assert os.stat(p).st_mode & stat.S_IXUSR


def test_the_pi_extension_ships():
    assert (hooks.bundled_dir() / "pi-ringleader-finish.ts").exists()
