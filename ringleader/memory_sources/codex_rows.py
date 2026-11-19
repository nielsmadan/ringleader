from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple


class RawEntry(NamedTuple):
    thread_id: str
    source_updated_at: int
    raw_memory: str
    rollout_slug: str | None


class Revision(NamedTuple):
    thread_id: str
    source_updated_at: int
    raw_memory: str
    rollout_summary: str
    rollout_slug: str | None
    generated_at: int

    @classmethod
    def decode(cls, values: Sequence[Any]) -> Revision:
        row = cls(*values)
        if (
            not isinstance(row.thread_id, str)
            or not row.thread_id
            or not isinstance(row.raw_memory, str)
            or not isinstance(row.rollout_summary, str)
            or type(row.source_updated_at) is not int
            or type(row.generated_at) is not int
            or (row.rollout_slug is not None and not isinstance(row.rollout_slug, str))
        ):
            raise ValueError("Unrecognized stage1 record fields")
        return row

    def raw_entry(self) -> RawEntry:
        return RawEntry(self.thread_id, self.source_updated_at, self.raw_memory, self.rollout_slug)


class StageRow(NamedTuple):
    thread_id: str
    source_updated_at: int
    raw_memory: str
    rollout_summary: str
    rollout_slug: str | None
    generated_at: int
    usage_count: int | None
    last_usage: int | None
    selected_for_phase2: int
    selected_for_phase2_source_updated_at: int | None

    @classmethod
    def decode(cls, values: Sequence[Any]) -> StageRow:
        if len(values) != len(cls._fields):
            raise ValueError("Unrecognized native Codex recovery row")
        row = cls(*values)
        row.revision()
        if row.selected_for_phase2 not in (0, 1) or any(
            val is not None and type(val) is not int
            for val in (
                row.usage_count,
                row.last_usage,
                row.selected_for_phase2,
                row.selected_for_phase2_source_updated_at,
            )
        ):
            raise ValueError("Unrecognized native selection or usage counters")
        return row

    def revision(self) -> Revision:
        return Revision.decode(
            (
                self.thread_id,
                self.source_updated_at,
                self.raw_memory,
                self.rollout_summary,
                self.rollout_slug,
                self.generated_at,
            )
        )
