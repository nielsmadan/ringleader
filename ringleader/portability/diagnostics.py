from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .service import ConversionService


def compatibility_rows(worker: ConversionService, cwd: Path) -> list[dict[str, Any]]:
    rows = []
    for agent in worker.adapters.agents():
        try:
            roots = worker.adapters.roots(agent, cwd)
            result = worker.adapters.writer(agent).inspect_target(roots, worker.runner)
            rows.append(
                {
                    "agent": agent,
                    "supported": result.supported,
                    "profile": result.profile.name if result.profile else None,
                    "diagnostics": [*result.diagnostics, *result.warnings],
                    "needs_verification": result.needs_verification,
                }
            )
        except (
            OSError,
            ValueError,
            RuntimeError,
            TypeError,
            LookupError,
            subprocess.SubprocessError,
        ) as exc:
            rows.append(
                {
                    "agent": agent,
                    "supported": False,
                    "profile": None,
                    "diagnostics": [str(exc)],
                    "needs_verification": False,
                }
            )
    return rows


def unfinished_rows(worker: ConversionService) -> list[dict[str, Any]]:
    rows = []
    for identifier in worker.store.ids():
        try:
            operation = worker.store.load(identifier)
            if operation.state not in ("complete", "rolled_back"):
                rows.append(
                    {"id": identifier, "state": operation.state, "error": operation.last_error}
                )
        except (OSError, ValueError, RuntimeError, TypeError, LookupError) as exc:
            rows.append({"id": identifier, "state": "unreadable", "error": str(exc)})
    return rows
