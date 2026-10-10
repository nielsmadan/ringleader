from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ringleader.portability.verify import codex


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.evidence:
        args.evidence.mkdir(parents=True, exist_ok=False)
        print(codex.run(args.codex, args.evidence.resolve()).summary)
    else:
        with TemporaryDirectory(prefix="ringleader-codex-smoke-") as temporary:
            print(codex.run(args.codex, Path(temporary).resolve()).summary)


if __name__ == "__main__":
    main()
