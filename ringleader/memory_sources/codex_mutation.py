from __future__ import annotations

import fcntl
import os
import sqlite3
import stat
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ..memory_types import (
    MemoryRef,
    MemorySnapshot,
    NativeIntent,
    NativeMemoryTransaction,
    NativeRemoval,
    Progress,
)
from . import codex as native
from . import codex_artifacts as artifacts
from .base import file_identity, memory_id
from .codex_rows import StageRow
from .transactions import (
    FileChange,
    NativeBusy,
    apply_file,
    directory,
    file_image,
    pack,
    unpack,
)

STAGE = "memory_stage1"
GLOBAL = "memory_consolidate_global"
MAX_WATERMARK = (1 << 63) - 1
JOB_FIELDS = (
    "kind",
    "job_key",
    "status",
    "worker_id",
    "ownership_token",
    "started_at",
    "finished_at",
    "lease_until",
    "retry_at",
    "retry_remaining",
    "last_error",
    "input_watermark",
    "last_success_watermark",
)


def installed_version() -> str:
    result = subprocess.run(
        ["codex", "--version"], capture_output=True, text=True, timeout=3, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _roots(source: native.CodexMemorySource, ref: MemoryRef) -> tuple[Path, Path]:
    path = Path(ref.store_id)
    version = next(
        (
            v
            for v, name in native.FILES.items()
            if source.sqlite_home and path == source.sqlite_home / name
        ),
        None,
    )
    if (
        version is None
        or ref.source != source.name
        or ref.memory_id != memory_id(source.name, str(path), ref.source_ref)
    ):
        raise ValueError("Unrecognized Codex memory store or record")
    source.verify_workspace(ref)
    if not source.installed or (source.version_probe or installed_version)() != "codex-cli 0.167.1":
        raise ValueError("Native memory mutation requires codex-cli 0.167.1")
    root = source.workspace(version)
    source.paths.check(root)
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = path.with_name(path.name + suffix)
        source.paths.check(candidate)
        if candidate.exists() and (
            not stat.S_ISREG(candidate.lstat().st_mode) or candidate.lstat().st_nlink != 1
        ):
            raise ValueError("Unsafe native SQLite file or sidecar")
    return path, root


def _row(db: sqlite3.Connection, key: str) -> StageRow | None:
    row = db.execute("SELECT * FROM stage1_outputs WHERE thread_id=?", (key,)).fetchone()
    return StageRow.decode(row) if row else None


def _job(db: sqlite3.Connection, kind: str, key: str) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM jobs WHERE kind=? AND job_key=?", (kind, key)).fetchone()
    if row is None:
        return None
    job = dict(zip(JOB_FIELDS, row, strict=True))
    if any(
        job[field] is not None and type(job[field]) is not int
        for field in (
            "started_at",
            "finished_at",
            "lease_until",
            "retry_at",
            "retry_remaining",
            "input_watermark",
            "last_success_watermark",
        )
    ):
        raise ValueError("Invalid native memory job counters")
    if job["status"] not in ("done", "pending", "running", "error"):
        raise ValueError("Unrecognized native memory job state")
    return job


def _new_job(kind: str, key: str) -> dict[str, Any]:
    return dict.fromkeys(JOB_FIELDS) | {
        "kind": kind,
        "job_key": key,
        "retry_remaining": 3,
        "last_success_watermark": 0,
    }


def _jobs(
    db: sqlite3.Connection, row: StageRow, stamp: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    extraction = _job(db, STAGE, row.thread_id)
    global_job = _job(db, GLOBAL, "global")
    running = db.execute(
        "SELECT 1 FROM jobs WHERE kind IN (?, ?) AND status='running' AND lease_until > ? LIMIT 1",
        (STAGE, GLOBAL, stamp),
    ).fetchone()
    if running:
        raise NativeBusy("A native extraction or consolidation job is running")
    if extraction and (extraction["input_watermark"] or 0) > max(
        row.source_updated_at, extraction["last_success_watermark"] or 0
    ):
        raise NativeBusy("A newer source revision is awaiting extraction")
    return extraction, global_job


def _write_job(db: sqlite3.Connection, job: dict[str, Any]) -> None:
    fields = ",".join(JOB_FIELDS)
    placeholders = ",".join("?" for _ in JOB_FIELDS)
    update = ",".join(f"{name}=excluded.{name}" for name in JOB_FIELDS[2:])
    db.execute(
        f"INSERT INTO jobs ({fields}) VALUES ({placeholders}) ON CONFLICT(kind,job_key) DO UPDATE SET {update}",
        tuple(job[name] for name in JOB_FIELDS),
    )


class CodexTransaction:
    def __init__(
        self,
        source: native.CodexMemorySource,
        ref: MemoryRef,
        db: sqlite3.Connection,
        root: Path,
        identity: str,
    ) -> None:
        self.source, self.ref, self.db, self.root, self.identity = source, ref, db, root, identity

    def _check_row(self, row: StageRow) -> None:
        workspace = self.source.workspace_id("v1" if self.root.name == "memories" else "v2")
        observed = native._record(
            row.revision(), Path(self.ref.store_id), self.identity, workspace, None
        )
        if observed.ref != self.ref:
            raise ValueError("Native Codex memory changed since review")

    def capture(self) -> MemorySnapshot:
        row = _row(self.db, self.ref.source_ref)
        if row is None:
            raise ValueError("Native memory no longer exists")
        self._check_row(row)
        _, summary = artifacts.summary_image(self.root, row, self.source.paths)
        section = ""
        if self.root.name == "memories":
            raw = file_image(self.root / "raw_memories.md", self.source.paths).text
            section, _ = artifacts.raw_section(raw, row, self.db)
        return MemorySnapshot(
            self.ref, pack(ref=asdict(self.ref), row=row, summary=summary.text, section=section)
        )

    def prepare(
        self, snapshot: MemorySnapshot, *, action: Literal["remove", "restore"], now: datetime
    ) -> NativeIntent:
        data = unpack(snapshot.payload)
        if snapshot.ref != self.ref or data["ref"] != asdict(self.ref):
            raise ValueError("Mismatched native memory snapshot")
        row = StageRow.decode(data["row"])
        self._check_row(row)
        current = _row(self.db, self.ref.source_ref)
        if action == "remove":
            if current is None or current.revision() != row.revision():
                raise ValueError("Selected Codex record changed")
            row = current
        elif current is not None:
            raise ValueError("A Codex memory already exists for this thread")
        stamp = int(now.timestamp())
        stage, phase2 = _jobs(self.db, row, stamp)
        target = max(
            stamp,
            row.source_updated_at,
            ((phase2 or {}).get("input_watermark") or 0) + 1,
            ((phase2 or {}).get("last_success_watermark") or 0) + 1,
        )
        if target > MAX_WATERMARK:
            raise ValueError("Native consolidation watermark cannot be incremented")
        changes = {}
        _, summary = artifacts.summary_image(self.root, row, self.source.paths)
        if action == "remove" and summary.text != data["summary"]:
            raise ValueError("Canonical summary changed since capture")
        if action == "restore" and summary.text is not None:
            raise ValueError("Restoration would overwrite an existing rollout summary")
        changes["summary"] = FileChange.create(
            summary, data["summary"] if action == "restore" else None
        ).data()
        if self.root.name == "memories":
            raw = file_image(self.root / "raw_memories.md", self.source.paths)
            selected, after = artifacts.raw_section(
                raw.text, row, self.db, insert=data["section"] if action == "restore" else ""
            )
            if action == "remove" and selected != data["section"]:
                raise ValueError("Selected raw memory changed since capture")
            if action == "restore" and selected:
                raise ValueError("Raw memory already contains the selected thread")
            changes["raw"] = FileChange.create(raw, after).data()
        return NativeIntent(
            action,
            pack(
                ref=asdict(self.ref),
                action=action,
                row=row,
                stage=stage,
                phase2=phase2,
                target=target,
                stamp=stamp,
                changes=changes,
            ),
        )

    def _validate_artifact(
        self, key: str, change: FileChange, row: StageRow, *, remove: bool
    ) -> None:
        present, absent = (
            (change.before.text, change.after) if remove else (change.after, change.before.text)
        )
        if key == "summary":
            if absent is not None:
                raise ValueError("Invalid summary change")
            artifacts.validate_summary(present, row)
        else:
            section, retained = artifacts.raw_section(present, row, self.db)
            if retained != absent and not (
                not remove and absent is None and retained == artifacts.EMPTY and section
            ):
                raise ValueError("Invalid raw memory section change")

    def apply(self, intent: NativeIntent, *, now: datetime, progress: Progress) -> NativeRemoval:
        data = unpack(intent.payload)
        row, target = StageRow.decode(data["row"]), data["target"]
        self._check_row(row)
        if (
            data["ref"] != asdict(self.ref)
            or data["action"] != intent.action
            or type(target) is not int
            or not -(1 << 63) <= target <= MAX_WATERMARK
        ):
            raise ValueError("Invalid Codex transaction intent")
        stage, phase2 = _jobs(self.db, row, int(now.timestamp()))
        current = _row(self.db, self.ref.source_ref)
        remove = intent.action == "remove"
        after_row = None if remove else row
        committed = (
            current == after_row
            and stage is not None
            and stage["status"] == "done"
            and (stage["last_success_watermark"] or 0) >= row.source_updated_at
            and phase2 is not None
            and (phase2["input_watermark"] or 0) >= target
        )
        if not committed and (
            current != (row if remove else None)
            or stage != data["stage"]
            or phase2 != data["phase2"]
        ):
            raise ValueError("Native row, selection or job changed since preparation")
        if (
            not committed
            and phase2
            and target <= max(phase2["input_watermark"] or 0, phase2["last_success_watermark"] or 0)
        ):
            raise ValueError("Invalid consolidation watermark")
        paths = {
            "summary": self.root
            / "rollout_summaries"
            / (artifacts.stem(row.thread_id, row.source_updated_at, row.rollout_slug) + ".md")
        }
        if self.root.name == "memories":
            paths["raw"] = self.root / "raw_memories.md"
        if data["changes"].keys() != paths.keys():
            raise ValueError("Missing or unrecognized native artifact")
        for key, entry in data["changes"].items():
            change = FileChange.parse(entry)
            self._validate_artifact(key, change, row, remove=remove)
            apply_file(paths[key], self.source.paths, change, key, progress=progress)
        if not committed:
            if remove:
                self.db.execute("DELETE FROM stage1_outputs WHERE thread_id=?", (row.thread_id,))
            else:
                self.db.execute("INSERT INTO stage1_outputs VALUES (?,?,?,?,?,?,?,?,?,?)", row)
            successful = max(
                row.source_updated_at, (stage or {}).get("last_success_watermark") or 0
            )
            _write_job(
                self.db,
                (stage or _new_job(STAGE, row.thread_id))
                | {
                    "status": "done",
                    "finished_at": data["stamp"],
                    "lease_until": None,
                    "last_error": None,
                    "input_watermark": successful,
                    "last_success_watermark": successful,
                },
            )
            _write_job(
                self.db,
                (phase2 or _new_job(GLOBAL, "global"))
                | {
                    "status": "pending",
                    "retry_at": None,
                    "retry_remaining": max(3, (phase2 or {}).get("retry_remaining", 3)),
                    "input_watermark": target,
                },
            )
            self.db.commit()
        progress.checkpoint("database")
        receipt = pack(
            ref=asdict(self.ref),
            dbIdentity=self.identity,
            target=target,
            action=intent.action,
            summary=paths["summary"].name,
        )
        return NativeRemoval("pending", receipt)


class GuardedCodexTransaction:
    def __init__(
        self, source: native.CodexMemorySource, ref: MemoryRef, root: Path, stack: ExitStack
    ) -> None:
        self.source, self.ref, self.root, self.stack = source, ref, root, stack
        self.live: CodexTransaction | None = None

    def _transaction(self, archived_row: StageRow | None = None) -> CodexTransaction:
        if self.live:
            return self.live
        path = Path(self.ref.store_id)
        with native.open_readonly(path, self.source.paths, codex_home=self.source.codex_home) as (
            preview,
            ident,
        ):
            if not native.verified_schema(preview):
                raise ValueError("Unverified native Codex memory schema")
            row = _row(preview, self.ref.source_ref) or archived_row
            if row is None:
                raise ValueError("Selected Codex memory disappeared")
            probe = CodexTransaction(self.source, self.ref, preview, self.root, ident)
            probe._check_row(row)
            _job(preview, STAGE, row.thread_id)
            _job(preview, GLOBAL, "global")
            artifacts.summary_image(self.root, row, self.source.paths)
            if self.root.name == "memories":
                raw = file_image(self.root / "raw_memories.md", self.source.paths).text
                artifacts.raw_section(raw, row, preview)
        self.source.verify_workspace(self.ref)
        db = self.stack.enter_context(
            closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0.25))
        )
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        if file_identity(path.stat()) != ident or not native.verified_schema(db):
            raise ValueError("Codex memory database changed or has unknown schema")
        self.live = CodexTransaction(self.source, self.ref, db, self.root, ident)
        return self.live

    def capture(self) -> MemorySnapshot:
        return self._transaction().capture()

    def prepare(
        self, snapshot: MemorySnapshot, *, action: Literal["remove", "restore"], now: datetime
    ) -> NativeIntent:
        return self._transaction(StageRow.decode(unpack(snapshot.payload)["row"])).prepare(
            snapshot, action=action, now=now
        )

    def apply(self, intent: NativeIntent, *, now: datetime, progress: Progress) -> NativeRemoval:
        return self._transaction(StageRow.decode(unpack(intent.payload)["row"])).apply(
            intent, now=now, progress=progress
        )


