from __future__ import annotations

from collections.abc import Callable

type CancelCheck = Callable[[], bool]


def never_cancelled() -> bool:
    return False
