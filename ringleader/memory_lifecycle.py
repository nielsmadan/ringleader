from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from . import (
    harvests,
    memory_catalog,
    memory_operations,
    memory_scan,
    publish_state,
    registry,
    session_env,
)
from .agents import canonical_agent
from .instruction_targets import document_scope, instruction_targets
from .memory_operations import Cleanup, Decision, Operation
from .memory_sources.base import MemoryReader, NativePaths
from .memory_sources.claude_code import is_instruction_area
from .memory_sources.transactions import MAX_FILE, NativeBusy
from .memory_types import (
    MemoryRef,
    MemorySnapshot,
    MemorySource,
    NativeIntent,
    Progress,
    origin_fields,
)
from .paths import AGENT_INSTRUCTION_BASENAMES, state_dir
from .presence import SETUP_COMMAND, HookStatus, PresenceClient, PresenceResult
from .stateio import publish_lock, sync_directory

type PendingAction = Literal["remove", "restore"]
type OutcomeState = Literal[
    "harvested", "removed", "removal-pending", "restored", "refused", "failed"
]


@dataclass(frozen=True)
class MemoryOutcome:
    operation_id: str | None
    memory_id: str
    state: OutcomeState
    cleanup: Cleanup
    harvest_path: str | None
    message: str
    pending_action: PendingAction | None = None


@dataclass(frozen=True)
class RemovalReview:
    registry_owner: str
    ref: MemoryRef
    decision: Decision
    operation_id: str | None
    harvest_id: str | None
    approval_hash: str | None
    retention: int
    frozen: bool
    route: Literal["discard", "new", "queue", "direct", "published"]
    queue: tuple[str, str] | None = None
    destinations: tuple[tuple[str, str], ...] = ()
    receipt_id: str | None = None
    markdown: str | None = None

    def verify(self, current: RemovalReview, markdown: str | None, token: str | None) -> None:
        if current.retention != self.retention:
            raise ValueError("Recovery retention changed; reopen the entry and confirm again")
        if not registry.owner_matches(self.registry_owner, current.registry_owner):
            raise ValueError("Removal review belongs to a different registry")
        if (
            current != replace(self, registry_owner=current.registry_owner, markdown=None)
            or self.markdown != markdown
        ):
            raise ValueError("Saved harvest changed; review the approval and confirm again")
        if token != (current.queue[1] if current.queue else None):
            raise ValueError("Pending harvest token changed")
        current.approve(markdown)

    def approve(self, markdown: str | None) -> RemovalReview:
        if markdown is None:
            if self.route == "new":
                raise ValueError("Complete requires a saved harvest or explicit approved Markdown")
            return self
        _checked_markdown(markdown)
        if self.route not in ("queue", "new") or (
            self.frozen and (not self.queue or self.queue[0] != markdown)
        ):
            raise ValueError("This approval can no longer be edited")
        return replace(self, markdown=markdown)


@dataclass(frozen=True)
class MemoryPresence:
    sessions: PresenceResult
    hooks: HookStatus
    current_sessions: tuple[tuple[str, str], ...]


def memory_presence(source: str) -> MemoryPresence:
    client = PresenceClient()
    agent = "claude" if source == "claude-code" else source
    return MemoryPresence(
        client.sessions(), client.hook_status(agent), session_env.known_sessions()
    )


def _verify_hook_coverage(source: MemorySource, hooks: HookStatus) -> None:
    expected = source.hook_path
    reported = hooks.path
    if hooks.state != "installed":
        raise NativeBusy(
            f"{source.name} presence hooks are {hooks.state}. "
            f"Expected hooks at {expected}. Hook status reported {reported or 'no absolute path'}. "
            f"Run `{SETUP_COMMAND}`."
        )
    try:
        expected_file = expected.stat()
        matches = (
            reported is not None
            and reported.is_absolute()
            and reported.name == expected.name
            and reported.parent.samefile(expected.parent)
            and stat.S_ISREG(expected_file.st_mode)
            and os.path.samestat(reported.stat(), expected_file)
        )
    except (OSError, ValueError):
        matches = False
    if not matches:
        raise NativeBusy(
            f"Cannot verify {source.name} presence hooks for {expected}. "
            f"Hook status reported {reported or 'no absolute path'}."
        )


def _result(
    op: Operation, state: OutcomeState, message: str, *, pending_action: PendingAction | None = None
) -> MemoryOutcome:
    reg = registry.load_registry(op.registry_path)
    paths = reg.get("memoryDecisions", {}).get(op.id, {}).get("harvestPath", [])
    return MemoryOutcome(
        op.id,
        op.ref.memory_id,
        state,
        op.cleanup,
        next(iter(paths or op.direct_paths), None),
        message,
        pending_action,
    )


