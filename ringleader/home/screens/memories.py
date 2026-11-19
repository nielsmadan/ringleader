from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal, Self

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.document._document import Document, EditResult, Location
from textual.document._document_navigator import DocumentNavigator
from textual.document._wrapped_document import WrappedDocument
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Log, OptionList, Static, TextArea

from ... import memory_scan, resume
from ...commands.mem_output import terminal_text
from ...commands.mem_scan import entry_metadata
from ...config import load_config
from ...decisions import MemoryDecisionOp, MemoryPruneOp, MemoryRecoveryOp, MemoryScanOp
from ...harvesting import tool_free_completion
from ...memory_lifecycle import RemovalReview
from ...memory_operations import RecoveryListing
from ...memory_types import MemoryRecord
from ...tui.background import CancelToken, Done, Failed
from ...tui.keys import nav_bindings
from ...tui.modals import AgentPickModal, ConfirmModal
from ...tui.rows import highlighted_key, set_rows
from ..base import InboxScreen
from ..jobs import HarvestReady, HarvestResult, memory_harvest_job
from ..memory_view import (
    MemoryListing,
    memory_listing,
    scan_catalog,
    source_lines,
)
from ..state import plural
from ..writer import Finished


class SourceScreen(InboxScreen):
    def __init__(self, reg: dict[str, Any]) -> None:
        super().__init__()
        self.lines = source_lines(reg)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Memory sources and stores")
        yield Log(auto_scroll=False)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one(Log)
        log.write(terminal_text("\n".join(self.lines) or "No scan results yet."))
        log.focus()


