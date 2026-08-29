from __future__ import annotations

import argparse

from ..session_env import current_session


def cmd_whoami(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="rl session whoami").parse_args(argv)
    found = current_session()
    if found is None:
        print("not inside a recognised agent session")
        return 1
    source, session_id = found
    print(f"{source} {session_id}")
    return 0
