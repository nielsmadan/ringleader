from __future__ import annotations

from ..decisions import Decision

DELETE_GRACE_SECONDS: float = 10.0
CONFIRM_EACH_DELETE: bool = False
CONFIRM_BULK: frozenset[Decision] = frozenset({"archive", "delete"})
SCAN_FRESH_SECONDS: float = 120.0
