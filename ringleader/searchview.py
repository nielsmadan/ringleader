from __future__ import annotations

from . import namefilter, overview
from .search import Match

_BOLD = "\033[1m"
_RESET = "\033[0m"
_SNIPPET_W = 60

_FIELD_LABELS = {
    "aiTitle": "ai title",
    "whatItIs": "what it is",
    "summary": "summary",
    "lastPrompt": "last prompt",
}


def _hl(value: str, span: tuple[int, int], color: bool) -> str:
    if not color:
        return value
    start, end = span
    return value[:start] + _BOLD + value[start:end] + _RESET + value[end:]


def _snippet(value: str, span: tuple[int, int]) -> tuple[str, tuple[int, int]]:
    start, end = span
    if len(value) <= _SNIPPET_W:
        return value, span
    pad = max(0, (_SNIPPET_W - (end - start)) // 2)
    left = min(max(0, start - pad), len(value) - _SNIPPET_W)
    right = left + _SNIPPET_W
    prefix = "…" if left > 0 else ""
    text = prefix + value[left:right] + ("…" if right < len(value) else "")
    shift = len(prefix) - left
    # an ellipsis is not part of the match, so the span must stop at the body
    body_end = len(prefix) + (right - left)
    return text, (min(start + shift, body_end), min(end + shift, body_end))


def _row(m: Match, now_iso: str, color: bool) -> list[str]:
    thread = m.thread
    name = m.value if m.matched_on == "name" else namefilter.display_name(thread)
    shown = _hl(m.value, m.span, color) if m.matched_on == "name" else name or "(unnamed)"
    parts = [shown]
    if thread.get("source"):
        parts.append(overview.source_badge(thread))
    mtime = thread.get("mtime")
    if mtime:
        parts.append(f"{round(overview.age_days(mtime, now_iso))}d")
    parts.append("[archived]" if m.archived else overview.effective_status(thread, now_iso))
    parts.append(overview.tilde(thread.get("project")) or "(unknown)")
    if m.filtered:
        parts.append("[filtered]")
    lines = [" · ".join(parts)]

    session_id = thread.get("sessionId") or ""
    if m.matched_on == "id":
        lines.append("    " + _hl(m.value, m.span, color))
    else:
        lines.append("    " + session_id)
    if m.matched_on not in ("name", "id"):
        text, span = _snippet(m.value, m.span)
        label = _FIELD_LABELS.get(m.matched_on, m.matched_on)
        lines.append(f"    ↳ {label}: {_hl(text, span, color)}")
    return lines


def render_search(
    matches: list[Match],
    now_iso: str,
    query: str,
    *,
    color: bool = False,
    limit: int | None = None,
) -> str:
    if not matches:
        return f'no sessions matching "{query}"'
    noun = "match" if len(matches) == 1 else "matches"
    lines = [f'=== search "{query}" — {len(matches)} {noun} ===']
    shown = matches if limit is None else matches[:limit]
    for m in shown:
        lines += _row(m, now_iso, color)
    hidden = len(matches) - len(shown)
    if hidden:
        lines.append(f"({hidden} more — raise --limit to see them)")
    return "\n".join(lines)