def _checked_markdown(markdown: str, *, direct: bool = False) -> str:
    limit = MAX_FILE if direct else publish_state.MAX_BYTES
    if not markdown.strip() or len(markdown.encode()) > limit:
        raise ValueError(f"Approved Markdown must be nonempty and at most {limit} UTF-8 bytes")
    return markdown


def _validate_decision(
    decision: Decision,
    remove: bool,
    markdown: str | None,
    direct: Sequence[Path] | None,
    token: str | None,
    review: RemovalReview | None,
) -> None:
    if review is not None and (not remove or direct is not None):
        raise ValueError("Removal review requires its original decision")
    if decision not in ("discard", "harvest") or (
        decision == "discard"
        and (not remove or markdown is not None or direct is not None or token is not None)
    ):
        raise ValueError(
            "Discard requires explicit removal without Markdown, a destination or a harvest token"
        )


def _validate_request(
    now: datetime, retention_days: int, markdown: str | None, direct: bool
) -> None:
    if now.utcoffset() is None:
        raise ValueError("Memory operation requires a timezone-aware clock")
    if type(retention_days) is not int or retention_days < 0:
        raise ValueError("Recovery retention must be a nonnegative integer")
    if markdown is not None:
        _checked_markdown(markdown, direct=direct)


def _published(op: Operation, reg: dict[str, Any]) -> bool:
    evidence = reg.get("memoryPublications", {}).get(op.harvest_id)
    if evidence is None:
        return False
    fields = {"harvestId": op.harvest_id, **origin_fields(op.origin)}
    if (
        not isinstance(evidence, dict)
        or not isinstance(evidence.get("receiptId"), str)
        or not isinstance(evidence.get("checkout"), str)
        or not isinstance(evidence.get("changedPaths"), list)
    ):
        raise TypeError("Incomplete memory publication evidence")
    if evidence.get("outcome") not in ("changed", "no-change") or any(
        key not in evidence or evidence[key] != value for key, value in fields.items()
    ):
        raise ValueError("Publication evidence does not match this memory revision")
    if not op.approval_hash or evidence.get("approvedBodyDigest") != op.approval_hash:
        raise ValueError("Publication evidence does not match the approved Markdown")
    return True


def _queue_item(
    op: Operation,
    snapshot: harvests.HarvestSnapshot,
    *,
    recheck: bool = True,
    approved_token: str | None = None,
) -> harvests.Harvest | None:
    if op.harvest_id in snapshot.ambiguous_ids:
        raise ValueError("Ambiguous memory harvest ID")
    item = next((item for item in snapshot.items if item.harvest_id == op.harvest_id), None)
    if item and recheck and harvests.load_harvest_path(item.path).token != item.token:
        raise ValueError("Pending harvest token changed")
    if item:
        owner = item.lifecycle_owner(op, approved_token=approved_token)
        if item.legacy_owner:
            item = replace(item, registry_owner=owner, legacy_owner=False)
    return item


def _queue_evidence(
    op: Operation,
    path: Path,
    expected: str | None,
    snapshot: harvests.HarvestSnapshot,
    *,
    durable: bool = True,
    replay: bool = False,
) -> None:
    reg = registry.load_registry(path)
    draft = op.payload.get("draft")
    if draft is not None and (
        not isinstance(draft, str) or hashlib.sha256(draft.encode()).hexdigest() != op.approval_hash
    ):
        raise ValueError("Journaled harvest draft does not match the approved Markdown")
    if draft is not None:
        _checked_markdown(draft)
    if _published(op, reg):
        if expected is not None and (not replay or draft is None):
            raise ValueError("Harvest has already been published")
        return
    item = _queue_item(
        op,
        snapshot,
        approved_token=op.payload.get("expected")
        if draft is not None and op.payload.get("writing")
        else None,
    )
    if item is not None:
        if draft is None or draft == item.content:
            _checked_markdown(item.content)
        if (draft is None or (expected is not None and not replay)) and (
            op.approval_hash != item.body_digest
        ):
            raise ValueError("Saved harvest content changed outside its approval")
        written = draft is not None and item.content == draft and op.payload.get("writing")
        if expected is not None and expected != item.token and not (replay and written):
            raise ValueError("Pending harvest token changed")
        if durable:
            sync_directory(item.path.parent.parent)
        if written:
            return
        if draft is not None and item.content != draft:
            if expected != item.token:
                raise ValueError("Replacing a saved draft requires its current harvest token")
            publish_state.require_unclaimed_memory(item.harvest_id)
            if durable:
                harvests.rewrite_memory_harvest_unlocked(
                    item,
                    folder_path=item.folder_path,
                    repo_url=item.repo_url,
                    clone_url=item.clone_url,
                    content=draft,
                )
        return
    if draft is None or (op.saved and not op.payload.get("writing")) or expected:
        raise ValueError("No matching saved harvest or durable publication evidence")
    if not durable:
        return
    harvests.write_harvest_unlocked(
        origin=op.origin,
        harvest_id=op.harvest_id,
        name=op.name,
        folder_path=op.project,
        repo_url=None,
        harvested_at=op.created,
        content=draft,
        registry_path=path,
        registry_owner=op.registry_path,
        snapshot=snapshot,
    )
    sync_directory(harvests.harvests_dir().parent)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _existing_identities(paths: Iterable[Path]) -> set[tuple[int, int]]:
    identities = set()
    for path in dict.fromkeys(paths):
        try:
            identities.add(_identity(path.stat()))
        except FileNotFoundError:
            continue
    return identities


