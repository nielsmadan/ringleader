from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

LIVE_STATUSES = ("active", "unfinished", "blocked")
FILTERABLE_STATUSES = (*LIVE_STATUSES, "finished", "abandoned", "harvested", "unclassified")
ABANDON_AGE_DAYS = 21

PROJECT_W = 30


class StatusMode(Enum):
    DEFAULT = "default"
    FILTERED = "filtered"
    ALL = "all"


@dataclass(frozen=True)
class StatusSelection:
    mode: StatusMode
    statuses: tuple[str, ...] = ()

    @classmethod
    def default(cls) -> StatusSelection:
        return cls(StatusMode.DEFAULT)

    @classmethod
    def filtered(cls, statuses) -> StatusSelection:
        return cls(StatusMode.FILTERED, tuple(dict.fromkeys(statuses)))

    @classmethod
    def all(cls) -> StatusSelection:
        return cls(StatusMode.ALL)


DEFAULT_STATUS_SELECTION = StatusSelection.default()


def _wrap(s: str, w: int = PROJECT_W) -> list[str]:
    """Wrap a label to width w, preferring to break after a path separator so the
    numeric columns stay anchored to the first line."""
    lines = []
    while len(s) > w:
        cut = s.rfind("/", 0, w + 1)
        cut = cut + 1 if cut > 0 else w
        lines.append(s[:cut])
        s = s[cut:]
    lines.append(s)
    return lines


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def age_days(mtime_iso: str, now_iso: str) -> float:
    return (_parse(now_iso) - _parse(mtime_iso)).total_seconds() / 86400.0


def is_kept(thread: dict) -> bool:
    """Kept long-term: out of the triage inbox for good, but still live and resumable."""
    return thread.get("decision") == "keep"


def effective_status(thread: dict, now_iso: str) -> str:
    dec = thread.get("decision")
    if dec == "harvested":
        return "harvested"
    if dec == "abandon":
        return "abandoned"
    st = thread.get("status", "unclassified")
    if dec == "keep":
        return st  # never auto-derive abandoned for kept sessions
    if st == "unfinished" and age_days(thread["mtime"], now_iso) > ABANDON_AGE_DAYS:
        return "abandoned"
    return st


def filter_by_status(
    threads, now_iso: str, selection: StatusSelection = DEFAULT_STATUS_SELECTION
) -> list[dict]:
    if selection.mode is not StatusMode.FILTERED:
        return list(threads)
    wanted = set(selection.statuses)
    return [t for t in threads if effective_status(t, now_iso) in wanted]


def filter_recent(threads, now_iso: str, days: int | None):
    if days is None:
        return threads
    return [t for t in threads if t.get("mtime") and age_days(t["mtime"], now_iso) <= days]


def group_key(thread: dict) -> str:
    # git identity groups worktrees/clones/monorepo-subdirs together; cwd is the fallback
    return thread.get("gitProjectKey") or thread.get("project") or "(unknown)"


def group_label(thread: dict) -> str:
    return thread.get("gitProjectLabel") or thread.get("project") or "(unknown)"


def tilde(path: str | None, home: str | None = None) -> str:
    """Abbreviate a leading home directory in a path to `~` for display."""
    if not path:
        return path or ""
    home = home if home is not None else os.path.expanduser("~")
    if home and path == home:
        return "~"
    if home and path.startswith(home + "/"):
        return "~" + path[len(home) :]
    return path


