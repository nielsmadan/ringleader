from __future__ import annotations

import hashlib
import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from itertools import combinations

from . import rollups
from .embed import Embedder
from .rollups import ProjectInfo, Scope

MIN_ISSUE_SESSIONS = 2
MERGE_THRESHOLD = 0.92
MAX_EMBED_SIGNATURES = 500
ID_CHARS = 16
EXAMPLE_SESSIONS = 3
EXAMPLE_TEXTS = 3
EXAMPLE_CHARS = 300


@dataclass(frozen=True)
class Issue:
    issue_id: str
    signatures: tuple[str, ...]
    sessions: int
    occurrences: int
    agents: tuple[str, ...]
    first_seen: str
    last_seen: str
    example_sessions: tuple[tuple[str, str], ...]
    example_text: str
    content_hash: str
    name: str | None
    summary: str | None


@dataclass(frozen=True)
class Merge:
    groups: list[list[str]]
    embed_failure: str | None


@dataclass(frozen=True)
class IssueFindings:
    issues: list[Issue]
    embed_failure: str | None


@dataclass(frozen=True)
class _Occurrence:
    ts: str
    agent: str
    session_id: str
    text: str


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:ID_CHARS]


def issue_id(signatures: Sequence[str]) -> str:
    return _digest("\n".join(sorted(signatures)))


def _by_signature(rows: Sequence[sqlite3.Row]) -> dict[str, list[_Occurrence]]:
    found: defaultdict[str, list[_Occurrence]] = defaultdict(list)
    for r in rows:
        found[r["signature"]].append(
            _Occurrence(r["ts"], r["t_agent"], r["t_session"], r["error_text"] or "")
        )
    return dict(found)


def _sessions(occurrences: Sequence[_Occurrence]) -> int:
    return len({(o.agent, o.session_id) for o in occurrences})


def _normalized(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(math.sumprod(vector, vector))
    return [x / norm for x in vector] if norm else [0.0] * len(vector)


def _components(count: int, pairs: Sequence[tuple[int, int]]) -> list[list[int]]:
    parent = list(range(count))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in pairs:
        parent[root(a)] = root(b)
    groups: defaultdict[int, list[int]] = defaultdict(list)
    for i in range(count):
        groups[root(i)].append(i)
    return list(groups.values())


def merge_signatures(
    signatures: Sequence[str], embedder: Embedder | None, threshold: float = MERGE_THRESHOLD
) -> Merge:
    exact = [[s] for s in signatures]
    if embedder is None or len(signatures) <= 1:
        return Merge(exact, None)
    head = list(signatures[:MAX_EMBED_SIGNATURES])
    try:
        vectors = [_normalized(v) for v in embedder.encode(head)]
    except Exception as error:  # noqa: BLE001 -- a failing model degrades to exact signatures
        return Merge(exact, f"embedding failed: {error}")
    if len(vectors) != len(head):
        return Merge(exact, f"embedder returned {len(vectors)} vectors for {len(head)} signatures")
    pairs = [
        (i, j)
        for i, j in combinations(range(len(head)), 2)
        if math.sumprod(vectors[i], vectors[j]) >= threshold
    ]
    merged = [[head[i] for i in members] for members in _components(len(head), pairs)]
    return Merge(merged + [[s] for s in signatures[MAX_EMBED_SIGNATURES:]], None)


def _build(
    members: Sequence[str],
    found: Mapping[str, Sequence[_Occurrence]],
    names: Mapping[str, tuple[str, str]],
) -> Issue:
    ordered = sorted(
        (o for s in members for o in found[s]), key=lambda o: (o.ts, o.agent, o.session_id, o.text)
    )
    texts = tuple(dict.fromkeys(o.text for o in ordered))[:EXAMPLE_TEXTS]
    sessions = tuple(dict.fromkeys((o.agent, o.session_id) for o in ordered))
    signatures = tuple(sorted(members))
    ident = issue_id(signatures)
    name, summary = names.get(ident, (None, None))
    return Issue(
        issue_id=ident,
        signatures=signatures,
        sessions=len(sessions),
        occurrences=len(ordered),
        agents=tuple(sorted({agent for agent, _ in sessions})),
        first_seen=ordered[0].ts,
        last_seen=ordered[-1].ts,
        example_sessions=sessions[:EXAMPLE_SESSIONS],
        example_text=texts[0][:EXAMPLE_CHARS] if texts else "",
        content_hash=_digest("\n".join(texts)),
        name=name,
        summary=summary,
    )


def group_issues(
    rows: Sequence[sqlite3.Row],
    *,
    embedder: Embedder | None,
    names: Mapping[str, tuple[str, str]],
    min_sessions: int,
    limit: int,
) -> IssueFindings:
    found = _by_signature(rows)
    kept = sorted(
        (s for s, occ in found.items() if _sessions(occ) >= min_sessions),
        key=lambda s: (-_sessions(found[s]), -len(found[s]), s),
    )
    merge = merge_signatures(kept, embedder)
    issues = [_build(members, found, names) for members in merge.groups]
    issues.sort(key=lambda i: (-i.sessions, -i.occurrences, i.issue_id))
    return IssueFindings(issues[:limit], merge.embed_failure)


def find_issues(  # noqa: PLR0913 -- mirrors the shared recurring_issues signature
    conn: sqlite3.Connection,
    scope: Scope,
    *,
    embedder: Embedder | None,
    names: Mapping[str, tuple[str, str]],
    min_sessions: int,
    limit: int,
    projects: Mapping[tuple[str, str], ProjectInfo] | None = None,
) -> IssueFindings:
    return group_issues(
        rollups.error_calls(conn, scope, projects),
        embedder=embedder,
        names=names,
        min_sessions=min_sessions,
        limit=limit,
    )


def issues_with_signatures(found: Sequence[Issue], signatures: AbstractSet[str]) -> tuple[str, ...]:
    return tuple(i.issue_id for i in found if not signatures.isdisjoint(i.signatures))


def recurring_issues(  # noqa: PLR0913 -- the shared interface fixes this signature
    conn: sqlite3.Connection,
    scope: Scope,
    *,
    embedder: Embedder | None,
    names: Mapping[str, tuple[str, str]],
    min_sessions: int,
    limit: int,
    projects: Mapping[tuple[str, str], ProjectInfo] | None = None,
) -> list[Issue]:
    return find_issues(
        conn,
        scope,
        embedder=embedder,
        names=names,
        min_sessions=min_sessions,
        limit=limit,
        projects=projects,
    ).issues