def _destination_digest(candidate: Path, protected: set[tuple[int, int]], *, durable: bool) -> str:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        fd = os.open(candidate.anchor, flags)
        stack.callback(os.close, fd)
        ancestors = [fd]
        for name in candidate.parts[1:-1]:
            fd = os.open(name, flags, dir_fd=fd)
            stack.callback(os.close, fd)
            ancestors.append(fd)
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        file_fd = os.open(candidate.name, file_flags, dir_fd=fd)
        stack.callback(os.close, file_fd)
        with os.fdopen(file_fd, "rb", closefd=False) as stream:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Direct integration needs a regular destination file")
            if _identity(before) in protected or any(
                _identity(os.fstat(parent)) in protected for parent in ancestors
            ):
                raise ValueError("Direct integration requires a destination outside native storage")
            body = stream.read(MAX_FILE + 1)
            if len(body) > MAX_FILE or not body.strip():
                raise ValueError("Direct integration needs a nonempty bounded destination file")
            if durable:
                os.fsync(file_fd)
                for parent in reversed(ancestors):
                    os.fsync(parent)
            named = os.stat(candidate.name, dir_fd=fd, follow_symlinks=False)
            intact = all(
                os.path.samestat(
                    os.stat(name, dir_fd=parent, follow_symlinks=False), os.fstat(child)
                )
                for name, parent, child in zip(
                    candidate.parts[1:-1], ancestors[:-1], ancestors[1:], strict=True
                )
            )
            if (
                not intact
                or not os.path.samestat(before, named)
                or before.st_ctime_ns != (os.fstat(file_fd).st_ctime_ns)
            ):
                raise ValueError("Direct integration destination changed while recording evidence")
            return hashlib.sha256(body).hexdigest()


def _entry(reg: Mapping[str, Any], ref: MemoryRef) -> dict[str, Any]:
    row = memory_catalog.rows_by_id(reg).get((ref.source, ref.memory_id))
    if (
        not row
        or not memory_catalog.configured(reg, row)
        or memory_catalog.reviewed_ref(row) != ref
    ):
        raise ValueError("Memory review is stale; refresh and review this revision")
    if row.get("mutationBlock"):
        raise ValueError(row["mutationBlock"])
    return row


def _operation_for_ref(ref: MemoryRef, history: Iterable[Operation]) -> Operation | None:
    matches = [op for op in history if op.ref == ref]
    if len(matches) > 1:
        raise ValueError("Multiple operations claim this memory revision")
    return next(iter(matches), None)


def _expiry(now: datetime, days: int) -> str:
    try:
        return (now + timedelta(days=days)).isoformat()
    except OverflowError:
        return datetime.max.replace(tzinfo=now.tzinfo).isoformat()


def _purge(op: Operation) -> None:
    op.payload.clear()
    op.name = op.ref.memory_id
    op.purged = True
    op.write()


