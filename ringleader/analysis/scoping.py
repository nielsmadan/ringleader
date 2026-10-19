from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

from ..projects import matches

if TYPE_CHECKING:
    from .rollups import ProjectInfo, Scope


@dataclass(frozen=True)
class Filter:
    where: str
    params: tuple[Any, ...]


def selected(selectors: Sequence[str], info: ProjectInfo | None, cwd: str | None) -> bool:
    if not selectors:
        return True
    thread = {
        "gitProjectLabel": info.label if info else None,
        "gitProjectKey": info.key if info else None,
        "gitRoot": info.root if info else None,
        "project": cwd,
    }
    return any(matches(thread, selector) for selector in selectors)


def placeholders(n: int) -> str:
    return ", ".join("?" * n)


def scope_filter(
    conn: sqlite3.Connection,
    scope: Scope,
    projects: Mapping[tuple[str, str], ProjectInfo] | None,
) -> Filter:
    clauses: list[str] = []
    params: list[Any] = []
    if not scope.include_archived:
        clauses.append("t.archived = 0")
    if scope.agents:
        clauses.append(f"t.agent IN ({placeholders(len(scope.agents))})")
        params.extend(sorted(scope.agents))
    if scope.selectors:
        known = projects or {}

        @cache
        def chosen(agent: str, session_id: str, cwd: str | None) -> int:
            return int(selected(scope.selectors, known.get((agent, session_id)), cwd))

        name = f"rl_selected_{id(chosen)}"
        conn.create_function(name, 3, chosen, deterministic=True)
        clauses.append(f"{name}(t.agent, t.session_id, t.cwd)")
    return Filter(" AND ".join(clauses) or "1", tuple(params))


def days_clause(scope: Scope | None, expression: str, flt: Filter) -> tuple[str, tuple[Any, ...]]:
    where, params = flt.where, list(flt.params)
    if scope is not None and scope.since:
        where += f" AND {expression} >= ?"
        params.append(scope.since.isoformat())
    if scope is not None and scope.until:
        where += f" AND {expression} <= ?"
        params.append(scope.until.isoformat())
    return where, tuple(params)
