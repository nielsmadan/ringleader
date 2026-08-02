from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import UTC, datetime

LIVE_STATUSES = ("active", "unfinished", "blocked")
ABANDON_AGE_DAYS = 21

PROJECT_W = 30


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


def compute_metrics(reg: dict, now_iso: str) -> dict:
    eff = [(t, effective_status(t, now_iso)) for t in reg["threads"]]
    by_status = Counter(es for _, es in eff)
    live = [t for t, es in eff if es in LIVE_STATUSES]
    by_project = Counter(group_key(t) for t in live)
    blocked = [t for t, es in eff if es == "blocked"]
    stale = [t for t in live if age_days(t["mtime"], now_iso) > ABANDON_AGE_DAYS]
    oldest = max(live, key=lambda t: age_days(t["mtime"], now_iso), default=None)
    return {
        "by_status": dict(by_status),
        "live_count": len(live),
        "spread_projects": len(by_project),
        "by_project": dict(by_project),
        "blocked_count": len(blocked),
        "stale_count": len(stale),
        "oldest_age_days": round(age_days(oldest["mtime"], now_iso), 1) if oldest else None,
        "unclassified": by_status.get("unclassified", 0),
    }


def _label(thread: dict) -> str:
    return thread.get("title") or thread.get("suggestedName") or thread["sessionId"][:8]


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


def render(reg: dict, now_iso: str, show_all: bool = False, cleaned: dict | None = None) -> str:
    m = compute_metrics(reg, now_iso)
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
    if show_all:
        lines.append("(--all: listing every status, not just live)")
    lines.append("")

    # group threads by git project (fallback: cwd); live-only unless --all
    groups: dict[str, list[tuple[dict, str]]] = defaultdict(list)
    labels: dict[str, str] = {}
    for t in reg["threads"]:
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
                f"  [{es}]{branch} {_label(t)} ({age}d{_size_suffix(t)}) — "
                f"{t.get('whatItIs') or ''}{flag}"
            )
        lines.append("")

    # status breakdown footer
    breakdown = ", ".join(f"{k}:{v}" for k, v in sorted(m["by_status"].items()))
    lines.append(f"All sessions by status: {breakdown}")
    return "\n".join(lines)


def triage_inbox(threads, now_iso: str) -> list[dict]:
    """Sessions awaiting a decision: not parked, not harvested, and not reviewed at
    their current state. A session re-enters once its file changes (reviewedAtMtime
    no longer matches mtime). Archived/deleted sessions are already gone from threads."""
    out = []
    for t in threads:
        if t.get("snoozedAt"):
            continue
        if t.get("decision") == "harvested":
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
                f"  [{es}] {_label(t)} ({age}d{_size_suffix(t)}) — {t.get('whatItIs') or ''}"
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
    `rl sync scan` refreshes — so a stale registry silently under-reports counts."""
    if not is_stale(reg, now_iso, max_age_minutes):
        return None
    if registry_age_minutes(reg, now_iso) is None:
        return "(registry never scanned — run `rl sync scan` to populate it)"
    return (
        f"(registry last scanned {registry_age_human(reg, now_iso)} — counts may be "
        f"out of date; run `rl sync scan` to refresh)"
    )


def project_detail(reg: dict, now_iso: str, show_all: bool = False) -> str:
    """A deep, single-repo report. `reg` should already be filtered to one project."""
    threads = reg["threads"]
    if not threads:
        return "(no sessions for that selector)"

    label = Counter(group_label(t) for t in threads).most_common(1)[0][0]
    checkouts = sorted({t["project"] for t in threads if t.get("project")})
    eff = [(t, effective_status(t, now_iso)) for t in threads]
    by_status = Counter(es for _, es in eff)

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


def render_rollup(reg, now_iso: str, *, include_snoozed=False, checkouts=False) -> str:
    from . import projects  # noqa: PLC0415 -- projects imports overview at module level; circular

    stats = projects.project_summary(reg, include_snoozed=include_snoozed)
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
    snoozed_total = sum(1 for t in reg["threads"] if t.get("snoozedAt"))
    bits = []
    if attn:
        bits.append(f"{attn} need a look ⚠")
    if snoozed_total and not include_snoozed:
        bits.append(f"{snoozed_total} snoozed hidden — -a 💤")
    suffix = f" ({'; '.join(bits)})" if bits else ""
    lines.append("")
    lines.append(f"{len(stats)} projects/dirs with sessions{suffix}")
    return "\n".join(lines)


def render_archived(reg, selector: str | None = None) -> str:
    rows = [ts for ts in reg.get("tombstones", []) if ts.get("reason") == "archived"]
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
    rows.sort(key=lambda ts: ts.get("deletedAt") or "")
    lines = []
    for ts in rows:
        date = (ts.get("deletedAt") or "")[:10]
        proj = ts.get("project") or "(unknown)"
        lines.append(f"{date}  {proj}  {ts['sessionId']}")
        lines.append(f"    {ts.get('summary') or '(no summary)'}")
        lines.append(f"    archive: {ts.get('archivePath')}")
        if ts.get("harvestPath"):
            lines.append(f"    harvested into: {ts['harvestPath']}")
    lines.append("")
    lines.append(f"{len(rows)} archived session(s)")
    return "\n".join(lines)