def compute_metrics(reg: dict, now_iso: str, *, include_snoozed: bool = True) -> dict:
    threads = reg["threads"]
    if not include_snoozed:
        threads = [t for t in threads if not snooze_active(t, now_iso)]
    eff = [(t, effective_status(t, now_iso)) for t in threads]
    by_status = Counter(es for _, es in eff)
    live = [t for t, es in eff if es in LIVE_STATUSES]
    by_project = Counter(group_key(t) for t in live)
    blocked = [t for t, es in eff if es == "blocked"]
    # kept sessions are deliberately long-lived: they stay in live/by_project, but must
    # not drive the nag metrics (stale/oldest) or they permanently skew them.
    nag = [t for t in live if not is_kept(t)]
    stale = [t for t in nag if age_days(t["mtime"], now_iso) > ABANDON_AGE_DAYS]
    oldest = max(nag, key=lambda t: age_days(t["mtime"], now_iso), default=None)
    return {
        "by_status": dict(by_status),
        "live_count": len(live),
        "spread_projects": len(by_project),
        "by_project": dict(by_project),
        "blocked_count": len(blocked),
        "stale_count": len(stale),
        "oldest_age_days": round(age_days(oldest["mtime"], now_iso), 1) if oldest else None,
        "unclassified": by_status.get("unclassified", 0),
        "kept_count": sum(1 for t in threads if is_kept(t)),
    }


def _label(thread: dict) -> str:
    return thread.get("title") or thread.get("suggestedName") or thread["sessionId"][:8]


def source_badge(thread: dict) -> str:
    s = thread.get("source") or "claude-code"
    return "claude" if s == "claude-code" else s


_BYTES_PER_UNIT = 1024


def _human_size(num_bytes) -> str:
    n = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < _BYTES_PER_UNIT or unit == "GB":
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= _BYTES_PER_UNIT


def _size_suffix(thread: dict) -> str:
    b = thread.get("sizeBytes")
    return f", {_human_size(b)}" if b else ""


def render(
    reg: dict,
    now_iso: str,
    selection: StatusSelection = DEFAULT_STATUS_SELECTION,
    cleaned: dict | None = None,
) -> str:
    scoped = dict(reg)
    scoped["threads"] = filter_by_status(reg["threads"], now_iso, selection)
    m = compute_metrics(scoped, now_iso)
    show_all = selection.mode is not StatusMode.DEFAULT
    lines = []
    lines.append("=== ringleader overview ===")
    lines.append(
        f"Live threads: {m['live_count']}  |  "
        f"spread across {m['spread_projects']} projects  |  "
        f"blocked: {m['blocked_count']}  |  stale(>21d): {m['stale_count']}"
    )
    if m["oldest_age_days"] is not None:
        lines.append(f"Oldest live thread: {m['oldest_age_days']} days untouched")
    if m["unclassified"]:
        lines.append(f"(! {m['unclassified']} unclassified — run session-sync)")
    lines.append("")

    groups: dict[str, list[tuple[dict, str]]] = defaultdict(list)
    labels: dict[str, str] = {}
    for t in scoped["threads"]:
        es = effective_status(t, now_iso)
        if show_all or es in LIVE_STATUSES:
            k = group_key(t)
            groups[k].append((t, es))
            labels.setdefault(k, group_label(t))

    for key in sorted(groups, key=lambda k: labels[k].lower()):
        head = f"## {tilde(labels[key])}"
        if cleaned and key in cleaned:
            head += f"  (cleaned {age_human(cleaned[key], now_iso)})"
        lines.append(head)
        items = sorted(groups[key], key=lambda pair: age_days(pair[0]["mtime"], now_iso))
        for t, es in items:
            age = round(age_days(t["mtime"], now_iso))
            flag = "  <- BLOCKED" if es == "blocked" else ""
            branch = f" @{t['gitBranch']}" if t.get("gitBranch") else ""
            lines.append(
                f"  [{es}] ({source_badge(t)}){branch} {_label(t)} ({age}d{_size_suffix(t)}) — "
                f"{t.get('whatItIs') or ''}{flag}"
            )
        lines.append("")

    breakdown = ", ".join(f"{k}:{v}" for k, v in sorted(m["by_status"].items()))
    lines.append(f"Sessions by status: {breakdown}")
    return "\n".join(lines)


def snooze_active(thread: dict, now_iso: str) -> bool:
    if not thread.get("snoozedAt"):
        return False
    until = thread.get("snoozedUntil")
    return until is None or now_iso < until