@contextmanager
def mutation(source: native.CodexMemorySource, ref: MemoryRef) -> Iterator[NativeMemoryTransaction]:
    _, root = _roots(source, ref)
    with directory(root.parent, source.paths) as folder:
        lock = os.open(
            root.with_suffix(".lock").name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=folder,
        )
        try:
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Unsafe native workspace lock file")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise NativeBusy("Native memory workspace is busy") from exc
            with ExitStack() as stack:
                yield GuardedCodexTransaction(source, ref, root, stack)
        except sqlite3.OperationalError as exc:
            if native.sqlite_busy(exc):
                raise NativeBusy("Native memory database is busy") from exc
            raise
        finally:
            os.close(lock)


def cleanup_status(
    source: native.CodexMemorySource, receipt: bytes
) -> Literal["pending", "completed", "failed", "unknown"]:
    try:
        data = unpack(receipt)
        ref = MemoryRef(**data["ref"])
        path, root = _roots(source, ref)
        with native.open_readonly(path, source.paths, codex_home=source.codex_home) as (
            db,
            identity,
        ):
            if identity != data["dbIdentity"] or not native.verified_schema(db):
                return "unknown"
            phase2 = _job(db, GLOBAL, "global")
            if phase2 is None:
                return "unknown"
            if phase2["status"] == "error":
                return "failed"
            if (
                phase2["status"] != "done"
                or (phase2["last_success_watermark"] or 0) < data["target"]
            ):
                return "pending"
            return _observe_artifacts(source, data, ref, db, root, identity)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return "unknown"


def _observe_artifacts(
    source: native.CodexMemorySource,
    data: dict[str, Any],
    ref: MemoryRef,
    db: sqlite3.Connection,
    root: Path,
    identity: str,
) -> Literal["pending", "completed", "unknown"]:
    row = _row(db, ref.source_ref)
    if data["action"] == "restore":
        return (
            "completed"
            if row
            and native._record(
                row.revision(),
                Path(ref.store_id),
                identity,
                source.workspace_id("v1" if root.name == "memories" else "v2"),
                None,
            ).ref
            == ref
            else "unknown"
        )
    if row is not None:
        return "unknown"
    name = data["summary"]
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".md"):
        return "unknown"
    folder = root / "rollout_summaries"
    source.paths.check(folder)
    artifacts.verify_summary_names(folder, folder / name, ref.source_ref, source.paths)
    if file_image(folder / name, source.paths).text is not None:
        return "pending"
    if root.name == "memories":
        raw = file_image(root / "raw_memories.md", source.paths).text
        if raw and (raw != artifacts.EMPTY and not raw.startswith(artifacts.HEADER)):
            return "unknown"
        if raw and f"## Thread `{ref.source_ref}`\n" in raw:
            return "pending"
        artifacts.raw_section(raw, None, db)
    return "completed"
