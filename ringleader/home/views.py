from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Literal, NamedTuple

from .. import overview

UNKNOWN = "(unknown)"
UNKNOWN_LABEL = "Unknown folder"
DAYS_BACK = 6
WEEKS_BACK = 8

type PeriodKind = Literal["day", "week", "month"]
type PeriodLabel = Callable[[PeriodKind, date], str]


class Stamped(NamedTuple):
    at: datetime
    thread: dict[str, Any]


@dataclass(frozen=True)
class FolderScope:
    path: str


@dataclass(frozen=True)
class TreeScope:
    path: str
    excluding: str | None = None


@dataclass(frozen=True)
class PeriodScope:
    start: datetime
    end: datetime | None
    label: str


@dataclass(frozen=True)
class FolderGroup:
    key: str
    label: str
    pending: int
    unclassified: int
    oldest_mtime: str
    newest_mtime: str

    @property
    def scope(self) -> FolderScope:
        return FolderScope(self.key)


@dataclass(frozen=True)
class FolderNode:
    path: str
    name: str
    own: int
    total: int
    children: tuple[FolderNode, ...]
    scope: TreeScope


@dataclass(frozen=True)
class DateBucket:
    key: str
    label: str
    kind: PeriodKind
    start: datetime
    end: datetime | None
    pending: int
    unclassified: int
    oldest_mtime: str
    newest_mtime: str

    @property
    def scope(self) -> PeriodScope:
        return PeriodScope(self.start, self.end, self.label)


def folder_of(thread: dict[str, Any]) -> str:
    return str(thread.get("project") or UNKNOWN)


def is_unclassified(thread: dict[str, Any]) -> bool:
    return bool(thread.get("status", "unclassified") == "unclassified")


def _under(folder: str, path: str) -> bool:
    return folder == path or folder.startswith(path.rstrip("/") + "/")


def matches(scope: FolderScope | TreeScope | PeriodScope, thread: dict[str, Any]) -> bool:
    match scope:
        case FolderScope(path=path):
            return folder_of(thread) == path
        case TreeScope(path=path, excluding=excluding):
            folder = folder_of(thread)
            if UNKNOWN in (path, folder):
                return folder == path
            return _under(folder, path) and not (excluding and _under(folder, excluding))
        case PeriodScope(start=start, end=end):
            mtime = overview.parse_instant(thread["mtime"])
            return start <= mtime and (end is None or mtime < end)


def _stamped(inbox: Iterable[dict[str, Any]]) -> list[Stamped]:
    return [Stamped(overview.parse_instant(t["mtime"]), t) for t in inbox]


def _counts(members: list[Stamped]) -> tuple[int, int, str, str]:
    oldest = min(members, key=lambda m: m.at).thread
    newest = max(members, key=lambda m: m.at).thread
    unclassified = sum(1 for m in members if is_unclassified(m.thread))
    return len(members), unclassified, oldest["mtime"], newest["mtime"]


def _by_folder(stamped: list[Stamped]) -> dict[str, list[Stamped]]:
    groups: dict[str, list[Stamped]] = defaultdict(list)
    for member in stamped:
        groups[folder_of(member.thread)].append(member)
    return groups


def folder_label(path: str, home: str) -> str:
    return UNKNOWN_LABEL if path == UNKNOWN else overview.tilde(path, home)


def folder_groups(inbox: Iterable[dict[str, Any]], home: str) -> tuple[FolderGroup, ...]:
    groups = [
        FolderGroup(key, folder_label(key, home), *_counts(members))
        for key, members in _by_folder(_stamped(inbox)).items()
    ]
    return tuple(sorted(groups, key=lambda g: (g.label.lower(), g.key)))


@dataclass
class _Trie:
    path: str
    name: str
    own: int = 0
    children: dict[str, _Trie] = field(default_factory=dict)

    def child(self, name: str) -> _Trie:
        if name not in self.children:
            self.children[name] = _Trie(f"{self.path.rstrip('/')}/{name}", name)
        return self.children[name]


