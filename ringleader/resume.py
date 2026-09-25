from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from enum import Enum
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

from . import namefilter, overview, scanner, search
from .search import _BLURB_FIELDS
from .slug import stored_under
from .sources.base import SessionLaunchingSource, WorkspaceRelocatingSource

_UNKNOWN = "(unknown)"

_NAME_TIER = 0
_ID_TIER = 1
_BLURB_TIER = 2

_CONSECUTIVE_WEIGHT = 3000
_BOUNDARY_WEIGHT = 2000
_START_WEIGHT = 1000
_LENGTH_WEIGHT = 500
_START_SPAN = 50
_LENGTH_SPAN = 500

_MAX_SCORE = _CONSECUTIVE_WEIGHT + _BOUNDARY_WEIGHT + _START_WEIGHT + _LENGTH_WEIGHT

# one tier step outruns every score inside a tier, so a blurb hit never beats a name hit
_TIER_BONUS = _MAX_SCORE + 1


class Scope(Enum):
    CWD = "cwd"
    RECURSIVE = "recursive"
    ALL = "all"


def in_scope(thread: dict[str, Any], scope: Scope, cwd: str) -> bool:
    if scope is Scope.ALL:
        return True
    raw = thread.get("project")
    if not raw:
        return False
    project, root = cast(str, raw.rstrip("/")), cwd.rstrip("/")
    if scope is Scope.CWD:
        return project == root
    return project == root or project.startswith(root + "/")


def eligible(thread: dict[str, Any]) -> bool:
    return bool(thread.get("sessionId"))


