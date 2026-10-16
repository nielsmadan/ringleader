from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

type Clock = Callable[[], datetime]
type Refresher = Callable[[str, str], tuple[dict[str, Any], str | None]]
# raises presence.PresenceUnavailable when it cannot tell
type Presence = Callable[[], frozenset[tuple[str, str]]]
