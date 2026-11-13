from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import cache
from itertools import count, pairwise
from pathlib import Path
from typing import Any, Literal, cast

from . import namefilter, overview, scanner, search
from .agents import canonical_agent, display_agent, source_of
from .search import BLURB_FIELDS
from .sources.base import (
    ForkNamingSource,
    SessionLaunchingSource,
    WorkspaceRelocatingSource,
    in_workspace,
)

SessionAction = Literal["resume", "fork", "worktree"]

_UNKNOWN = "(unknown)"

_BLURB_TIER = 0
_FUZZY_TIER = 1
_NAME_SUBSTRING_TIER = 2
_NAME_PREFIX_TIER = 3
_NAME_EXACT_TIER = 4

_CONSECUTIVE_WEIGHT = 3000
_BOUNDARY_WEIGHT = 2000
_START_WEIGHT = 1000
_LENGTH_WEIGHT = 500
_START_SPAN = 50
_LENGTH_SPAN = 500

_MAX_SCORE = _CONSECUTIVE_WEIGHT + _BOUNDARY_WEIGHT + _START_WEIGHT + _LENGTH_WEIGHT

# one tier step outruns every score inside a tier, so a blurb hit never beats a name hit
_TIER_BONUS = _MAX_SCORE + 1


@dataclass(frozen=True)
class Haystack:
    name: str
    sid: str
    project: str
    blurbs: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        texts = (self.name, self.sid, self.project, *(text for _, text in self.blurbs))
        if any(text != text.lower() for text in texts):
            raise ValueError("Haystack text must be lowercased; build it with Haystack.of")

    @classmethod
    def of(
        cls, *, name: str, sid: str, project: str, blurbs: Iterable[tuple[str, str]]
    ) -> Haystack:
        return cls(
            name=name.lower(),
            sid=sid.lower(),
            project=project.lower(),
            blurbs=tuple((field, text.lower()) for field, text in blurbs),
        )


class Scope(Enum):
    CWD = "cwd"
    RECURSIVE = "recursive"
    ALL = "all"


def bounds(scope: Scope, cwd: str) -> tuple[str | None, bool]:
    """The workspace a scope covers — `None` for everywhere — and whether it reaches below."""
    return (None if scope is Scope.ALL else cwd), scope is Scope.RECURSIVE


def in_scope(thread: dict[str, Any], scope: Scope, cwd: str) -> bool:
    root, recursive = bounds(scope, cwd)
    return in_workspace(thread.get("project"), root, recursive=recursive)


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


def _score_lowered(q: str, t: str) -> int | None:
    # either end alone has a blind spot: a run is invisible to the scan that starts away
    # from it, so take the better of the two alignments
    rated = [_rate(q, t, idxs) for idxs in (_align_left(q, t), _align_right(q, t)) if idxs]
    return max(rated) if rated else None


def score(query: str, text: str) -> int | None:
    if not query:
        return 0
    return _score_lowered(query.lower(), text.lower())


def _lowered(value: str | None) -> str:
    return (search.clean(value) or "").lower()


def _name(thread: dict[str, Any]) -> str | None:
    return search.clean(namefilter.display_name(thread)) or search.clean(thread.get("aiTitle"))


def _project_label(thread: dict[str, Any]) -> str:
    label_ = thread.get("gitProjectLabel")
    if label_:
        return _lowered(label_)
    project = thread.get("project")
    return _lowered(Path(project).name) if isinstance(project, str) else ""


def haystack_of(thread: dict[str, Any]) -> Haystack:
    blurbs = ((field, _lowered(thread.get(field))) for field in BLURB_FIELDS)
    return Haystack.of(
        name=_name(thread) or "",
        sid=_lowered(thread.get("sessionId")),
        project=_project_label(thread),
        blurbs=((field, text) for field, text in blurbs if text),
    )


def _name_match(q: str, tokens: list[str], name: str) -> tuple[int, str] | None:
    if not name:
        return None
    if name == q:
        return _NAME_EXACT_TIER * _TIER_BONUS, "name"
    if name.startswith(q):
        return _NAME_PREFIX_TIER * _TIER_BONUS, "name"
    if all(token in name for token in tokens):
        return _NAME_SUBSTRING_TIER * _TIER_BONUS + (_score_lowered(q, name) or 0), "name"
    return None


def _fuzzy_match(compact: str, haystack: Haystack) -> tuple[int, str] | None:
    scored = [
        (raw, field)
        for field, raw in (
            ("name", _score_lowered(compact, haystack.name) if haystack.name else None),
            (
                "id",
                _score_lowered(compact, haystack.sid) if haystack.sid.startswith(compact) else None,
            ),
            ("project", _score_lowered(compact, haystack.project) if haystack.project else None),
        )
        if raw is not None
    ]
    if not scored:
        return None
    raw, field = max(scored, key=lambda pair: pair[0])
    return _FUZZY_TIER * _TIER_BONUS + raw, field


def _blurb_match(tokens: list[str], haystack: Haystack) -> tuple[int, str] | None:
    fields = (("name", haystack.name), ("project", haystack.project), *haystack.blurbs)
    if not all(any(token in text for _, text in fields) for token in tokens):
        return None
    whole = next((f for f, text in haystack.blurbs if all(t in text for t in tokens)), None)
    field = whole or next(f for f, text in fields if tokens[0] in text)
    return _BLURB_TIER * _TIER_BONUS, field


