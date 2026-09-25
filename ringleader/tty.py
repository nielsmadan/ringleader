from __future__ import annotations

from collections.abc import Mapping
from typing import TextIO


def want_color(mode: str, stream: TextIO, env: Mapping[str, str]) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return bool(getattr(stream, "isatty", lambda: False)()) and not env.get("NO_COLOR")
