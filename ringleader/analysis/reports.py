from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from .. import paths, stateio

type ReportKind = Literal["findings", "recommendations"]
REPORT_KINDS: tuple[ReportKind, ...] = ("findings", "recommendations")
SLUG_CHARS = 40
_FENCE = "---"
_KEY_LINE = re.compile(r"^[A-Za-z_][\w-]*\s*:")


@dataclass(frozen=True)
class ReportMeta:
    report_id: str
    kind: ReportKind
    created: str
    title: str
    window: str | None
    based_on: tuple[str, ...]
    path: Path


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:SLUG_CHARS].strip("-")


def report_filename(kind: ReportKind, created: datetime, title: str, attempt: int) -> str:
    suffix = "" if attempt <= 1 else f"-{attempt}"
    parts = (f"{created:%Y-%m-%dT%H%M}", kind, _slug(title) or kind)
    return "-".join(parts) + f"{suffix}.md"


def claim_report_path(directory: Path, kind: ReportKind, created: datetime, title: str) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    attempt = 1
    while True:
        path = directory / report_filename(kind, created, title, attempt)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            attempt += 1
            continue
        os.close(fd)
        return path


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.split("\n")
    if lines[0].strip() != _FENCE:
        return {}, text
    close = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == _FENCE), None)
    if close is None or not all(_KEY_LINE.match(line) for line in lines[1:close] if line.strip()):
        return {}, text
    meta = {
        key.strip(): value.strip()
        for key, _, value in (line.partition(":") for line in lines[1:close])
        if key.strip()
    }
    return meta, "\n".join(lines[close + 1 :])


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _heading(body: str) -> str | None:
    return next(
        (
            line[2:].strip()
            for line in body.split("\n")
            if line.startswith("# ") and line[2:].strip()
        ),
        None,
    )


def _meta_of(path: Path, kind: ReportKind) -> ReportMeta | None:
    try:
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        datetime.fromisoformat(meta["created"])
    except (OSError, UnicodeDecodeError, KeyError, ValueError):
        return None
    return ReportMeta(
        report_id=meta.get("id", path.stem),
        kind=kind,
        created=meta["created"],
        title=meta.get("title", kind),
        window=meta.get("window") or None,
        based_on=tuple(
            part.strip() for part in meta.get("based_on", "").split(",") if part.strip()
        ),
        path=path,
    )


def list_reports(kind: ReportKind | None, limit: int | None) -> list[ReportMeta]:
    kinds = REPORT_KINDS if kind is None else (kind,)
    found = [
        meta
        for k in kinds
        for path in sorted(paths.analysis_reports_dir(k).glob("*.md"))
        if (meta := _meta_of(path, k)) is not None
    ]
    found.sort(key=lambda m: (datetime.fromisoformat(m.created), m.report_id), reverse=True)
    return found if limit is None else found[:limit]


def _document(meta: ReportMeta, body: str) -> str:
    lines = [
        _FENCE,
        f"kind: {meta.kind}",
        f"id: {meta.report_id}",
        f"created: {meta.created}",
        f"title: {_one_line(meta.title)}",
        *([f"window: {_one_line(meta.window)}"] if meta.window else []),
        *([f"based_on: {', '.join(meta.based_on)}"] if meta.based_on else []),
        _FENCE,
        "",
    ]
    return "\n".join(lines) + body.strip("\n") + "\n"


def save_report(
    kind: ReportKind,
    body: str,
    now: datetime,
    *,
    title: str | None,
    window: str | None,
    based_on: Sequence[str],
) -> ReportMeta:
    _, text = parse_frontmatter(body)
    if not text.strip():
        raise ValueError("report body is empty")
    known = {m.report_id for m in list_reports(None, None)}
    unknown = [r for r in based_on if r not in known]
    if unknown:
        raise ValueError(
            f"unknown report id: {', '.join(unknown)}; list the ids with rl analyze reports"
        )
    chosen = _one_line(title or "") or _heading(text) or kind
    path = claim_report_path(paths.analysis_reports_dir(kind), kind, now, chosen)
    meta = ReportMeta(
        report_id=path.stem,
        kind=kind,
        created=now.isoformat(timespec="seconds"),
        title=chosen,
        window=window,
        based_on=tuple(based_on),
        path=path,
    )
    try:
        stateio.atomic_write_text(path, _document(meta, text))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return meta
