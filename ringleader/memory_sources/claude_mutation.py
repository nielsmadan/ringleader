from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from ..memory import INDEX_NAME
from ..memory_types import (
    MemoryRef,
    MemorySnapshot,
    NativeIntent,
    NativeMemoryTransaction,
    NativeRemoval,
    Progress,
)
from . import claude_code as native
from .base import file_identity
from .claude_index import IndexLine, index_lines, inline_content
from .transactions import (
    FILE_PERMISSIONS,
    FileChange,
    FileImage,
    apply_file,
    file_image,
    pack,
    read_text,
    unpack,
)


def _split(index: str, name: str) -> tuple[str, str]:
    selected: list[str] = []
    retained: list[IndexLine] = []
    retained_numbers: list[int] = []
    bom = "\ufeff" if index.startswith("\ufeff") else ""
    lines = index_lines(index.removeprefix("\ufeff"))
    for number, line in enumerate(lines):
        take = line.topic is None if name == INDEX_NAME else line.topic == name
        if (
            name != INDEX_NAME
            and not take
            and not line.literal
            and name.casefold() in unquote(line.source).casefold()
            and ("]" in line.source or "<" in line.source)
        ):
            raise ValueError("Index link to the selected topic cannot be separated exactly")
        if take:
            selected.append(line.source)
        else:
            retained.append(line)
            retained_numbers.append(number)
    if selected and any("html_block" in line.block for line in lines):
        raise ValueError("Index links in an ambiguous Markdown context cannot be separated exactly")
    remaining = "".join(line.source for line in retained)
    if selected and index_lines(remaining, original_lines=retained_numbers) != retained:
        raise ValueError("Removing this index entry would reinterpret retained Markdown")
    return "".join(selected), bom + remaining


def _remove_index(index: str | None, name: str) -> str | None:
    if index is None:
        return None
    if name == INDEX_NAME:
        content, block = inline_content(index)
        if block or not content:
            raise ValueError(block or "Inline memory disappeared")
    return _split(index, name)[1]


def _restore_index(old: str | None, current: str | None, name: str) -> str | None:
    if old is None:
        return current
    selected, kept = _split(old, name)
    if not selected:
        return current
    if current == kept:
        return old
    if current is None and kept.strip():
        raise ValueError("Original index disappeared after removal")
    if name == INDEX_NAME:
        if inline_content(current or "")[0]:
            raise ValueError("New inline memory would be overwritten")
    elif _split(current or "", name)[0]:
        raise ValueError("A link to the restored topic already exists")
    text = current or ""
    restored = text + ("\n" if text and not text.endswith("\n") else "") + selected
    if _split(restored, name)[0] != selected:
        raise ValueError("Restored index content cannot be separated exactly")
    return restored


