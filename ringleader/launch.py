from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence

type Runner = Callable[[Sequence[str], str], int]


def exec_agent(argv: Sequence[str], cwd: str) -> int:
    os.chdir(cwd)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvpe(argv[0], list(argv), os.environ)