class MemoryLifecycle:
    def __init__(
        self,
        *,
        sources: Callable[
            [Mapping[str, Any]], Iterable[MemoryReader]
        ] = memory_scan.configured_sources,
        presence: Callable[[str], MemoryPresence] = memory_presence,
    ) -> None:
        self.sources = sources
        self.presence = presence

    def _source(self, reg: Mapping[str, Any], ref: MemoryRef) -> MemorySource:
        found = next((s for s in self.sources(reg) if s.name == ref.source), None)
        if isinstance(found, MemorySource):
            return found
        raise ValueError("Native memory mutation is unavailable for this source")

    def _confirm(
        self, reg: Mapping[str, Any], row: dict[str, Any], *, defer_busy: bool
    ) -> str | None:
        ref = memory_catalog.native_ref(row)
        source = self._source(reg, ref)
        try:
            record = source.read_selected(ref)
        except NativeBusy as exc:
            if not defer_busy:
                raise
            return str(exc)
        if record.ref != ref or record.mutation_block:
            raise ValueError(
                record.mutation_block or "Native memory disappeared or changed since review"
            )
        return None

    def _destinations(
        self,
        native: MemoryRef,
        reg: Mapping[str, Any],
        paths: Sequence[Path],
        *,
        durable: bool = True,
    ) -> dict[str, str]:
        if not paths:
            raise ValueError("Direct integration requires at least one destination")
        roots = [Path(native.store_id), state_dir()]
        adapters = [s for s in self.sources(reg) if isinstance(s, MemorySource)]
        configs = tuple(root for s in adapters for root in s.instruction_config_roots())
        roots.extend(
            p
            for s in adapters
            for p in s.protected_locations(native if s.name == native.source else None)
        )
        protected = _existing_identities(roots)
        scopes = {path: document_scope(path, configs) for path in map(Path.absolute, paths)}
        instructions = {
            (root, recurse): instruction_targets(root, configs, recurse=recurse)
            for root, recurse in set(scopes.values())
        }
        return {
            str(candidate): self._attest(
                candidate, protected | instructions[scope].identities, configs, durable=durable
            )
            for candidate, scope in scopes.items()
        }

    @staticmethod
    def _attest(
        candidate: Path,
        protected: set[tuple[int, int]],
        configs: Sequence[Path],
        *,
        durable: bool,
    ) -> str:
        if candidate.resolve(strict=True) != candidate:
            raise ValueError("Direct integration requires a canonical destination")
        if candidate.name.casefold() in AGENT_INSTRUCTION_BASENAMES or any(
            is_instruction_area(candidate, config, NativePaths()) for config in (None, *configs)
        ):
            raise ValueError("Instruction files and Git internals cannot be integration targets")
        return _destination_digest(candidate, protected, durable=durable)

    def _evidence(
        self,
        op: Operation,
        path: Path,
        expected: str | None,
        *,
        durable: bool = True,
        replay: bool = False,
        snapshot: harvests.HarvestSnapshot | None = None,
    ) -> None:
        if not op.direct_paths:
            _queue_evidence(
                op,
                path,
                expected,
                snapshot or harvests.snapshot_harvests(),
                durable=durable,
                replay=replay,
            )
            return
        approved = op.payload.get("directMarkdown")
        if (
            not isinstance(approved, str)
            or not approved.strip()
            or hashlib.sha256(approved.encode()).hexdigest() != op.approval_hash
        ):
            raise ValueError("Missing or changed direct-integration approval")
        if expected or op.payload.get("draft") is not None:
            raise ValueError("Direct integration has already been recorded")
        if (
            self._destinations(
                op.native,
                registry.load_registry(path),
                tuple(map(Path, op.direct_paths)),
                durable=durable,
            )
            != op.direct_paths
        ):
            raise ValueError("Direct-integration destination changed")

    def _review(  # noqa: PLR0913 -- reuse the locked registry and queue observations
        self,
        ref: MemoryRef,
        decision: Decision,
        default: int,
        op: Operation | None,
        reg: dict[str, Any],
        *,
        path: Path,
        snapshot: harvests.HarvestSnapshot | None = None,
        durable: bool = False,
    ) -> RemovalReview:
        if op and op.decision != decision:
            raise ValueError("Another decision already owns this memory revision")
        frozen = bool(op and op.remove_requested)
        review = RemovalReview(
            registry_owner=op.registry_path if op else str(path.resolve()),
            ref=ref,
            decision=decision,
            operation_id=op.id if op else None,
            harvest_id=op.harvest_id if op else None,
            approval_hash=op.approval_hash if op else None,
            retention=op.retention if op and frozen else default,
            frozen=frozen,
            route="discard" if decision == "discard" else "new",
        )
        if decision == "discard" or op is None:
            return review
        if not op.saved or "writing" in op.payload_keys:
            raise ValueError(f"Saved harvest changed or interrupted; run `rl mem recover {op.id}`")
        if op.direct_paths:
            self._evidence(op, Path(op.registry_path), None, durable=durable, snapshot=snapshot)
            return replace(
                review, route="direct", destinations=tuple(sorted(op.direct_paths.items()))
            )
        try:
            published = _published(op, reg)
        except TypeError as exc:
            raise ValueError(str(exc)) from exc
        if published:
            return replace(
                review,
                route="published",
                receipt_id=reg["memoryPublications"][op.harvest_id]["receiptId"],
            )
        item = _queue_item(op, snapshot or harvests.snapshot_harvests(), recheck=False)
        if item is None or item.body_digest != op.approval_hash:
            raise ValueError(
                "Saved harvest changed or is unavailable; recover the approval before continuing"
            )
        return replace(review, route="queue", queue=(_checked_markdown(item.content), item.token))

    def review_removal(
        self, ref: MemoryRef, path: Path, decision: Decision, default: int
    ) -> RemovalReview:
        with publish_lock():
            reg = registry.load_registry(path)
            _entry(reg, ref)
            records = memory_operations.history(
                registry_path=path,
                ref=ref,
                select=lambda op: (
                    bool(op.direct_paths)
                    and op.ref == ref
                    and registry.owner_matches(op.registry_path, path)
                ),
            )
            op = _operation_for_ref(ref, memory_operations.owned(records, path))
            return self._review(ref, decision, default, op, reg, path=path)

    def preview_evidence(
        self,
        ref: MemoryRef,
        path: Path,
        *,
        direct: Sequence[Path] | None,
        expected_harvest_token: str | None,
    ) -> tuple[str, ...]:
        reg = registry.load_registry(path)
        row = _entry(reg, ref)
        records = memory_operations.history(registry_path=path, ref=ref)
        header = _operation_for_ref(ref, memory_operations.owned(records, path))
        op = memory_operations.load(header.id, path) if header else None
        if op and op.decision != "harvest":
            raise ValueError("Another decision already owns this memory revision")
        if direct is not None:
            if op and (op.saved or op.remove_requested):
                raise ValueError("Direct integration needs a new approval with exact Markdown")
            return tuple(
                self._destinations(memory_catalog.native_ref(row), reg, direct, durable=False)
            )
        if op is None:
            raise ValueError("Complete requires a saved harvest or explicit direct integration")
        try:
            self._evidence(op, path, expected_harvest_token, durable=False)
        except TypeError as exc:
            raise ValueError(str(exc)) from exc
        return tuple(op.direct_paths)

    def _quiet(self, op: Operation, source: MemorySource) -> None:
        context = self.presence(op.ref.source)
        result = context.sessions
        if not result.available or result.problems:
            raise NativeBusy("Native session presence is unknown")
        _verify_hook_coverage(source, context.hooks)
        running = {
            session.session_id
            for session in result.sessions
            if session.running and canonical_agent(session.agent) == op.ref.source
        }
        if any(
            canonical_agent(agent) == op.ref.source and identity not in running
            for agent, identity in context.current_sessions
        ):
            raise NativeBusy(f"The current {op.ref.source} session is active")
        for session in result.sessions:
            if not session.running or canonical_agent(session.agent) != op.ref.source:
                continue
            if not source.session_may_use_store(op.native, op.project, session.cwd):
                continue
            raise NativeBusy(f"A {op.ref.source} session is active; retry when its store is quiet")

    def decide(  # noqa: PLR0913
        self,
        ref: MemoryRef,
        *,
        decision: Decision,
        markdown: str | None,
        expected_harvest_token: str | None,
        registry_path: Path,
        now: datetime,
        retention_days: int = 30,
        remove: bool = False,
        direct: Sequence[Path] | None = None,
        require_retention_match: bool = False,
        review: RemovalReview | None = None,
    ) -> MemoryOutcome:
        op = None
        with publish_lock():
            try:
                _validate_decision(
                    decision, remove, markdown, direct, expected_harvest_token, review
                )
                _validate_request(now, retention_days, markdown, direct is not None)
                reg = registry.load_registry(registry_path)
                history = memory_operations.history(
                    registry_path=registry_path, ref=ref, cleanup=True
                )
                lineages = memory_operations.Lineages(history)
                header = _operation_for_ref(ref, memory_operations.owned(history, registry_path))
                op = header
                op = memory_operations.load(header.id, registry_path) if header else None
                snapshot = (
                    harvests.snapshot_harvests()
                    if decision == "harvest" and direct is None and not (op and op.direct_paths)
                    else None
                )
                if review is not None:
                    current = self._review(
                        ref,
                        decision,
                        retention_days,
                        op,
                        reg,
                        path=registry_path,
                        snapshot=snapshot,
                        durable=True,
                    )
                    review.verify(current, markdown, expected_harvest_token)
                if op and op.decision != decision:
                    raise ValueError("Another decision already owns this memory revision")
                if (
                    op
                    and remove
                    and require_retention_match
                    and op.remove_requested
                    and op.retention != retention_days
                ):
                    raise ValueError(
                        "Recovery retention changed; reopen the entry and confirm again"
                    )
                if op and op.source_state in ("removed", "restored"):
                    return self._repeat(op, markdown, registry_path)
                row = _entry(reg, ref)
                observed_busy = (
                    self._confirm(reg, row, defer_busy=remove)
                    if not op or not op.payload.get("intent")
                    else None
                )
                if op is None:
                    if decision == "harvest" and markdown is None:
                        raise ValueError(
                            "Complete requires a saved harvest or explicit direct integration"
                        )
                    op = Operation.new(ref, row, decision, registry_path, now)
                    op.sequence = 1 + lineages.latest(op).sequence
                lineages.check_approval(op, require_latest=header is not None and remove)
                if direct is not None:
                    if markdown is None or op.saved or op.remove_requested:
                        raise ValueError(
                            "Direct integration needs a new approval with exact Markdown"
                        )
                    op.direct_paths = self._destinations(op.native, reg, direct)
                    op.payload["directMarkdown"] = markdown
                elif markdown is not None:
                    self._approve(op, markdown, expected_harvest_token, registry_path, snapshot)
                if op.direct_paths and markdown is not None:
                    op.approval_hash = hashlib.sha256(markdown.encode()).hexdigest()
                if (
                    remove
                    and markdown is None
                    and decision == "harvest"
                    and not (review and review.route == "direct")
                ):
                    self._evidence(op, registry_path, expected_harvest_token, snapshot=snapshot)
                if remove:
                    if not op.remove_requested:
                        op.retention = retention_days
                    op.remove_requested = True
                op.write()
                outcome = self._run(
                    op,
                    registry_path,
                    now,
                    fresh=True,
                    expected=expected_harvest_token,
                    apply_native=remove and observed_busy is None,
                    snapshot=snapshot,
                )
                return (
                    _result(op, "removal-pending", observed_busy, pending_action="remove")
                    if observed_busy
                    else outcome
                )
            except (
                ValueError,
                OSError,
                sqlite3.Error,
                subprocess.SubprocessError,
                KeyError,
                TypeError,
                registry.RegistryConflict,
            ) as exc:
                return self._error(op, ref.memory_id, exc)

    def _repeat(self, op: Operation, markdown: str | None, path: Path) -> MemoryOutcome:
        if op.restoring:
            raise ValueError("Restore is in progress; recover the current operation")
        if op.source_state == "restored":
            raise ValueError("Restored entry requires a fresh review")
        if (
            markdown is not None
            and hashlib.sha256(markdown.encode()).hexdigest() != op.approval_hash
        ):
            raise ValueError("Completed memory operation has a different approval")
        return self._finish(op, path)

    def _approve(
        self,
        op: Operation,
        markdown: str,
        expected: str | None,
        path: Path,
        snapshot: harvests.HarvestSnapshot | None,
    ) -> None:
        approval_hash = hashlib.sha256(markdown.encode()).hexdigest()
        if op.payload.get("intent") and op.approval_hash == approval_hash:
            self._evidence(op, path, expected, snapshot=snapshot)
            return
        if (
            op.direct_paths
            or op.payload.get("intent")
            or (op.remove_requested and op.approval_hash != approval_hash)
        ):
            raise ValueError("This approval can no longer be edited")
        if _published(op, registry.load_registry(path)):
            if op.payload.get("writing") and op.payload.get("draft") == markdown:
                return
            raise ValueError("This approval can no longer be edited")
        assert snapshot is not None
        item = _queue_item(op, snapshot)
        if item and item.content != markdown:
            if item.token != expected:
                raise ValueError("Replacing a saved draft requires its current harvest token")
            publish_state.require_unclaimed_memory(item.harvest_id)
        elif expected is not None and (not item or expected != item.token):
            raise ValueError("Pending harvest token changed")
        if op.saved and not item:
            raise ValueError("Saved harvest is missing")
        op.approval_hash = approval_hash
        op.payload.update(
            draft=markdown,
            expected=expected,
            writing=True,
        )

    @staticmethod
    def _error(op: Operation | None, memory_id: str, exc: Exception) -> MemoryOutcome:
        state: OutcomeState = (
            "refused" if isinstance(exc, (ValueError, KeyError, FileNotFoundError)) else "failed"
        )
        return MemoryOutcome(
            op.id if op and memory_operations.durable(op.id) else None,
            memory_id,
            state,
            op.cleanup if op else "not-needed",
            None,
            str(exc),
        )

    def _run(  # noqa: PLR0913
        self,
        op: Operation,
        path: Path,
        now: datetime,
        *,
        fresh: bool,
        expected: str | None = None,
        apply_native: bool = True,
        snapshot: harvests.HarvestSnapshot | None = None,
    ) -> MemoryOutcome:
        if op.decision == "harvest" and not op.restoring:
            self._evidence(
                op,
                path,
                expected if expected is not None else op.payload.get("expected"),
                replay=True,
                snapshot=snapshot,
            )
            op.saved = True
            for field in ("draft", "expected", "writing"):
                op.payload.pop(field, None)
        if op.remove_requested and not op.restoring:
            op.source_state = "removal-pending"
        op.write()
        op.project_registry(path)
        if op.harvest_id:
            publish_state.reconcile_memory_queue_unlocked(path)
        if not apply_native or (not op.remove_requested and not op.restoring):
            return _result(op, "harvested", "Approved knowledge saved")
        return self._mutate(op, path, now, fresh=fresh)

    def _mutate(
        self, op: Operation, path: Path, now: datetime, *, fresh: bool, begin_restore: bool = False
    ) -> MemoryOutcome:
        reg = registry.load_registry(path)
        source = self._source(reg, op.ref)
        try:
            source.verify_workspace(op.native)
            self._quiet(op, source)
            if not (op.restoring or begin_restore) and "intent" in op.payload:
                existing = memory_catalog.rows_by_id(reg).get(op.identity)
                if existing is not None and memory_catalog.reviewed_ref(existing) != op.ref:
                    raise ValueError(
                        "A different review incarnation is present; refresh and review this revision"
                    )
            if not (op.restoring or begin_restore) and "intent" not in op.payload:
                _entry(reg, op.ref)
                if (
                    op.native.source == "claude-code"
                    and op.native.incarnation.startswith("inode:")
                    and not fresh
                ):
                    raise ValueError("Native file continuity requires a fresh review")
            self._apply(op, source, now, fresh=fresh, begin_restore=begin_restore)
        except NativeBusy as exc:
            return _result(
                op,
                "removal-pending",
                str(exc),
                pending_action="restore" if begin_restore or op.restoring else "remove",
            )
        op.source_state = "restored" if op.restoring else "removed"
        if not op.restoring:
            op.expires = _expiry(now, op.retention)
        op.restoring = False
        op.payload.pop("intent", None)
        op.progress = []
        op.write()
        return self._finish(op, path)

    @staticmethod
    def _apply(
        op: Operation, source: MemorySource, now: datetime, *, fresh: bool, begin_restore: bool
    ) -> None:
        action: Literal["remove", "restore"] = (
            "restore" if op.restoring or begin_restore else "remove"
        )
        with source.mutation(op.native) as transaction:
            if "snapshot" not in op.payload:
                snap = transaction.capture()
                if snap.ref != op.native:
                    raise ValueError("Native capture does not match the approved revision")
                op.payload["snapshot"] = snap.payload.decode()
                op.write()
            elif (
                fresh
                and action == "remove"
                and op.native.incarnation.startswith("inode:")
                and not op.progress
            ):
                transaction.capture()
            if begin_restore or "intent" not in op.payload:
                snap = MemorySnapshot(op.native, op.payload["snapshot"].encode())
                intent = transaction.prepare(snap, action=action, now=now)
                if begin_restore:
                    op.progress = []
                    op.done = False
                    op.restoring = True
                op.payload["intent"] = intent.payload.decode()
                op.write()
            intent = NativeIntent(action, op.payload["intent"].encode())
            outcome = transaction.apply(
                intent, now=now, progress=Progress(frozenset(op.progress), op.component)
            )
            op.cleanup = outcome.cleanup
            op.receipt = outcome.receipt.decode() if outcome.receipt else None

    @staticmethod
    def _finish(op: Operation, path: Path, *, reconcile_queue: bool = False) -> MemoryOutcome:
        op.project_registry(path)
        if reconcile_queue and op.harvest_id:
            publish_state.reconcile_memory_queue_unlocked(path, operation_ids={op.id})
        if not op.done:
            op.done = True
            op.write()
        if op.retention == 0 and not op.purged:
            _purge(op)
        op.project_registry(path)
        if op.source_state == "restored":
            return _result(op, "restored", "Selected native memory restored")
        return _result(op, "removed", "Selected native memory removed")

    def recover(
        self, operation_id: str, *, registry_path: Path, now: datetime, restore: bool = False
    ) -> MemoryOutcome:
        op = None
        with publish_lock():
            try:
                header = memory_operations.operation_header(operation_id, registry_path)
                op = header
                history = memory_operations.history(
                    registry_path=registry_path, ref=header.ref, cleanup=True
                )
                if memory_operations.Lineages(history).latest(header).id != header.id:
                    if restore:
                        raise ValueError("A newer decision owns this memory identity")
                    return self._recover_superseded(header, registry_path, now)
                op = memory_operations.load(operation_id, registry_path)
                if restore and not op.restoring:
                    if op.source_state == "restored":
                        return self._finish(op, registry_path)
                    if (
                        op.source_state != "removed"
                        or op.purged
                        or not op.expires
                        or datetime.fromisoformat(op.expires) <= now
                    ):
                        raise ValueError("Selected operation has no available recovery payload")
                    return self._mutate(op, registry_path, now, fresh=False, begin_restore=True)
                if op.native_finished:
                    if op.receipt:
                        source = self._source(registry.load_registry(registry_path), op.ref)
                        observed = source.cleanup_status(op.receipt.encode())
                        if observed != op.cleanup:
                            op.cleanup = observed
                            op.write()
                    return self._finish(op, registry_path, reconcile_queue=True)
                return self._run(op, registry_path, now, fresh=False)
            except (
                OSError,
                ValueError,
                sqlite3.Error,
                subprocess.SubprocessError,
                KeyError,
                TypeError,
                registry.RegistryConflict,
            ) as exc:
                return self._error(op, op.ref.memory_id if op else "", exc)

    def _recover_superseded(self, op: Operation, path: Path, now: datetime) -> MemoryOutcome:
        reg = registry.load_registry(path)
        projection = memory_operations.needs_projection(op, reg)
        if op.native_finished:
            return self._finish(op, path, reconcile_queue=True)
        if memory_operations.needs_harvest_repair(op, reg):
            return self._run(
                memory_operations.load(op.id, path), path, now, fresh=False, apply_native=False
            )
        if projection:
            op.project_registry(path)
            return _result(op, "removal-pending", "Projected superseded operation history")
        raise ValueError("A newer decision owns this memory identity")


