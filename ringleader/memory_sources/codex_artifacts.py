from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from uuid import UUID

from .base import NativePaths
from .codex_rows import RawEntry, StageRow
from .transactions import FileImage, file_image

HEADER = "# Raw Memories\n\nMerged stage-1 raw memories (stable ascending thread-id order):\n\n"
EMPTY = "# Raw Memories\n\nNo raw memories yet.\n"
THREAD = re.compile(r"## Thread `([0-9a-f-]{36})`\n")
UUID_UNIX_TIME = 7
ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def stem(key: str, updated: int, slug: str | None) -> str:
    uid = UUID(key)
    if str(uid) != key:
        raise ValueError("Unrecognized native Codex thread ID")
    timestamp = updated
    if uid.version == UUID_UNIX_TIME:
        timestamp = (uid.int >> 80) // 1000
    elif uid.version in (1, 6):
        timestamp = (uid.time - 0x01B21DD213814000) // 10_000_000
    stamp = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H-%M-%S")
    value = uid.int & 0xFFFFFFFF
    suffix = "".join(ALPHABET[(value // 62**pos) % 62] for pos in (3, 2, 1, 0))
    label = re.sub(r"[^a-zA-Z0-9]", "_", (slug or "")[:60]).lower().rstrip("_")
    return f"{stamp}-{suffix}" + (f"-{label}" if label else "")


def _timestamp(updated: int) -> str:
    return datetime.fromtimestamp(updated, UTC).isoformat(timespec="seconds")


_SUMMARY_METADATA = re.compile(
    r"rollout_path: [^\r\n]+\ncwd: [^\r\n]+\n(?:git_branch: [^\r\n]*\n)?\n"
)
_RAW_METADATA = re.compile(r"cwd: [^\r\n]+\nrollout_path: [^\r\n]+\n")


def validate_summary(text: str | None, row: StageRow) -> None:
    if text is not None:
        prefix = f"thread_id: {row.thread_id}\nupdated_at: {_timestamp(row.source_updated_at)}\n"
        metadata = _SUMMARY_METADATA.match(text, len(prefix))
        if (
            not text.startswith(prefix)
            or not metadata
            or not text.endswith("\n")
            or len(text) != metadata.end() + len(row.rollout_summary) + 1
            or not text.startswith(row.rollout_summary, metadata.end())
        ):
            raise ValueError("Unrecognized or changed canonical rollout summary")


def summary_image(root: Path, row: StageRow, paths: NativePaths) -> tuple[Path, FileImage]:
    folder = root / "rollout_summaries"
    paths.check(folder)
    file = folder / (stem(row.thread_id, row.source_updated_at, row.rollout_slug) + ".md")
    image = file_image(file, paths)
    validate_summary(image.text, row)
    verify_summary_names(folder, file, row.thread_id, paths)
    return file, image


def verify_summary_names(folder: Path, selected: Path, key: str, paths: NativePaths) -> None:
    if folder.exists():
        for other in folder.iterdir():
            if other != selected and other.suffix == ".md":
                with paths.open_file(other) as stream:
                    first = stream.readline(128)
                if not re.fullmatch(rb"thread_id: [0-9a-f-]{36}\n", first):
                    raise ValueError("Unrecognized rollout summary metadata")
                if first == f"thread_id: {key}\n".encode():
                    raise ValueError("A different rollout summary names the selected thread")


def _headers(
    text: str, db: sqlite3.Connection, selected: StageRow | None
) -> Iterator[tuple[re.Match[str], RawEntry | None]]:
    for batch in batched(THREAD.finditer(text, len(HEADER)), 32, strict=False):
        keys = {match[1] for match in batch if not selected or match[1] != selected.thread_id}
        entries = {}
        if keys:
            params = ",".join("?" for _ in keys)
            entries = {
                row[0]: RawEntry(*row)
                for row in db.execute(
                    f"SELECT thread_id,source_updated_at,raw_memory,rollout_slug FROM stage1_outputs WHERE thread_id IN ({params})",
                    tuple(keys),
                )
            }
        if selected:
            entries[selected.thread_id] = selected.revision().raw_entry()
        for match in batch:
            yield match, entries.get(match[1])


def raw_section(
    text: str | None, row: StageRow | None, db: sqlite3.Connection, *, insert: str = ""
) -> tuple[str, str | None]:
    if text is None or text == EMPTY:
        return "", HEADER + insert if insert else text
    if not text.startswith(HEADER):
        raise ValueError("Unrecognized raw memories header")
    offset, selected = len(HEADER), ""
    others = []
    previous = ""
    for match, entry in _headers(text, db, row):
        if match.start() < offset:
            continue
        key = match[1]
        if match.start() != offset or key <= previous:
            raise ValueError("Unrecognized, unordered or duplicate raw memory section")
        previous = key
        if entry is None:
            raise ValueError("Cannot verify raw memory section against its native row")
        stamp = f"updated_at: {_timestamp(entry.source_updated_at)}\n"
        meta = _RAW_METADATA.match(text, match.end() + len(stamp))
        filename = (
            "rollout_summary_file: "
            + stem(key, entry.source_updated_at, entry.rollout_slug)
            + ".md\n\n"
        )
        body = entry.raw_memory.strip() + "\n\n"
        if (
            not text.startswith(stamp, match.end())
            or not meta
            or not text.startswith(filename, meta.end())
            or not text.startswith(body, meta.end() + len(filename))
        ):
            raise ValueError("Raw memory section changed or uses an unfamiliar layout")
        end = meta.end() + len(filename) + len(body)
        section = text[offset:end]
        if row and key == row.thread_id:
            selected = section
        else:
            if insert and row and key > row.thread_id:
                others.append(insert)
                insert = ""
            others.append(section)
        offset = end
    if offset != len(text):
        raise ValueError("Unrecognized raw memory section")
    if insert:
        others.append(insert)
    return selected, HEADER + "".join(others) if others else EMPTY