def _by_recency(threads: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(threads, key=lambda t: t.get("mtime") or "", reverse=True)


def candidates(reg: dict[str, Any], *, scope: Scope, cwd: str) -> list[dict[str, Any]]:
    threads = reg.get("threads") or []
    return _by_recency(t for t in threads if eligible(t) and in_scope(t, scope, cwd))


def _align_right(q: str, t: str) -> list[int] | None:
    end, idxs = len(t), []
    for ch in reversed(q):
        idx = t.rfind(ch, 0, end)
        if idx < 0:
            return None
        idxs.append(idx)
        end = idx
    idxs.reverse()
    return idxs


def _align_left(q: str, t: str) -> list[int] | None:
    pos, idxs = 0, []
    for ch in q:
        idx = t.find(ch, pos)
        if idx < 0:
            return None
        idxs.append(idx)
        pos = idx + 1
    return idxs


def _rate(q: str, t: str, idxs: list[int]) -> int:
    consecutive = sum(1 for a, b in pairwise(idxs) if b == a + 1)
    boundary = sum(1 for i in idxs if i == 0 or not t[i - 1].isalnum())
    return int(
        _CONSECUTIVE_WEIGHT * consecutive / max(len(q) - 1, 1)
        + _BOUNDARY_WEIGHT * boundary / len(q)
        + _START_WEIGHT * (1 - min(idxs[0], _START_SPAN) / _START_SPAN)
        + _LENGTH_WEIGHT * (1 - min(len(t), _LENGTH_SPAN) / _LENGTH_SPAN)
    )


def score(query: str, text: str) -> int | None:
    if not query:
        return 0
    q, t = query.lower(), text.lower()
    # either end alone has a blind spot: a run is invisible to the scan that starts away
    # from it, so take the better of the two alignments
    rated = [_rate(q, t, idxs) for idxs in (_align_left(q, t), _align_right(q, t)) if idxs]
    return max(rated) if rated else None


def _fields(thread: dict[str, Any]) -> Iterator[tuple[str, str, int]]:
    name = label(thread)
    if name != _UNKNOWN:
        yield "name", name, _NAME_TIER
    sid = search.clean(thread.get("sessionId"))
    if sid:
        yield "id", sid, _ID_TIER
    for field in _BLURB_FIELDS:
        value = search.clean(thread.get(field))
        if value:
            yield field, value, _BLURB_TIER


def match(thread: dict[str, Any], query: str) -> tuple[int, str] | None:
    best: tuple[int, str] | None = None
    for field, value, tier in _fields(thread):
        raw = score(query, value)
        if raw is None:
            continue
        total = raw + (_BLURB_TIER - tier) * _TIER_BONUS
        if best is None or total > best[0]:
            best = (total, field)
    return best


def rank(threads: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return _by_recency(threads)
    scored = [(m[0], t) for t in threads if (m := match(t, q)) is not None]
    scored.sort(key=lambda pair: pair[1].get("mtime") or "", reverse=True)
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [t for _, t in scored]


def label(thread: dict[str, Any]) -> str:
    for value in (namefilter.display_name(thread), thread.get("aiTitle")):
        cleaned = search.clean(value)
        if cleaned:
            return cleaned
    return (search.clean(thread.get("sessionId")) or "")[:8] or _UNKNOWN


def _status(thread: dict[str, Any], now_iso: str) -> str | None:
    # effective_status subscripts mtime, but only once a decision has failed to settle it
    mtime = thread.get("mtime")
    settled = thread.get("decision") in ("harvested", "abandon")
    if not settled and (not isinstance(mtime, str) or not mtime):
        return None
    return overview.effective_status(thread, now_iso)


def badges(thread: dict[str, Any], now_iso: str) -> list[str]:
    out = []
    if overview.is_filtered(thread):
        out.append("filtered")
    if overview.snooze_active(thread, now_iso):
        out.append("snoozed")
    if overview.is_kept(thread):
        out.append("kept")
    if thread.get("finishOnExit"):
        out.append("finishing")
    status = _status(thread, now_iso)
    if status in ("harvested", "abandoned"):
        out.append(status)
    if drifted(thread):
        out.append("drifted")
    return out


def source_of(thread: dict[str, Any]) -> str:
    return thread.get("source") or "claude-code"


def agent_name(source: str) -> str:
    return "claude" if source == "claude-code" else source


def source_name(agent: str) -> str:
    return "claude-code" if agent == "claude" else agent


def launchable_agents() -> tuple[str, ...]:
    sources = (source_type() for source_type in scanner.SOURCE_TYPES)
    return tuple(
        source.name
        for source in sources
        if scanner.supports(source, "launches_sessions", SessionLaunchingSource)
    )


@cache
def _slug_keyed_sources() -> frozenset[str]:
    """Sources whose agent files a transcript under the *launch* directory's slug, so
    a session that cd'd is stored where its recorded cwd does not predict."""
    return frozenset(
        source.name
        for source in (source_type() for source_type in scanner.SOURCE_TYPES)
        if scanner.supports(source, "relocates_workspace", WorkspaceRelocatingSource)
    )


def _in_source_root(name: str, file: str) -> bool:
    source = scanner.source_by_name(name)
    # `root` is not part of the Source protocol, so read it off the concrete source
    root: Path | None = getattr(source, "root", None)
    return source is not None and root is not None and Path(file).parent == root


def drifted(thread: dict[str, Any]) -> bool:
    # the transcript is filed under the *launch* dir's slug; a different slug means it cd'd
    name = source_of(thread)
    if name not in _slug_keyed_sources():
        return False
    file, project = thread.get("file"), thread.get("project")
    if not file or not project:
        return False
    if _in_source_root(name, file):
        return False  # droid's global-root layout: no slug dir to compare against
    return not stored_under(file, project)


def launch_cwd(thread: dict[str, Any], threads: Sequence[dict[str, Any]] = ()) -> str | None:
    project = thread.get("project")
    if not project:
        return None
    if not drifted(thread):
        return str(project)
    parent = Path(thread["file"]).parent
    for other in threads:
        if source_of(other) != source_of(thread):
            continue
        if not other.get("file") or not other.get("project"):
            continue
        if Path(other["file"]).parent == parent and not drifted(other):
            return str(other["project"])
    return str(project)