class ClaudeTransaction:
    def __init__(self, source: native.ClaudeMemorySource, ref: MemoryRef, stack: ExitStack) -> None:
        self.source = source
        self.ref = ref
        self.note = Path(ref.source_ref)
        self.index = self.note.parent / INDEX_NAME
        self.stack = stack
        self.fresh = False

    def capture(self) -> MemorySnapshot:
        stream = self.stack.enter_context(self.source.paths.open_file(self.note))
        body = read_text(stream)
        info = os.fstat(stream.fileno())
        identity = file_identity(info)
        if (
            hashlib.sha256(body.encode()).hexdigest() != self.ref.fingerprint
            or identity != self.ref.incarnation
        ):
            raise ValueError("Claude memory changed since review")
        self.fresh = True
        index = file_image(self.index, self.source.paths)
        if self.note != self.index and index.identity == identity:
            raise ValueError("Selected topic aliases the Claude index")
        _remove_index(index.text, self.note.name)
        return MemorySnapshot(
            self.ref,
            pack(
                ref=asdict(self.ref),
                body=body,
                mode=stat.S_IMODE(info.st_mode) & FILE_PERMISSIONS,
                index=asdict(index),
            ),
        )

    def prepare(
        self, snapshot: MemorySnapshot, *, action: Literal["remove", "restore"], now: datetime
    ) -> NativeIntent:
        data = unpack(snapshot.payload)
        if (
            snapshot.ref != self.ref
            or data["ref"] != asdict(self.ref)
            or hashlib.sha256(data["body"].encode()).hexdigest() != self.ref.fingerprint
        ):
            raise ValueError("Mismatched Claude memory recovery snapshot")
        current = file_image(self.note, self.source.paths)
        index = file_image(self.index, self.source.paths)
        original = FileImage(**data["index"])
        if self.note != self.index and original.identity == self.ref.incarnation:
            raise ValueError("Archived topic aliases the Claude index")
        if action == "remove":
            if (
                current.text != data["body"]
                or current.identity != self.ref.incarnation
                or current.mode != data.get("mode", current.mode)
                or index != original
            ):
                raise ValueError("Claude note or index changed since capture")
            target = _remove_index(index.text, self.note.name)
        else:
            if self.note != self.index and current.text is not None:
                raise ValueError("Restored Claude topic would overwrite an existing file")
            target = _restore_index(original.text, index.text, self.note.name)
        changes = {}
        if self.note != self.index:
            changes["topic"] = FileChange.create(
                current,
                None if action == "remove" else data["body"],
                mode=data.get("mode") if action == "restore" else None,
            ).data()
        changes["index"] = FileChange.create(index, target).data()
        return NativeIntent(
            action,
            pack(
                ref=asdict(self.ref),
                action=action,
                changes=changes,
                originalIndex=original.text if action == "restore" else None,
            ),
        )

    def apply(self, intent: NativeIntent, *, now: datetime, progress: Progress) -> NativeRemoval:
        data = unpack(intent.payload)
        if (
            data["ref"] != asdict(self.ref)
            or data["action"] != intent.action
            or set(data["changes"])
            != ({"index"} if self.note == self.index else {"topic", "index"})
        ):
            raise ValueError("Invalid Claude mutation intent")
        changes = {key: FileChange.parse(entry) for key, entry in data["changes"].items()}
        if self.note != self.index and (
            native._same_native_path(self.note, self.index, self.source.paths)
            or changes["index"].before.identity == self.ref.incarnation
        ):
            raise ValueError("Selected topic aliases the Claude index")
        for key, change in changes.items():
            path = self.note if key == "topic" else self.index
            if key == "topic" and (
                path == self.index or (change.after is None) != (intent.action == "remove")
            ):
                raise ValueError("Invalid topic change")
            if key == "topic" or (self.note == self.index and intent.action == "remove"):
                body = change.before.text if intent.action == "remove" else change.after
                if (
                    body is None
                    or hashlib.sha256(body.encode()).hexdigest() != self.ref.fingerprint
                ):
                    raise ValueError("Invalid reviewed Claude content")
                if intent.action == "remove" and change.before.identity != self.ref.incarnation:
                    raise ValueError("Invalid reviewed Claude file identity")
            if (
                key == "index"
                and intent.action == "remove"
                and _remove_index(change.before.text, self.note.name) != change.after
            ):
                raise ValueError("Invalid index removal")
            if key == "index" and intent.action == "restore":
                original = data["originalIndex"]
                if self.note == self.index and (
                    not isinstance(original, str)
                    or hashlib.sha256(original.encode()).hexdigest() != self.ref.fingerprint
                ):
                    raise ValueError("Restored inline note differs from the reviewed revision")
                if _restore_index(original, change.before.text, self.note.name) != change.after:
                    raise ValueError("Invalid index restoration")
        for key, change in changes.items():
            path = self.note if key == "topic" else self.index
            apply_file(
                path,
                self.source.paths,
                change,
                key,
                progress=progress,
                weak_guard=not self.fresh and path == self.note and intent.action == "remove",
            )
        return NativeRemoval("not-needed", None)


@contextmanager
def mutation(
    source: native.ClaudeMemorySource, ref: MemoryRef
) -> Iterator[NativeMemoryTransaction]:
    source.selected_store(ref)
    with ExitStack() as stack:
        yield ClaudeTransaction(source, ref, stack)