def match(
    thread: dict[str, Any], query: str, haystack: Haystack | None = None
) -> tuple[int, str] | None:
    """Fuzzy over the short identifying fields only; a long blurb matches by substring,
    or a short query would find its letters scattered through nearly every summary."""
    tokens = query.lower().split()
    if not tokens:
        return None
    q, compact = " ".join(tokens), "".join(tokens)
    found = haystack or haystack_of(thread)
    return (
        _name_match(q, tokens, found.name)
        or _fuzzy_match(compact, found)
        or _blurb_match(tokens, found)
    )


def rank(
    threads: list[dict[str, Any]],
    query: str,
    haystack_of: Callable[[dict[str, Any]], Haystack] = haystack_of,
) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return _by_recency(threads)
    scored = [(m[0], t) for t in threads if (m := match(t, q, haystack_of(t))) is not None]
    scored.sort(key=lambda pair: pair[1].get("mtime") or "", reverse=True)
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [t for _, t in scored]


def label(thread: dict[str, Any]) -> str:
    return _name(thread) or (search.clean(thread.get("sessionId")) or "")[:8] or _UNKNOWN


def _status(thread: dict[str, Any], now_iso: str) -> str | None:
    # effective_status subscripts mtime, but only once a decision has failed to settle it
    mtime = thread.get("mtime")
    settled = thread.get("decision") in ("harvested", "abandon")
    if not settled and (not isinstance(mtime, str) or not mtime):
        return None
    return overview.effective_status(thread, now_iso)


def badges(
    thread: dict[str, Any],
    now_iso: str,
    sources: Mapping[str, WorkspaceRelocatingSource] | None = None,
) -> list[str]:
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
    if drifted(thread, sources):
        out.append("drifted")
    return out


agent_name = display_agent
source_name = canonical_agent


def launchable_agents() -> tuple[str, ...]:
    sources = (source_type() for source_type in scanner.SOURCE_TYPES)
    return tuple(
        source.name
        for source in sources
        if scanner.supports(source, "launches_sessions", SessionLaunchingSource)
    )


def forks_sessions(name: str) -> bool:
    return scanner.supports(scanner.source_by_name(name), "forks_sessions", SessionLaunchingSource)


def fork_namer(name: str) -> ForkNamingSource | None:
    src = scanner.source_by_name(name)
    named = scanner.supports(src, "names_forks", ForkNamingSource)
    return src if isinstance(src, ForkNamingSource) and named and forks_sessions(name) else None


def names_forks(name: str) -> bool:
    return fork_namer(name) is not None


_FORK_SUFFIX = re.compile(r"(?P<stem>.*)-fork-(?P<number>\d+)")


def fork_name(name: str, taken: Iterable[str]) -> str:
    """`name-fork-1`, or the next number up when `name` is itself a numbered fork,
    skipping any number another session already holds."""
    match = _FORK_SUFFIX.fullmatch(name)
    stem, start = (match["stem"], int(match["number"]) + 1) if match else (name, 1)
    used = frozenset(taken)
    return next(f"{stem}-fork-{n}" for n in count(start) if f"{stem}-fork-{n}" not in used)


def suggested_fork_name(thread: dict[str, Any], threads: Iterable[dict[str, Any]]) -> str:
    return fork_name(label(thread), (label(t) for t in threads))


@cache
def _slug_keyed_sources() -> frozenset[str]:
    """Sources whose agent files a transcript under the *launch* directory's slug, so
    a session that cd'd is stored where its recorded cwd does not predict."""
    return frozenset(
        source.name
        for source in (source_type() for source_type in scanner.SOURCE_TYPES)
        if scanner.supports(source, "relocates_workspace", WorkspaceRelocatingSource)
    )


def slug_sources() -> dict[str, WorkspaceRelocatingSource]:
    return {
        name: cast(WorkspaceRelocatingSource, scanner.source_by_name(name))
        for name in _slug_keyed_sources()
    }


def drifted(
    thread: dict[str, Any], sources: Mapping[str, WorkspaceRelocatingSource] | None = None
) -> bool:
    name = source_of(thread)
    if name not in _slug_keyed_sources():
        return False
    file, project = thread.get("file"), thread.get("project")
    if not file or not project:
        return False
    source = sources[name] if sources else scanner.source_by_name(name)
    stored = source.storage_dir(file)
    # `root` is not part of the Source protocol, so read it off the concrete source
    root: Path | None = getattr(source, "root", None)
    if root is not None and stored == root:
        return False  # droid's global-root layout: no slug dir to compare against
    return stored.name != source.storage_for(project).name


def launch_cwd(thread: dict[str, Any], threads: Sequence[dict[str, Any]] = ()) -> str | None:
    project = thread.get("project")
    if not project:
        return None
    if not drifted(thread):
        return str(project)
    source = slug_sources()[source_of(thread)]
    stored = source.storage_dir(thread["file"])
    for other in threads:
        if source_of(other) != source_of(thread):
            continue
        if not other.get("file") or not other.get("project"):
            continue
        if source.storage_dir(other["file"]) == stored and not drifted(other):
            return str(other["project"])
    return str(project)