def _join(name: str, child: str) -> str:
    return name + child if name.endswith("/") else f"{name}/{child}"


def _freeze(node: _Trie, excluding: str | None) -> FolderNode:
    children = [_freeze(c, excluding) for c in node.children.values()]
    total = node.own + sum(c.total for c in children)
    if node.own == 0 and len(children) == 1:
        only = children[0]
        return FolderNode(
            only.path, _join(node.name, only.name), only.own, total, only.children, only.scope
        )
    ordered = tuple(sorted(children, key=lambda c: (c.name.lower(), c.name)))
    scope = TreeScope(node.path, excluding if excluding and _under(excluding, node.path) else None)
    return FolderNode(node.path, node.name, node.own, total, ordered, scope)


def _root(trie: _Trie, excluding: str | None) -> list[FolderNode]:
    return [_freeze(trie, excluding)] if trie.own or trie.children else []


def _leaf(path: str, name: str, own: int) -> FolderNode:
    return FolderNode(path, name, own, own, (), TreeScope(path))


def folder_tree(inbox: Iterable[dict[str, Any]], home: str) -> tuple[FolderNode, ...]:
    counts = Counter(folder_of(t) for t in inbox)
    home_root = _Trie(home, "~")
    other_root = _Trie("/", "/")
    loose: list[FolderNode] = []
    for folder, own in counts.items():
        if folder == UNKNOWN:
            continue
        if not folder.startswith("/"):
            loose.append(_leaf(folder, folder, own))
            continue
        inside = _under(folder, home)
        root = home_root if inside else other_root
        rest = folder[len(home) :] if inside else folder
        node = root
        for segment in (s for s in rest.split("/") if s):
            node = node.child(segment)
        node.own += own
    roots = [
        *_root(home_root, None),
        *_root(other_root, home),
        *sorted(loose, key=lambda n: (n.name.lower(), n.name)),
        *([_leaf(UNKNOWN, UNKNOWN_LABEL, counts[UNKNOWN])] if UNKNOWN in counts else []),
    ]
    return tuple(roots)


def iso_period_label(kind: PeriodKind, first: date) -> str:
    return first.strftime("%Y-%m") if kind == "month" else first.isoformat()


def _bucket_key(kind: PeriodKind, first: date) -> str:
    return f"{kind}:{first:%Y-%m}" if kind == "month" else f"{kind}:{first.isoformat()}"


def _midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=tz)


def _next_month(first: date) -> date:
    return date(first.year + first.month // 12, first.month % 12 + 1, 1)


def _period(
    local_day: date, today: date, day_floor: date, week_floor: date
) -> tuple[PeriodKind, date, date | None]:
    if local_day >= today:
        return "day", today, None
    if local_day >= day_floor:
        return "day", local_day, local_day + timedelta(days=1)
    if local_day >= week_floor:
        monday = local_day - timedelta(days=local_day.weekday())
        return "week", monday, min(monday + timedelta(days=7), day_floor)
    first = local_day.replace(day=1)
    return "month", first, min(_next_month(first), week_floor)


def date_buckets(
    inbox: Iterable[dict[str, Any]],
    now: datetime,
    tz: tzinfo,
    *,
    label: PeriodLabel = iso_period_label,
) -> tuple[DateBucket, ...]:
    today = now.astimezone(tz).date()
    day_floor = today - timedelta(days=DAYS_BACK)
    week_floor = today - timedelta(days=today.weekday() + 7 * WEEKS_BACK)
    groups: dict[tuple[PeriodKind, date, date | None], list[Stamped]] = defaultdict(list)
    for member in _stamped(inbox):
        local_day = member.at.astimezone(tz).date()
        groups[_period(local_day, today, day_floor, week_floor)].append(member)
    buckets = [
        DateBucket(
            _bucket_key(kind, first),
            label(kind, first),
            kind,
            _midnight(first, tz),
            None if end is None else _midnight(end, tz),
            *_counts(members),
        )
        for (kind, first, end), members in groups.items()
    ]
    return tuple(sorted(buckets, key=lambda b: b.start, reverse=True))
