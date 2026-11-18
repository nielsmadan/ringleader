from __future__ import annotations

import argparse
import sys

from ..shell import SHELLS, init_script


def cmd_init(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl shell init",
        description="Print an `rl` shell function that lets `rl resume` leave your shell in "
        "the session's directory once the agent exits. Add "
        '`eval "$(rl shell init zsh)"` to ~/.zshrc (or the bash equivalent to ~/.bashrc).',
    )
    ap.add_argument("shell", choices=SHELLS)
    args = ap.parse_args(argv)
    sys.stdout.write(init_script(args.shell))
    return 0