def triage_inbox(threads, now_iso: str) -> list[dict]:
    """Sessions awaiting a decision: not parked, not harvested, not kept, and not
    reviewed at their current state.

    The three ways a session leaves this inbox differ in what brings it back:
    snoozed returns when its wake date passes, reviewed returns when the file changes
    (reviewedAtMtime no longer matches mtime), and kept never returns until
    `rl session unkeep`. Archived/deleted sessions are already gone from threads."""
    out = []
    for t in threads:
        if snooze_active(t, now_iso):
            continue
        if t.get("decision") in ("harvested", "keep"):
            continue
        if t.get("reviewedAtMtime") is not None and t.get("reviewedAtMtime") == t.get("mtime"):
            continue
        out.append(t)
    return out


def triage_count(threads, now_iso: str) -> int:
    return len(triage_inbox(threads, now_iso))


def render_triage(threads, now_iso: str, *, unclassified_only: bool = False) -> str:
    inbox = triage_inbox(threads, now_iso)
    if unclassified_only:
        inbox = [t for t in inbox if t.get("status", "unclassified") == "unclassified"]
    if not inbox:
        return (
            "no unclassified sessions — every session has been triaged."
            if unclassified_only
            else "all triaged — nothing awaiting a decision."
        )
    inbox.sort(key=lambda t: age_days(t["mtime"], now_iso), reverse=True)
    header = (
        f"=== unclassified ({len(inbox)}) — never triaged ==="
        if unclassified_only
        else f"=== triage inbox ({len(inbox)}) — awaiting your decision ==="
    )
    lines = [header]
    groups: dict[str, list[dict]] = defaultdict(list)
    labels: dict[str, str] = {}
    for t in inbox:
        k = group_key(t)
        groups[k].append(t)
        labels.setdefault(k, group_label(t))
    for key in sorted(groups, key=lambda k: labels[k].lower()):
        lines.append(f"## {tilde(labels[key])}")
        for t in groups[key]:
            age = round(age_days(t["mtime"], now_iso))
            es = effective_status(t, now_iso)
            lines.append(
                f"  [{es}] ({source_badge(t)}) {_label(t)} ({age}d{_size_suffix(t)}) — "
                f"{t.get('whatItIs') or ''}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def registry_age_minutes(reg: dict, now_iso: str) -> float | None:
    ls = reg.get("lastScan")
    if not ls:
        return None
    return (_parse(now_iso) - _parse(ls)).total_seconds() / 60.0


def is_stale(reg: dict, now_iso: str, max_age_minutes: float = 60) -> bool:
    age = registry_age_minutes(reg, now_iso)
    return age is None or age > max_age_minutes


def newer_than(mtime_iso: str, ref_iso: str) -> bool:
    """True if mtime is strictly after ref (both ISO 8601)."""
    return _parse(mtime_iso) > _parse(ref_iso)


_MINUTES_PER_HOUR = 60
_MINUTES_PER_DAY = 1440


def age_human(iso: str | None, now_iso: str) -> str:
    if not iso:
        return "never"
    mins = (_parse(now_iso) - _parse(iso)).total_seconds() / 60.0
    if mins < 1:
        return "just now"
    if mins < _MINUTES_PER_HOUR:
        return f"{int(mins)}m ago"
    if mins < _MINUTES_PER_DAY:
        return f"{int(mins // 60)}h ago"
    return f"{int(mins // 1440)}d ago"


def registry_age_human(reg: dict, now_iso: str) -> str:
    return age_human(reg.get("lastScan"), now_iso)


def staleness_banner(reg: dict, now_iso: str, max_age_minutes: float = 60) -> str | None:
    """A one-line warning when the registry hasn't been scanned recently, or None
    when it's fresh. Read commands report from the registry, which only
    `rl session refresh` refreshes — so a stale registry silently under-reports counts."""
    if not is_stale(reg, now_iso, max_age_minutes):
        return None
    if registry_age_minutes(reg, now_iso) is None:
        return "(registry never scanned — run `rl session refresh` to populate it)"
    return (
        f"(registry last scanned {registry_age_human(reg, now_iso)} — counts may be "
        f"out of date; run `rl session refresh` to refresh)"
    )


def project_detail(
    reg: dict,
    now_iso: str,
    selection: StatusSelection = DEFAULT_STATUS_SELECTION,
) -> str:
    """A deep, single-repo report. `reg` should already be filtered to one project."""
    selected_threads = reg["threads"]
    if not selected_threads:
        return "(no sessions for that selector)"
    threads = filter_by_status(selected_threads, now_iso, selection)
    if selection.mode is StatusMode.FILTERED and not threads:
        return f"(no sessions matching --status {', '.join(selection.statuses)})"

    label = Counter(group_label(t) for t in threads).most_common(1)[0][0]
    checkouts = sorted({t["project"] for t in threads if t.get("project")})
    eff = [(t, effective_status(t, now_iso)) for t in threads]
    by_status = Counter(es for _, es in eff)

    show_all = selection.mode is not StatusMode.DEFAULT
    if show_all:
        interesting = list(eff)
    else:
        interesting = [(t, es) for t, es in eff if es in LIVE_STATUSES or es == "abandoned"]
    interesting.sort(key=lambda p: age_days(p[0]["mtime"], now_iso), reverse=True)

    blocked = by_status.get("blocked", 0)
    stale = sum(1 for t, es in interesting if age_days(t["mtime"], now_iso) > ABANDON_AGE_DAYS)
    dupes = {n: c for n, c in Counter(_label(t) for t, _ in eff).items() if n and c > 1}

    lines = [f"=== {tilde(label)} ==="]
    lines.append(f"checkouts ({len(checkouts)}):")
    lines += [f"  {tilde(c)}" for c in checkouts]
    lines.append("")
    total_bytes = sum(t.get("sizeBytes") or 0 for t in threads)
    lines.append(f"{len(threads)} sessions  |  total size: {_human_size(total_bytes)}")
    lines.append("by status: " + ", ".join(f"{k}:{v}" for k, v in sorted(by_status.items())))
    lines.append(
        f"blocked (waiting on you): {blocked}  |  "
        f"stale >21d: {stale}  |  duplicate names: {len(dupes)}"
    )
    if dupes:
        lines.append("  duplicates: " + ", ".join(f"{n} x{c}" for n, c in dupes.items()))
    lines.append("")

    if selection.mode is StatusMode.FILTERED:
        heading = "matching threads"
    else:
        heading = "all threads" if show_all else "live/abandoned threads"
    lines.append(f"{heading} ({len(interesting)}), oldest first:")
    for t, es in interesting:
        age = round(age_days(t["mtime"], now_iso))
        branch = f" @{t['gitBranch']}" if t.get("gitBranch") else ""
        flag = "  <- BLOCKED" if es == "blocked" else ""
        lines.append(
            f"  [{es}]{branch} {_label(t)} ({age}d{_size_suffix(t)}) — "
            f"{t.get('whatItIs') or ''}{flag}"
        )

    hidden = []
    if not show_all:
        if by_status.get("finished"):
            hidden.append(f"{by_status['finished']} finished")
        if by_status.get("harvested"):
            hidden.append(f"{by_status['harvested']} harvested")
        if by_status.get("unclassified"):
            hidden.append(f"{by_status['unclassified']} unclassified")
    if hidden:
        lines.append("")
        lines.append("(" + ", ".join(hidden) + " not shown)")
    return "\n".join(lines)


def render_rollup(
    reg,
    now_iso: str,
    *,
    selection: StatusSelection = DEFAULT_STATUS_SELECTION,
    checkouts=False,
) -> str:
    from . import projects  # noqa: PLC0415 -- projects imports overview at module level; circular

    scoped = dict(reg)
    scoped["threads"] = filter_by_status(reg["threads"], now_iso, selection)
    include_snoozed = selection.mode is not StatusMode.DEFAULT
    stats = projects.project_summary(scoped, now_iso, include_snoozed=include_snoozed)
    lines = [
        f"{'PROJECT':{PROJECT_W}} {'total':>5} {'class':>5} {'unnamed':>7} "
        f"{'chk':>4} {'cleaned':>9} {'mem':>9}"
    ]
    for s in stats:
        flags = (" ⚠" if s.needs_attention else "") + ("" if s.is_git else "  (no git)")
        if s.snoozed:
            flags += f"  💤{s.snoozed}"
        cleaned_h = age_human(s.cleaned_at, now_iso)
        # "never" only means something where memory exists to triage
        mem_h = (
            age_human(s.mem_cleaned_at, now_iso)
            if s.mem_cleaned_at
            else "never"
            if s.mem_entries
            else "—"
        )
        wrapped = _wrap(tilde(s.label or "(unknown)"))
        lines.append(
            f"{wrapped[0]:{PROJECT_W}} {s.total:5} {s.classified:5} "
            f"{s.unnamed:7} {len(s.checkouts):4} {cleaned_h:>9} {mem_h:>9}{flags}"
        )
        lines += wrapped[1:]
        if checkouts:
            lines += [f"    {tilde(path)}" for path in s.checkouts]
    attn = sum(1 for s in stats if s.needs_attention)
    snoozed_total = sum(1 for t in scoped["threads"] if snooze_active(t, now_iso))
    bits = []
    if attn:
        bits.append(f"{attn} need a look ⚠")
    if snoozed_total and not include_snoozed:
        bits.append(f"{snoozed_total} snoozed hidden — --status all 💤")
    kept_total = sum(1 for t in scoped["threads"] if is_kept(t))
    if kept_total:
        bits.append(f"{kept_total} kept — rl overview --kept 📌")
    suffix = f" ({'; '.join(bits)})" if bits else ""
    lines.append("")
    lines.append(f"{len(stats)} projects/dirs with sessions{suffix}")
    return "\n".join(lines)


def render_archived(reg, selector: str | None = None) -> str:
    rows = list(reg.get("archived", []))
    if selector:
        sel = selector.lower()
        rows = [
            ts
            for ts in rows
            if sel in (ts.get("project") or "").lower()
            or sel in (ts.get("archivePath") or "").lower()
        ]
    if not rows:
        return "no archived sessions"
    rows.sort(key=lambda ts: ts.get("archivedAt") or "")
    lines = []
    for ts in rows:
        date = (ts.get("archivedAt") or "")[:10]
        proj = ts.get("project") or "(unknown)"
        lines.append(f"{date}  {proj}  {ts['sessionId']}")
        lines.append(f"    {ts.get('summary') or '(no summary)'}")
        lines.append(f"    archive: {ts.get('archivePath')}")
        if ts.get("harvestPath"):
            lines.append(f"    harvested into: {ts['harvestPath']}")
    lines.append("")
    lines.append(f"{len(rows)} archived session(s)")
    return "\n".join(lines)


def render_snoozed(threads, now_iso: str) -> str:
    rows = [t for t in threads if snooze_active(t, now_iso)]
    if not rows:
        return "no snoozed sessions"
    rows.sort(key=lambda t: t.get("snoozedUntil") or "")
    lines = []
    for t in rows:
        name = t.get("title") or t.get("suggestedName") or (t.get("sessionId") or "")[:8]
        mtime = t.get("mtime")
        age = round(age_days(mtime, now_iso)) if mtime else 0
        until = t.get("snoozedUntil") or "no wake date"
        lines.append(f"{name} · {age}d · {until}")
    return "\n".join(lines)


def render_kept(threads, now_iso: str) -> str:
    rows = [t for t in threads if is_kept(t)]
    if not rows:
        return "no kept sessions"
    rows.sort(key=lambda t: t.get("keptAt") or "")
    lines = []
    for t in rows:
        name = t.get("title") or t.get("suggestedName") or (t.get("sessionId") or "")[:8]
        mtime = t.get("mtime")
        age = round(age_days(mtime, now_iso)) if mtime else 0
        project = t.get("gitProjectLabel") or t.get("project") or "—"
        what = t.get("whatItIs") or ""
        line = f"{name} · {project} · {age}d"
        lines.append(f"{line} · {what}" if what else line)
    return "\n".join(lines)
