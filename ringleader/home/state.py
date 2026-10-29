from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .. import overview, registry, resume, roundup_clock
from ..agents import source_of
from ..revalidate import Identity

type StepKind = Literal["roundup", "woke", "recent", "publish", "project", "resume"]


@dataclass(frozen=True)
class ProjectRef:
    key: str
    label: str
    pending: int
    unclassified: int
    oldest_mtime: str
    newest_mtime: str


@dataclass(frozen=True)
class NextStep:
    kind: StepKind
    title: str
    detail: str
    deletes: bool
    costs_model: bool
    project: ProjectRef | None = None
    session: tuple[str, str] | None = None


@dataclass(frozen=True)
class HomeState:
    now_iso: str
    live_count: int
    unclassified: int
    spread_projects: int
    blocked_count: int
    stale_count: int
    kept_count: int
    triage_total: int
    recent_undecided: tuple[dict[str, Any], ...]
    woke: tuple[dict[str, Any], ...]
    waking_soon: tuple[dict[str, Any], ...]
    in_flight: tuple[dict[str, Any], ...]
    projects: tuple[ProjectRef, ...]
    pending_harvests: int | None
    last_roundup_at: str | None
    roundup_stale: bool


@dataclass(frozen=True)
class RenderBasis:
    view: int
    minute: datetime
    live_ids: frozenset[Identity] | None
    now: datetime = field(compare=False)

    @classmethod
    def at(cls, view: int, now: datetime, live_ids: frozenset[Identity] | None) -> RenderBasis:
        return cls(view, now.replace(second=0, microsecond=0), live_ids, now)


@dataclass(frozen=True)
class ProjectScope:
    project: ProjectRef


@dataclass(frozen=True)
class RecentScope:
    days: int


@dataclass(frozen=True)
class WokeScope:
    pass


type TriageScope = ProjectScope | RecentScope | WokeScope

RECENT_DAYS = 7
SOON_DAYS = 7
IN_FLIGHT_LIMIT = 8


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _by_mtime(threads: list[dict[str, Any]], *, newest_first: bool) -> list[dict[str, Any]]:
    return sorted(threads, key=lambda t: overview.parse_instant(t["mtime"]), reverse=newest_first)


def visible_threads(reg: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in reg["threads"] if not overview.is_filtered(t)]


def is_woke(thread: dict[str, Any], now_iso: str) -> bool:
    until = thread.get("snoozedUntil")
    if not thread.get("snoozedAt") or not isinstance(until, str):
        return False
    return until <= now_iso and overview.in_triage_inbox(thread, now_iso)


def _is_unclassified(thread: dict[str, Any]) -> bool:
    return bool(thread.get("status", "unclassified") == "unclassified")


LABEL_PATH_PARTS = 3


def short_path(path: str | None) -> str:
    parts = overview.tilde(path or "(unknown)").split("/")
    return "…/" + "/".join(parts[-2:]) if len(parts) > LABEL_PATH_PARTS else "/".join(parts)


def project_label(thread: dict[str, Any]) -> str:
    label = thread.get("gitProjectLabel")
    if label:
        return str(label)
    return short_path(thread.get("project"))


def _project_ref(key: str, threads: list[dict[str, Any]]) -> ProjectRef:
    ordered = _by_mtime(threads, newest_first=False)
    return ProjectRef(
        key=key,
        label=project_label(ordered[0]),
        pending=len(ordered),
        unclassified=sum(1 for t in ordered if _is_unclassified(t)),
        oldest_mtime=ordered[0]["mtime"],
        newest_mtime=ordered[-1]["mtime"],
    )


def project_queue(threads: list[dict[str, Any]], now_iso: str) -> tuple[ProjectRef, ...]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for thread in overview.triage_inbox(threads, now_iso):
        groups[overview.group_key(thread)].append(thread)
    refs = [_project_ref(key, members) for key, members in groups.items()]
    return tuple(
        sorted(refs, key=lambda ref: (overview.parse_instant(ref.oldest_mtime), ref.label.lower()))
    )


def _within_days(thread: dict[str, Any], now_iso: str, days: int) -> bool:
    return overview.age_days(thread["mtime"], now_iso) <= days


def _waking_soon(thread: dict[str, Any], now_iso: str) -> bool:
    until = thread.get("snoozedUntil")
    return (
        overview.snooze_active(thread, now_iso)
        and isinstance(until, str)
        and overview.age_days(now_iso, until) <= SOON_DAYS
    )


def _in_flight(thread: dict[str, Any], now_iso: str) -> bool:
    status = overview.effective_status(thread, now_iso)
    return (
        (status in overview.LIVE_STATUSES or status == "unclassified")
        and _within_days(thread, now_iso, RECENT_DAYS)
        and not thread.get("finishOnExit")
        and not overview.snooze_active(thread, now_iso)
    )