def save_memory_harvest(
    ref: MemoryRef,
    *,
    markdown: str,
    expected_harvest_token: str | None,
    registry_path: Path,
    now: datetime,
) -> MemoryOutcome:
    return MemoryLifecycle().decide(
        ref,
        decision="harvest",
        markdown=markdown,
        expected_harvest_token=expected_harvest_token,
        registry_path=registry_path,
        now=now,
    )


def complete_memory(  # noqa: PLR0913 -- optional guards bind the interactive review
    ref: MemoryRef,
    *,
    markdown: str | None,
    expected_harvest_token: str | None,
    registry_path: Path,
    now: datetime,
    retention_days: int,
    require_retention_match: bool = False,
    review: RemovalReview | None = None,
) -> MemoryOutcome:
    return MemoryLifecycle().decide(
        ref,
        decision="harvest",
        markdown=markdown,
        expected_harvest_token=expected_harvest_token,
        registry_path=registry_path,
        now=now,
        retention_days=retention_days,
        remove=True,
        require_retention_match=require_retention_match,
        review=review,
    )


def discard_memory(
    ref: MemoryRef,
    *,
    registry_path: Path,
    now: datetime,
    retention_days: int,
    require_retention_match: bool = False,
    review: RemovalReview | None = None,
) -> MemoryOutcome:
    return MemoryLifecycle().decide(
        ref,
        decision="discard",
        markdown=None,
        expected_harvest_token=None,
        registry_path=registry_path,
        now=now,
        retention_days=retention_days,
        remove=True,
        require_retention_match=require_retention_match,
        review=review,
    )