class MemoriesScreen(InboxScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "view", "read"),
        Binding("s", "sources", "sources"),
        Binding("r", "recovery", "recovery"),
        Binding("v", "retained", "retained drafts"),
        Binding("ctrl+r", "scan", "refresh"),
        *nav_bindings(),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
    ]
    DEFAULT_CSS = """
    MemoriesScreen #memory-title, MemoriesScreen #memory-status { padding: 0 1; }
    MemoriesScreen #memory-rows { height: 1fr; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._ephemeral: dict[str, Any] | None = None
        self._rows: dict[str, dict[str, Any]] = {}
        self._listed_catalog: dict[str, Any] | None = None
        self._listed_pending: set[str] = set()
        self._listing: MemoryListing | None = None
        self._scan: CancelToken | None = None
        self._ticket: int | None = None
        self._queued_scan = False
        self._detail: CancelToken | None = None
        self._note = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="memory-title")
        yield Static("", id="memory-status")
        yield OptionList(compact=True, id="memory-rows")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()
        self.action_scan()

    def on_unmount(self) -> None:
        for token in (self._scan, self._detail):
            if token:
                token.cancel()

    @property
    def catalog(self) -> dict[str, Any]:
        return self._ephemeral if self._ephemeral is not None else self.inbox.view()

    def render_data(self) -> None:
        if not self.query("#memory-rows"):
            return
        catalog, pending = self.catalog, self.inbox.pending_memories
        if (
            self._listing is None
            or self._listed_catalog is not catalog
            or self._listed_pending != pending
        ):
            self._listed_catalog, self._listed_pending = catalog, pending
            listing = self._listing = memory_listing(catalog, pending)
            self._rows = listing.rows
            self.query_one("#memory-title", Static).update(
                Text(
                    f"Memories · {listing.undecided} to review · {len(listing.rows)} "
                    + ("entry" if len(listing.rows) == 1 else "entries")
                    + (" · read-only" if self.inbox.read_only else "")
                )
            )
            rows = self.query_one(OptionList)
            keep = highlighted_key(rows)
            rows.set_options(listing.options)
            rows.highlighted = next(
                (i for i, o in enumerate(listing.options) if keep and o.id == keep),
                next((i for i, o in enumerate(listing.options) if not o.disabled), None),
            )
        listing = self._listing
        status = [self._note] if self._note else []
        if self.inbox.unsaved_memory_drafts:
            status.append(
                f"{plural(len(self.inbox.unsaved_memory_drafts), 'retained draft')} · v to view and copy"
            )
        if self._rows and not self._note:
            status.append("Enter reads the selected entry")
        if listing.diagnostics:
            status.append(
                f"{plural(listing.diagnostics, 'source or store notice')} · s for details"
            )
        if not self._rows and not self._note:
            status.append(
                "No entries in available stores."
                if listing.diagnostics
                else "No native memory entries."
            )
        self.query_one("#memory-status", Static).update(Text(terminal_text(" · ".join(status))))

    def action_scan(self) -> None:
        if self.inbox.leaving:
            return
        if self._scan or self._ticket is not None:
            self._queued_scan = True
            return
        self._note = "Scanning native stores…"
        path, started = Path(self.inbox.registry_path), self.inbox.now_iso()
        previous, read_only = self._ephemeral, self.inbox.read_only
        self._scan = self.inbox.run_job(
            lambda _token, _progress: scan_catalog(
                path, started, previous, read_only=read_only, sources=self.inbox.memory.sources
            ),
            name="rl-memory-scan",
            on_result=self._scanned,
        )
        self.render_data()

    def _scanned(self, result: Done[dict[str, Any]] | Failed) -> None:
        token, self._scan = self._scan, None
        if not self.is_mounted or token is None or token.is_cancelled():
            return
        if isinstance(result, Failed):
            self._note = f"Scan unavailable: {result.error}"
        elif self.inbox.read_only:
            self._ephemeral, self._note = result.value, ""
        else:
            batch = self.inbox.submit_batch([MemoryScanOp(result.value)], label="scan memories")
            if batch:
                self._ticket = batch.tickets[0][0]
                return
            self._note = "Scan was not saved"
        self._settled()

    def _settled(self) -> None:
        self.render_data()
        if self._queued_scan:
            self._queued_scan = False
            self.action_scan()

    def op_finished(self, event: Finished) -> None:
        if self._ticket == event.ticket:
            self._ticket = None
            self._note = "" if event.outcome.ok else event.outcome.message
            self._settled()
        elif isinstance(event.op, (MemoryDecisionOp, MemoryRecoveryOp)):
            self.action_scan()

    def ops_dropped(self, tickets: Sequence[int]) -> None:
        if self._ticket in tickets:
            self._ticket = None
            self._note = "Scan cancelled"
            self.render_data()

    def action_view(self) -> None:
        row = self._rows.get(highlighted_key(self.query_one(OptionList)) or "")
        if not row or self._detail:
            return
        reg = self.catalog
        self._note = "Reading native entry…"
        self.render_data()
        self._detail = self.inbox.run_job(
            lambda _t, _p: memory_scan.read_record(reg, row, sources=self.inbox.memory.sources),
            name="rl-memory-detail",
            on_result=lambda result: self._open(result, entry_metadata(row, reg)),
        )

    def _open(self, result: Done[MemoryRecord] | Failed, metadata: dict[str, Any]) -> None:
        token, self._detail = self._detail, None
        if not token or token.is_cancelled() or not self.is_mounted:
            return
        self._note = str(result.error) if isinstance(result, Failed) else ""
        self.render_data()
        if isinstance(result, Done) and self.app.screen is self:
            self.app.push_screen(MemoryDetailScreen(result.value, metadata.get("decision") or {}))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.action_view()

    def action_sources(self) -> None:
        self.app.push_screen(SourceScreen(self.catalog))

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "retained" and not self.inbox.unsaved_memory_drafts:
            return False
        return super().check_action(action, parameters)

    def action_retained(self) -> None:
        if self.inbox.unsaved_memory_drafts:
            self.app.push_screen(ReadDraftModal(tuple(self.inbox.unsaved_memory_drafts)))

    def action_recovery(self) -> None:
        self.app.push_screen(RecoveryScreen())

    def action_cursor_down(self) -> None:
        self.query_one(OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(OptionList).action_cursor_up()


_DRAFT_LINE_BREAK = re.compile(r"(\r\n|\r|\n)")
_DRAFT_CONTROL_DISPLAY = str.maketrans(
    {
        **{chr(code): chr(0x2400 + code) for code in range(32) if code != ord("\t")},
        "\x7f": "␡",
        **{chr(code): "�" for code in (*range(128, 160), 0x2028, 0x2029)},
    }
)


class _DraftHistoryText(str):
    # Textual passes replaced_text back to the document on undo. Adjacent CR and LF may be two rows.
    parts: tuple[str, ...]

    def __new__(cls, parts: list[str]) -> Self:
        value = super().__new__(cls, "".join(parts))
        value.parts = tuple(parts)
        return value


class _DraftDocument(Document):
    def __init__(self, text: str) -> None:
        super().__init__("")
        self._cached_text: str | None = text
        parts = _DRAFT_LINE_BREAK.split(text)
        self._raw_lines = parts[::2]
        self._line_endings = parts[1::2]
        self._lines = [line.translate(_DRAFT_CONTROL_DISPLAY) for line in self._raw_lines]

    @property
    def text(self) -> str:
        if self._cached_text is None:
            self._cached_text = self.get_text_range(self.start, self.end)
        return self._cached_text

    def raw_line(self, index: int) -> str:
        return self._raw_lines[index]

    def _range_parts(self, top: Location, bottom: Location) -> list[str]:
        (top_row, first), (bottom_row, last) = top, bottom
        if top_row == bottom_row:
            return [self._raw_lines[top_row][first:last]]
        parts = [""] * (2 * (bottom_row - top_row) + 1)
        parts[::2] = self._raw_lines[top_row : bottom_row + 1]
        parts[1::2] = self._line_endings[top_row:bottom_row]
        parts[0] = parts[0][first:]
        parts[-1] = parts[-1][:last]
        return parts

    def get_text_range(self, start: Location, end: Location) -> str:
        top, bottom = sorted((min(start, self.end), min(end, self.end)))
        return "".join(self._range_parts(top, bottom))

    def replace_range(self, start: Location, end: Location, text: str) -> EditResult:
        top, bottom = sorted((min(start, self.end), min(end, self.end)))
        removed = self._range_parts(top, bottom)
        replaced = removed[0] if len(removed) == 1 else _DraftHistoryText(removed)
        top_row, top_col = top
        bottom_row, bottom_col = bottom
        parts = (
            list(text.parts)
            if isinstance(text, _DraftHistoryText)
            else _DRAFT_LINE_BREAK.split(text)
        )
        raw_lines = parts[::2]
        raw_lines[0] = self._raw_lines[top_row][:top_col] + raw_lines[0]
        end_location = top_row + len(raw_lines) - 1, len(raw_lines[-1])
        raw_lines[-1] += self._raw_lines[bottom_row][bottom_col:]
        self._raw_lines[top_row : bottom_row + 1] = raw_lines
        self._line_endings[top_row:bottom_row] = parts[1::2]
        self._lines[top_row : bottom_row + 1] = [
            line.translate(_DRAFT_CONTROL_DISPLAY) for line in raw_lines
        ]
        self._cached_text = None
        return EditResult(end_location, replaced)


class _DraftEditor(TextArea):
    document: _DraftDocument

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        raw = self.document.raw_line(line_index)
        if raw != line.plain:
            for column, char in enumerate(raw):
                if ord(char) in _DRAFT_CONTROL_DISPLAY:
                    line.stylize("bold underline", column, column + 1)
        return line

    @property
    def control_hint(self) -> str:
        row, column = self.cursor_location
        char = self.document.raw_line(row)[column : column + 1]
        if char and ord(char) in _DRAFT_CONTROL_DISPLAY:
            return f"Control character at cursor · U+{ord(char):04X}"
        return "Underlined symbols mark control characters"

    def _set_document(self, text: str, language: str | None) -> None:
        self.document = _DraftDocument(text)
        self.wrapped_document = WrappedDocument(self.document, tab_width=self.indent_width)
        self.navigator = DocumentNavigator(self.wrapped_document)
        self.move_cursor((0, 0))
        self._rewrap_and_refresh_virtual_size()


class DraftModal(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "approve", "save and remove", priority=True),
        Binding("escape", "cancel", "cancel", priority=True),
    ]
    DEFAULT_CSS = """
    DraftModal { background: $surface; }
    DraftModal TextArea { height: 1fr; }
    """

    def __init__(self, draft: str) -> None:
        super().__init__()
        self.draft = draft

    def compose(self) -> ComposeResult:
        yield Static("Edit harvested Markdown · Ctrl+S to review removal · Esc to cancel")
        yield _DraftEditor(self.draft, soft_wrap=True, id="memory-draft")
        yield Static(id="draft-control-hint")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(_DraftEditor).focus()
        self._show_control_hint()

    @on(TextArea.SelectionChanged)
    @on(TextArea.Changed)
    def _show_control_hint(self) -> None:
        self.query_one("#draft-control-hint", Static).update(
            self.query_one(_DraftEditor).control_hint
        )

    def action_approve(self) -> None:
        value = self.query_one(TextArea).text
        if value.strip():
            self.dismiss(value)
        else:
            self.notify("Enter nonempty Markdown to save", severity="warning")

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReadDraftModal(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "copy", "copy all", priority=True),
        Binding("r", "retry", "review retry", priority=True),
        Binding("n", "next", "next draft", priority=True),
        Binding("escape", "close", "back", priority=True),
    ]
    DEFAULT_CSS = (
        "ReadDraftModal { background: $surface; } ReadDraftModal TextArea { height: 1fr; }"
    )

    def __init__(self, drafts: tuple[MemoryDecisionOp, ...], *, frozen: bool = False) -> None:
        super().__init__()
        self.drafts = drafts
        self.frozen = frozen
        self.index = len(drafts) - 1

    def compose(self) -> ComposeResult:
        yield Static(id="draft-origin")
        yield _DraftEditor(read_only=True, id="memory-draft")
        yield Static(
            "Removal already approved · r to review retry"
            if self.frozen
            else "Copy the draft, then reopen the current entry and review it before submitting."
        )
        yield Footer()

    def on_mount(self) -> None:
        self.show_draft()
        self.query_one(_DraftEditor).focus()

    def show_draft(self) -> None:
        op = self.drafts[self.index]
        ref = op.ref
        title = (
            "Frozen approval"
            if self.frozen
            else f"Retained draft {self.index + 1}/{len(self.drafts)}"
        )
        self.query_one("#draft-origin", Static).update(
            Text(
                terminal_text(
                    f"{title} · {ref.source} · {ref.source_ref}\nFingerprint {ref.fingerprint} · "
                    f"incarnation {ref.incarnation}"
                    + (f" · queue token {op.expected_token}" if op.expected_token else "")
                )
            )
        )
        self.query_one(_DraftEditor).load_text(op.markdown or "")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "retry":
            return self.frozen
        if action == "next":
            return len(self.drafts) > 1
        return super().check_action(action, parameters)

    def action_next(self) -> None:
        self.index = (self.index + 1) % len(self.drafts)
        self.show_draft()

    def action_retry(self) -> None:
        if self.frozen:
            self.dismiss(True)

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.drafts[self.index].markdown or "")
        self.notify("Copied exact Markdown")

    def action_close(self) -> None:
        self.dismiss(False)


class MemoryDetailScreen(InboxScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("h", "harvest", "harvest"),
        Binding("e", "draft_native", "edit native text"),
        Binding("d", "discard", "discard"),
        Binding("c", "cancel_job", "cancel draft"),
    ]

    def __init__(self, record: MemoryRecord, decision: dict[str, Any]) -> None:
        super().__init__()
        self.record = record
        self.decision = decision
        self._generation: CancelToken | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            Text(
                terminal_text(
                    f"{self.record.name} · {self.record.ref.source}"
                    + (" · read-only" if self.inbox.read_only else "")
                )
            )
        )
        yield Static("", id="detail-status")
        yield Log(id="memory-content", auto_scroll=False)
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one(Log)
        ref = self.record.ref
        location = (
            ref.source_ref if ref.source == "claude-code" else f"{ref.store_id} · {ref.source_ref}"
        )
        log.write(terminal_text(f"{location}\n\n{self.record.content}"))
        log.focus()
        self.status(self.record.mutation_block or self.decision.get("sourceState") or "")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"harvest", "draft_native", "discard"}:
            if self._blocked_reason("discard" if action == "discard" else "harvest"):
                return False
            if action == "draft_native" and not self._can_draft_native():
                return False
        return super().check_action(action, parameters)

    def on_unmount(self) -> None:
        self.action_cancel_job()

    def status(self, text: str) -> None:
        self.query_one("#detail-status", Static).update(Text(terminal_text(text)))

    def _blocked_reason(self, decision: str) -> str | None:
        if self.inbox.read_only:
            return "Read-only · memory decisions are disabled"
        if self.record.ref.memory_id in self.inbox.pending_memories:
            return "A decision is queued"
        if self.decision and self.decision.get("decision") != decision:
            return f"This revision already has a {self.decision.get('decision')} decision"
        return self.record.mutation_block

    def _allowed(self, decision: str) -> bool:
        reason = self._blocked_reason(decision)
        if reason:
            self.status(reason)
        return not reason and self._generation is None

    def _can_draft_native(self) -> bool:
        return not self.decision

    def action_draft_native(self) -> None:
        if self._can_draft_native():
            self._open_review("harvest", native=True)

    def action_harvest(self) -> None:
        self._open_review("harvest")

    def action_discard(self) -> None:
        self._open_review("discard")

    def _open_review(self, action: Literal["harvest", "discard"], *, native: bool = False) -> None:
        if not self._allowed(action):
            return
        ref, path = self.record.ref, Path(self.inbox.registry_path)
        service, projected = self.inbox.memory, self.decision

        def read() -> RemovalReview:
            review = service.review_removal(
                ref, path, action, load_config().memory_recovery_retention_days
            )
            if projected and review.operation_id != projected.get("operationId"):
                raise ValueError("Saved decision changed; refresh Memories and review again")
            if native and review.route != "new":
                raise ValueError("This entry has a saved decision; reopen it for review")
            return review

        def loaded(result: Done[RemovalReview] | Failed) -> None:
            if self._generation is not job or job.is_cancelled():
                return
            self._generation = None
            if not self.is_mounted or self.app.screen is not self:
                return
            if isinstance(result, Failed):
                self.status(f"Cannot open approval: {result.error}")
                return
            self.status("")
            review = result.value
            if review.queue:
                body, token = review.queue
                if review.frozen:
                    op = MemoryDecisionOp(ref, "harvest", body, token, review.retention, review)
                    self.app.push_screen(
                        ReadDraftModal((op,), frozen=True),
                        lambda yes: self._confirm(review, None) if yes else None,
                    )
                else:
                    self._edit(body, review)
            elif review.route != "new":
                self._confirm(review, None)
            elif native:
                self._edit(self.record.content, review)
            else:
                self._generate(review)

        self.status("Checking saved approval…")
        job = self.inbox.run_job(lambda _t, _p: read(), name="rl-memory-approval", on_result=loaded)
        self._generation = job

    def _generate(self, review: RemovalReview) -> None:
        model = self.inbox.model_for("harvest")
        if model is None or not tool_free_completion(model):
            self.app.push_screen(
                AgentPickModal(
                    "No tool-free harvest model. Escape then e edits the native text. Review in which agent?",
                    resume.launchable_agents(),
                ),
                self._handoff,
            )
            return
        self.status("Drafting harvested Markdown… · c cancels")
        job = self.inbox.run_job(
            memory_harvest_job(self.record, model),
            name="rl-memory-harvest",
            on_result=lambda result: (
                self._drafted(result, review) if self._generation is job else None
            ),
        )
        self._generation = job

    def _handoff(self, agent: str | None) -> None:
        if agent:
            self.inbox.start_memory_handoff(agent, self.record.ref)

    def _drafted(self, result: Done[HarvestResult] | Failed, review: RemovalReview) -> None:
        job = self._generation
        if not job or job.is_cancelled() or not self.is_mounted:
            return
        self._generation = None
        if isinstance(result, Failed):
            self.status(f"Draft failed: {result.error} · h to retry · e to edit native text")
        elif (
            isinstance(result.value, HarvestReady)
            and result.value.markdown
            and self.app.screen is self
        ):
            self.status("")
            self._edit(result.value.markdown, review)
        else:
            self.status("No draft returned · h to retry · e to edit native text")

    def _edit(self, draft: str, review: RemovalReview) -> None:
        self.app.push_screen(
            DraftModal(draft),
            callback=lambda value: self._confirm(review, value) if value is not None else None,
        )

    def action_cancel_job(self) -> None:
        if self._generation:
            self._generation.cancel()
            self._generation = None
            if self.is_mounted and self.query("#detail-status"):
                self.status("Draft cancelled")

    def _confirm(self, review: RemovalReview, markdown: str | None) -> None:
        if not self._allowed(review.decision):
            return
        try:
            approved = review.approve(markdown)
        except ValueError as exc:
            if markdown is not None:
                self._edit(markdown, review)
            self.notify(terminal_text(str(exc)), severity="warning", markup=False)
            return
        token = approved.queue[1] if approved.queue else None
        self._show_confirmation(
            MemoryDecisionOp(
                approved.ref, approved.decision, markdown, token, approved.retention, approved
            )
        )

    def _show_confirmation(self, op: MemoryDecisionOp) -> None:
        days = op.retention_days
        retention = f"Recovery for {days} days." if days else "No recovery copy after removal."
        verb = (
            (
                "Save approved Markdown and remove"
                if op.markdown is not None
                else "Use saved approval and remove"
            )
            if op.decision == "harvest"
            else "Discard"
        )
        evidence = ""
        if op.review:
            if op.review.destinations:
                evidence = (
                    " Verified destinations: "
                    + ", ".join(path for path, _ in op.review.destinations)
                    + "."
                )
            elif op.review.receipt_id:
                evidence = f" Publication receipt {op.review.receipt_id} (approved digest {op.review.approval_hash})."
            elif op.review.queue:
                evidence = f" Queue token {op.review.queue[1]}."
        message = Text(
            terminal_text(f"{verb} native entry {self.record.name}?{evidence} {retention} [y/n]")
        )
        self.app.push_screen(ConfirmModal(message), lambda yes: self._submit(op) if yes else None)

    def _submit(self, op: MemoryDecisionOp) -> None:
        if self._allowed(op.decision) and self.inbox.submit(
            [op], label=f"{op.decision} native memory"
        ):
            self.app.pop_screen()
        else:
            self.inbox.retain_memory_draft(op)


class RecoveryScreen(InboxScreen):
    BINDINGS: ClassVar[list[BindingType]] = [
        *nav_bindings(),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("r", "restore", "restore"),
        Binding("c", "recover", "retry"),
        Binding("p", "prune", "prune expired"),
        Binding("ctrl+r", "refresh_list", "refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.listing = RecoveryListing((), frozenset(), ())

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Memory recovery" + (" · read-only" if self.inbox.read_only else ""))
        yield Static("", id="recovery-status")
        yield OptionList(id="recovery-rows")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()
        self.action_refresh_list()

    def action_cursor_down(self) -> None:
        self.query_one("#recovery-rows", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#recovery-rows", OptionList).action_cursor_up()

    def action_refresh_list(self) -> None:
        if self.inbox.leaving:
            return
        self.query_one("#recovery-status", Static).update("Reading recovery history…")
        self.inbox.refresh_recovery()

    def show_recovery(self, result: Done[RecoveryListing] | Failed) -> None:
        self.listing = (
            result.value if isinstance(result, Done) else RecoveryListing((), frozenset(), ())
        )
        if isinstance(result, Failed):
            status = f"Recovery unavailable: {result.error}"
        elif not self.listing.rows:
            status = "No recovery operations."
        else:
            status = (
                f"{len(self.listing.retry)} to retry · {len(self.listing.expired)} expired copies"
            )
        self.query_one("#recovery-status", Static).update(Text(terminal_text(status)))
        rows = self.query_one(OptionList)
        set_rows(
            rows,
            [(r["operationId"], Text(terminal_text(self._label(r)))) for r in self.listing.rows],
            keep=highlighted_key(rows),
        )

    def _label(self, row: dict[str, Any]) -> str:
        expiry = row["recoveryExpiresAt"]
        state = "restoring" if row["restoring"] else row["sourceState"]
        recovery = f"until {expiry}" if row["restorable"] else "unavailable"
        if row["operationId"] in self.listing.expired:
            recovery = f"expired {expiry}"
        newer = " · superseded" if row["supersededBy"] else ""
        diagnostic = f" · {row['diagnostic']}" if row.get("diagnostic") else ""
        return f"{row['name']} · {row['source']} · {row['decision']} / {state}{newer}\n  recovery {recovery} · consolidation {row['cleanup']}{diagnostic}"

    def op_finished(self, event: Finished) -> None:
        if isinstance(event.op, (MemoryDecisionOp, MemoryRecoveryOp, MemoryPruneOp)):
            self.action_refresh_list()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"restore", "recover", "prune"} and self.inbox.read_only:
            return False
        return super().check_action(action, parameters)

    def _selected(self) -> dict[str, Any] | None:
        key = highlighted_key(self.query_one(OptionList))
        return next((r for r in self.listing.rows if r["operationId"] == key), None)

    def action_restore(self) -> None:
        row = self._selected()
        if row and row["restorable"] and not self.inbox.read_only:
            message = Text(terminal_text(f"Restore {row['name']} to {row['sourceRef']}? [y/n]"))
            op = MemoryRecoveryOp(row["operationId"], True)
            self._confirm_action(message, op, "restore memory")

    def _confirm_action(
        self, message: Text, op: MemoryRecoveryOp | MemoryPruneOp, label: str
    ) -> None:
        def confirmed(yes: bool | None) -> None:
            if yes:
                self.inbox.submit([op], label=label)

        self.app.push_screen(ConfirmModal(message), confirmed)

    def action_recover(self) -> None:
        row = self._selected()
        if row and row["operationId"] in self.listing.retry and not self.inbox.read_only:
            self.inbox.submit(
                [MemoryRecoveryOp(row["operationId"], False)], label="retry memory operation"
            )

    def action_prune(self) -> None:
        if not self.listing.expired or self.inbox.read_only:
            return
        message = Text(
            f"Permanently prune all expired recovery copies ({len(self.listing.expired)} currently expired)? [y/n]"
        )
        self._confirm_action(message, MemoryPruneOp(), "prune expired recovery")