def build_home_state(
    reg: dict[str, Any], now_iso: str, *, pending_harvests: int | None, stale_hours: int
) -> HomeState:
    visible = visible_threads(reg)
    metrics = overview.compute_metrics({**reg, "threads": visible}, now_iso)
    inbox = overview.triage_inbox(visible, now_iso)
    recent = [t for t in inbox if _within_days(t, now_iso, RECENT_DAYS)]
    woke = [t for t in visible if is_woke(t, now_iso)]
    soon = sorted(
        (t for t in visible if _waking_soon(t, now_iso)),
        key=lambda t: overview.parse_instant(t["snoozedUntil"]),
    )
    flying = [t for t in visible if _in_flight(t, now_iso)]
    return HomeState(
        now_iso=now_iso,
        live_count=metrics["live_count"],
        unclassified=metrics["unclassified"],
        spread_projects=metrics["spread_projects"],
        blocked_count=metrics["blocked_count"],
        stale_count=metrics["stale_count"],
        kept_count=metrics["kept_count"],
        triage_total=len(inbox),
        recent_undecided=tuple(_by_mtime(recent, newest_first=True)),
        woke=tuple(_by_mtime(woke, newest_first=False)),
        waking_soon=tuple(soon),
        in_flight=tuple(_by_mtime(flying, newest_first=True)[:IN_FLIGHT_LIMIT]),
        projects=project_queue(visible, now_iso),
        pending_harvests=pending_harvests,
        last_roundup_at=reg.get("lastRoundupAt"),
        roundup_stale=roundup_clock.is_stale(reg, now_iso, stale_hours),
    )


def _last_run(state: HomeState) -> str:
    if state.last_roundup_at is None:
        return "never run"
    return f"last run {overview.age_human(state.last_roundup_at, state.now_iso)}"


def _roundup_step(state: HomeState) -> NextStep:
    return NextStep(
        kind="roundup",
        title=f"Run the roundup ({_last_run(state)})",
        detail="scans, then shows what it would archive, delete and enrich before doing anything",
        deletes=True,
        costs_model=True,
    )


def _woke_step(state: HomeState) -> NextStep:
    return NextStep(
        kind="woke",
        title=f"Review {plural(len(state.woke), 'snoozed session')} that woke up",
        detail="their snooze ran out — decide on each again",
        deletes=False,
        costs_model=False,
    )


def _recent_step(state: HomeState) -> NextStep:
    return NextStep(
        kind="recent",
        title=f"Triage {plural(len(state.recent_undecided), 'session')} from the last "
        f"{RECENT_DAYS} days",
        detail="archive, keep going, snooze, keep long-term or delete each one",
        deletes=False,
        costs_model=False,
    )


def _publish_step(count: int) -> NextStep:
    return NextStep(
        kind="publish",
        title=f"Publish {plural(count, 'pending harvest')} with an agent",
        detail="hands off to your agent's ringleader-publish skill",
        deletes=False,
        costs_model=False,
    )


def _project_step(project: ProjectRef, now_iso: str) -> NextStep:
    oldest = overview.age_human(project.oldest_mtime, now_iso).removesuffix(" ago")
    return NextStep(
        kind="project",
        title=f"Triage {project.label} next ({project.pending} waiting, oldest {oldest})",
        detail=f"{project.unclassified} never classified · oldest activity first",
        deletes=False,
        costs_model=False,
        project=project,
    )


def _resume_step(thread: dict[str, Any], now_iso: str) -> NextStep:
    where = overview.tilde(thread.get("project"))
    age = overview.age_human(thread.get("mtime"), now_iso)
    return NextStep(
        kind="resume",
        title=f"Resume {resume.label(thread)}",
        detail=f"{resume.agent_name(source_of(thread))} · {age} · {where}",
        deletes=False,
        costs_model=False,
        session=registry.identity(thread),
    )


def _has_backlog(state: HomeState) -> bool:
    covered = {registry.identity(t) for t in (*state.recent_undecided, *state.woke)}
    return bool(state.projects) and state.triage_total > len(covered)


def next_steps(state: HomeState) -> tuple[NextStep, ...]:
    steps = [
        *([_roundup_step(state)] if state.roundup_stale else []),
        *([_woke_step(state)] if state.woke else []),
        *([_recent_step(state)] if state.recent_undecided else []),
        *([_publish_step(state.pending_harvests)] if state.pending_harvests else []),
        *([_project_step(state.projects[0], state.now_iso)] if _has_backlog(state) else []),
    ]
    if steps or not state.in_flight:
        return tuple(steps)
    return (_resume_step(state.in_flight[0], state.now_iso),)


def scope_rows(
    scope: TriageScope, threads: list[dict[str, Any]], now_iso: str
) -> list[dict[str, Any]]:
    match scope:
        case ProjectScope(project=project):
            rows = [
                t
                for t in overview.triage_inbox(threads, now_iso)
                if overview.group_key(t) == project.key
            ]
        case RecentScope(days=days):
            rows = [
                t for t in overview.triage_inbox(threads, now_iso) if _within_days(t, now_iso, days)
            ]
        case WokeScope():
            rows = [t for t in threads if is_woke(t, now_iso)]
    return _by_mtime(rows, newest_first=False)


def scope_title(scope: TriageScope) -> str:
    match scope:
        case ProjectScope(project=project):
            return project.label
        case RecentScope(days=days):
            return f"Last {days} days"
        case WokeScope():
            return "Woke from snooze"
