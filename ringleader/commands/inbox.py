from __future__ import annotations

import argparse

from . import default_registry


def cmd_inbox(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="rl inbox",
        description="Blitz through dormant sessions in a terminal UI: "
        "j/k to move, h/d/s/enter/;/u to act.",
    )
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    try:
        import textual  # noqa: F401, PLC0415 -- core dep; imported lazily to keep other commands fast
    except ImportError:
        print(
            "rl inbox needs Textual — reinstall ringleader (`uv tool install --force ringleader`)"
        )
        return 2

    from ..inbox.app import InboxApp  # noqa: PLC0415 -- only importable once textual is present

    InboxApp(registry_path=args.registry).run()
    return 0