def mark_memory_integrated(
    ref: MemoryRef, *, markdown: str, paths: Sequence[Path], registry_path: Path, now: datetime
) -> MemoryOutcome:
    return MemoryLifecycle().decide(
        ref,
        decision="harvest",
        markdown=markdown,
        expected_harvest_token=None,
        registry_path=registry_path,
        now=now,
        direct=paths,
    )


def restore_memory(operation_id: str, *, registry_path: Path, now: datetime) -> MemoryOutcome:
    return MemoryLifecycle().recover(
        operation_id, registry_path=registry_path, now=now, restore=True
    )


def recover_memory(operation_id: str, *, registry_path: Path, now: datetime) -> MemoryOutcome:
    return MemoryLifecycle().recover(operation_id, registry_path=registry_path, now=now)


def prune_memory_recovery(
    *, registry_path: Path, now: datetime, delete: bool = False
) -> tuple[str, ...]:
    def candidates(*, cleanup: bool = False) -> list[Operation]:
        return [
            op
            for op in memory_operations.owned(
                memory_operations.history(cleanup=cleanup), registry_path
            )
            if memory_operations.expired(op, now)
        ]

    if not delete:
        return tuple(op.id for op in candidates())
    with publish_lock():
        eligible = candidates(cleanup=True)
        for op in eligible:
            _purge(op)
            op.project_registry(registry_path)
        return tuple(op.id for op in eligible)
