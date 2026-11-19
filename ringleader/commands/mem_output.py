from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from typing import Any, NoReturn

from ..registry import RegistryConflict
from . import default_registry


def terminal_text(value: str) -> str:
    return "".join(
        char if char.isprintable() or char in "\n\t" else ascii(char)[1:-1] for char in value
    )


def wants_json(argv: list[str]) -> bool:
    return "--json" in (argv[: argv.index("--")] if "--" in argv else argv)


class MemoryParser(argparse.ArgumentParser):
    def __init__(self, verb: str, argv: list[str], description: str) -> None:
        super().__init__(
            prog=f"rl mem {verb}".rstrip(), description=description, allow_abbrev=False
        )
        self.as_json = wants_json(argv)
        self.stdout = sys.stdout
        self.add_argument("--registry", default=default_registry(), type=Path)
        self.add_argument("--json", action="store_true", help="emit one versioned JSON object")

    def emit(self, data: dict[str, Any], message: str = "") -> None:
        if self.as_json:
            print(json.dumps({"version": 1, **data}, ensure_ascii=False), file=self.stdout)
        elif message:
            print(terminal_text(message))

    def fail(self, message: str, *, code: int = 1, **data: Any) -> int:
        print(f"{self.prog}: {terminal_text(message)}", file=sys.stderr)
        if self.as_json:
            self.emit({**data, "error": {"message": message}})
        return code

    def error(self, message: str) -> NoReturn:
        if not self.as_json:
            super().error(message)
        raise SystemExit(self.fail(message, code=2))

    def run(self, action: Callable[[], int]) -> int:
        try:
            with redirect_stdout(sys.stderr) if self.as_json else nullcontext():
                return action()
        except BrokenPipeError:
            raise
        except (OSError, ValueError, KeyError, RegistryConflict) as exc:
            return self.fail(str(exc))
        except KeyboardInterrupt:
            return self.fail("Cancelled", code=130)
