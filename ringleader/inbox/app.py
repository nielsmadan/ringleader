from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static

from .. import config, export, harvests, notify, overview, prune, registry
from ..commands import archive as archive_cmd
from ..commands import harvest as harvest_cmd
from ..commands import roundup
from ..config import load_config
from ..fuzzytime import parse_duration
from ..models import active_model
from ..paths import archive_root, default_registry


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _today_str() -> str:
    return datetime.now(UTC).date().isoformat()


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _row_label(t: dict) -> str:
    project = overview.group_label(t)
    desc = t.get("whatItIs") or t.get("title") or t.get("aiTitle") or (t.get("sessionId") or "")[:8]
    return f"{project}: {desc}"


class SnoozeModal(ModalScreen[str]):
    """A prompt for a fuzzy snooze duration (e.g. `3d`, `next monday`)."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "cancel", show=False)]

    DEFAULT_CSS = """
    SnoozeModal {
        align: center middle;
    }
    #snooze-box {
        grid-size: 1;
        grid-gutter: 1;
        padding: 1 2;
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield Grid(
            Label("snooze for how long? (3d, 2w, tomorrow, next monday…)"),
            Input(placeholder="3d", id="snooze-input"),
            Label("", id="snooze-error"),
            id="snooze-box",
        )

    def on_mount(self) -> None:
        self.query_one("#snooze-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        try:
            until = parse_duration(text, datetime.now(UTC))
        except ValueError as e:
            self.query_one("#snooze-error", Label).update(str(e))
            return
        self.dismiss(until)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RoundupConfirmModal(ModalScreen[bool]):
    """Pre-flight prompt: sessions look stale, offer to run roundup now."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "confirm", "yes", show=False),
        Binding("n", "cancel", "no", show=False),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    DEFAULT_CSS = """
    RoundupConfirmModal {
        align: center middle;
    }
    #roundup-confirm-box {
        grid-size: 1;
        grid-gutter: 1;
        padding: 1 2;
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Grid(
            Label(self._message, id="roundup-confirm-message"),
            id="roundup-confirm-box",
        )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class RoundupProgressModal(ModalScreen):
    """Shown while the pre-flight roundup worker runs; no key bindings — it
    dismisses itself once the worker finishes."""

    DEFAULT_CSS = """
    RoundupProgressModal {
        align: center middle;
    }
    #roundup-progress-box {
        grid-size: 1;
        grid-gutter: 1;
        padding: 1 2;
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield Grid(
            Label("running roundup…", id="roundup-progress-message"),
            id="roundup-progress-box",
        )


class DetailModal(ModalScreen):
    """A read-only detail view of one session; `h` (or escape) pops back."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("h", "pop", "back", show=False),
        Binding("escape", "pop", "back", show=False),
    ]

    def __init__(self, thread: dict) -> None:
        super().__init__()
        self._thread = thread

    def compose(self) -> ComposeResult:
        t = self._thread
        status = overview.effective_status(t, _now_iso())
        lines = [
            f"project: {overview.group_label(t)}",
            f"status: {status}",
            f"what it is: {t.get('whatItIs') or '(none)'}",
            f"last prompt: {t.get('lastPrompt') or '(none)'}",
            f"branch: {t.get('gitBranch') or '(none)'}",
            "",
            "--- conversation ---",
            "",
            export.render_conversation(t["file"]),
        ]
        yield VerticalScroll(Static("\n".join(lines)))

    def action_pop(self) -> None:
        self.app.pop_screen()


class InboxApp(App):
    """The inbox-zero TUI: blitz through dormant sessions with j/k + actions."""

    TITLE = "rl inbox"

    _TABS: ClassVar[tuple[str, ...]] = ("inbox", "selected", "harvesting")

    DEFAULT_CSS = """
    InboxApp > #empty-state {
        width: 100%;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
        display: none;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("up", "cursor_up", "up", show=False),
        # priority=True so this beats Screen's default tab->focus_next binding.
        Binding("tab", "next_tab", "next tab", show=False, priority=True),
        Binding("shift+tab", "prev_tab", "prev tab", show=False, priority=True),
        Binding("h", "harvest", "harvest"),
        Binding("d", "delete", "delete"),
        Binding("s", "snooze", "snooze"),
        Binding("K", "keep", "keep"),
        # ListView consumes Enter itself (posts Selected); this never fires directly —
        # on_list_view_selected dispatches per-tab instead. Kept for the footer/help text.
        Binding("enter", "select_day", "select today", show=False),
        Binding(";", "detail", "detail"),
        Binding("u", "undo", "undo"),
        Binding("a", "archive", "archive"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, registry_path: str | Path | None = None) -> None:
        super().__init__()
        self.registry_path = (
            str(registry_path) if registry_path is not None else str(default_registry())
        )
        self._reg: dict = registry.empty_registry()
        self._active_tab: str = "inbox"
        self._inbox: list[dict] = []
        self._selected: list[dict] = []
        self._harvesting: list[dict] = []
        self._harvest_by_session: dict[str, harvests.Harvest] = {}
        self._staged_deletes: set[str] = set()
        self._undo_stack: list[tuple[int, dict]] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView(id="inbox-list")
        yield Label("", id="empty-state")
        yield Footer()

    async def on_mount(self) -> None:
        self._reg = registry.load_registry(self.registry_path)
        cfg = config.load_config()
        if self._roundup_is_stale(cfg):
            self._preflight_roundup()
            return
        self._refresh_tab_data(self._active_tab)
        await self._render_active_tab()

    def _roundup_is_stale(self, cfg) -> bool:
        last = self._reg.get("lastRoundupAt")
        if not last:
            return True
        age_hours = (datetime.now(UTC) - _parse_iso(last)).total_seconds() / 3600
        return age_hours >= cfg.roundup_stale_hours

    # push_screen_wait requires an active worker context, so the confirm/progress
    # dance (and the render that must follow either branch) all live in one worker.
    @work
    async def _preflight_roundup(self) -> None:
        confirmed = await self.push_screen_wait(
            RoundupConfirmModal("Sessions may be stale — refresh, prune & enrich now? [y/n]")
        )
        if confirmed:
            self.push_screen(RoundupProgressModal())
            try:
                roundup.cmd_roundup(["--registry", self.registry_path, "--yes"])
            except Exception as e:
                self.notify(f"roundup failed: {e}", title="rl inbox", severity="error")
            else:
                notify.notify("ringleader", "roundup complete")
            finally:
                self.pop_screen()
            self._reg = registry.load_registry(self.registry_path)
        self._refresh_tab_data(self._active_tab)
        await self._render_active_tab()

    def _compute_inbox(self) -> list[dict]:
        now = _now_iso()
        today = _today_str()
        inbox = overview.triage_inbox(self._reg["threads"], now)
        # staged deletes aren't in the registry yet (that happens on quit), so a
        # tab-away-and-back recompute must not resurrect them.
        return [
            t
            for t in inbox
            if t.get("selectedForDay") != today and t["sessionId"] not in self._staged_deletes
        ]

    def _compute_selected(self) -> list[dict]:
        return registry.selected_today(self._reg, _today_str())

    def _compute_harvesting(self) -> list[dict]:
        # archiving pops a thread out of reg["threads"] entirely, so anything still
        # here with decision=="harvested" is by definition still live.
        return [t for t in self._reg["threads"] if t.get("decision") == "harvested"]

    def _refresh_tab_data(self, tab: str) -> None:
        if tab == "inbox":
            self._inbox = self._compute_inbox()
        elif tab == "selected":
            self._selected = self._compute_selected()
        elif tab == "harvesting":
            self._harvesting = self._compute_harvesting()
            self._harvest_by_session = {h.session_id: h for h in harvests.load_harvests()}

    def _active_rows(self) -> list[dict]:
        return getattr(self, f"_{self._active_tab}")

    def _row_label_for(self, t: dict) -> str:
        label = _row_label(t)
        if self._active_tab == "harvesting":
            h = self._harvest_by_session.get(t["sessionId"])
            preview = ""
            if h is not None:
                # skip the markdown title/heading lines (`# ...`, `## Summary`) to
                # surface actual digest prose in the row.
                preview = next(
                    (
                        ln.strip()
                        for ln in h.content.splitlines()
                        if ln.strip() and not ln.strip().startswith("#")
                    ),
                    "",
                )
            if preview:
                label = f"{label} — {preview[:80]}"
        return label

    async def _render_active_tab(self) -> None:
        rows = self._active_rows()
        self.sub_title = f"[{self._active_tab}] ({len(rows)})"
        list_view = self._list_view()
        await list_view.clear()
        await list_view.extend(ListItem(Label(self._row_label_for(t))) for t in rows)
        if rows:
            list_view.index = 0
        self._render_empty_state(rows)

    def _render_empty_state(self, rows: list[dict]) -> None:
        empty_label = self.query_one("#empty-state", Label)
        list_view = self._list_view()
        if rows:
            list_view.display = True
            empty_label.display = False
            return
        list_view.display = False
        empty_label.display = True
        message = (
            "📭 inbox zero — nothing to triage" if self._active_tab == "inbox" else "(nothing here)"
        )
        empty_label.update(message)

    def _list_view(self) -> ListView:
        return self.query_one("#inbox-list", ListView)

    def _current(self) -> tuple[int | None, dict | None]:
        list_view = self._list_view()
        idx = list_view.index
        rows = self._active_rows()
        if idx is None or not (0 <= idx < len(rows)):
            return None, None
        return idx, rows[idx]

    async def _remove_row(self, idx: int) -> None:
        del self._active_rows()[idx]
        await self._list_view().pop(idx)
        self._render_empty_state(self._active_rows())

    async def _insert_row(self, idx: int, t: dict) -> None:
        rows = self._active_rows()
        idx = max(0, min(idx, len(rows)))
        rows.insert(idx, t)
        list_view = self._list_view()
        await list_view.insert(idx, [ListItem(Label(self._row_label_for(t)))])
        if list_view.index is None:
            list_view.index = idx
        self._render_empty_state(rows)

    # -- cursor movement --------------------------------------------------

    def action_cursor_down(self) -> None:
        self._list_view().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._list_view().action_cursor_up()

    # -- tabs --------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # priority bindings reach run_action even with a modal on top of the screen
        # stack; gate the tab switch here so it can't steal `tab` from e.g. the
        # snooze Input.
        return not (action in ("next_tab", "prev_tab") and len(self.screen_stack) > 1)

    async def action_next_tab(self) -> None:
        await self._switch_tab(1)

    async def action_prev_tab(self) -> None:
        await self._switch_tab(-1)

    async def _switch_tab(self, direction: int) -> None:
        idx = self._TABS.index(self._active_tab)
        self._active_tab = self._TABS[(idx + direction) % len(self._TABS)]
        self._refresh_tab_data(self._active_tab)
        await self._render_active_tab()

    # -- harvest ------------------------------------------------------------

    @work(exclusive=True)
    async def action_harvest(self) -> None:
        if self._active_tab != "inbox":
            return
        idx, t = self._current()
        if t is None:
            return
        model = active_model(load_config())
        if model is None:
            self.notify(
                "no model configured — set one with `rl config`",
                title="rl inbox",
                severity="warning",
            )
            return
        try:
            condensed = export.render_conversation(t["file"])
            data = model.complete_json(
                harvest_cmd._prompt(t, condensed), harvest_cmd.HARVEST_SCHEMA
            )

            def _has_content(key: str) -> bool:
                return any(i for i in (data.get(key) or []) if i)

            # per-item filtering, matching _render_md's own [i for i in ... if i]
            empty = not (data.get("summary") or "").strip() and not any(
                _has_content(k) for k in ("gotchas", "decisions", "whatChanged")
            )
            if empty:
                archive_cmd.archive_thread(self._reg, t, archive_root_path=archive_root())
            else:
                md = harvest_cmd._render_md(t, data)
                path = harvests.write_harvest(
                    session_id=t["sessionId"],
                    name=t.get("title") or t.get("suggestedName") or "",
                    folder_path=t.get("gitRoot") or t.get("project"),
                    repo_url=t.get("gitRemote"),
                    harvested_at=_now_iso(),
                    content=md,
                )
                registry.mark_harvested(self._reg, t["sessionId"], [str(path)])
        except Exception as e:
            self.notify(f"harvest failed: {e}", title="rl inbox", severity="error")
            return
        registry.save_registry(self.registry_path, self._reg)
        await self._remove_row(idx)

    # -- delete / undo --------------------------------------------------

    async def action_delete(self) -> None:
        if self._active_tab != "inbox":
            return
        idx, t = self._current()
        if t is None:
            return
        self._staged_deletes.add(t["sessionId"])
        self._undo_stack.append((idx, t))
        await self._remove_row(idx)

    async def action_undo(self) -> None:
        if self._active_tab == "selected":
            await self.action_unselect()
            return
        if self._active_tab != "inbox":
            return
        if not self._undo_stack:
            return
        idx, t = self._undo_stack.pop()
        self._staged_deletes.discard(t["sessionId"])
        await self._insert_row(idx, t)

    # -- snooze --------------------------------------------------------

    @work
    async def action_snooze(self) -> None:
        if self._active_tab != "inbox":
            return
        idx, t = self._current()
        if t is None:
            return
        until = await self.push_screen_wait(SnoozeModal())
        if until is None:
            return
        registry.snooze_thread(self._reg, t["sessionId"], _now_iso(), until=until)
        registry.save_registry(self.registry_path, self._reg)
        await self._remove_row(idx)

    # -- keep long-term ----------------------------------------------------

    async def action_keep(self) -> None:
        """Keep a session long-term: it leaves the inbox for good, unlike snooze
        (which returns on a date) or leaving it open (which returns on new activity)."""
        if self._active_tab != "inbox":
            return
        idx, t = self._current()
        if t is None:
            return
        registry.keep_thread(self._reg, t["sessionId"], _now_iso())
        registry.save_registry(self.registry_path, self._reg)
        await self._remove_row(idx)

    # -- select for today --------------------------------------------------

    async def action_select_day(self) -> None:
        if self._active_tab != "inbox":
            return
        idx, t = self._current()
        if t is None:
            return
        registry.select_for_day(self._reg, t["sessionId"], _today_str())
        registry.save_registry(self.registry_path, self._reg)
        await self._remove_row(idx)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if self._active_tab == "inbox":
            await self.action_select_day()
        elif self._active_tab == "selected":
            await self.action_unselect()

    # -- selected tab: un-select --------------------------------------------

    async def action_unselect(self) -> None:
        if self._active_tab != "selected":
            return
        idx, t = self._current()
        if t is None:
            return
        registry.clear_selected(self._reg, t["sessionId"])
        registry.save_registry(self.registry_path, self._reg)
        await self._remove_row(idx)

    # -- harvesting tab: archive ---------------------------------------------

    async def action_archive(self) -> None:
        if self._active_tab != "harvesting":
            return
        idx, t = self._current()
        if t is None:
            return
        archive_cmd.archive_thread(self._reg, t, archive_root_path=str(archive_root()))
        registry.save_registry(self.registry_path, self._reg)
        self._harvest_by_session.pop(t["sessionId"], None)
        await self._remove_row(idx)

    # -- detail --------------------------------------------------------

    def action_detail(self) -> None:
        if self._active_tab != "inbox":
            return
        _idx, t = self._current()
        if t is None:
            return
        self.push_screen(DetailModal(t))

    # -- exit --------------------------------------------------------

    async def action_quit(self) -> None:
        await self._apply_staged_deletes()
        self.exit()

    async def _apply_staged_deletes(self) -> None:
        candidates = [t for t in self._reg["threads"] if t["sessionId"] in self._staged_deletes]
        deleted = prune.delete_sessions(self._reg, candidates, _now_iso())
        registry.save_registry(self.registry_path, self._reg)
        print(f"deleting {deleted} session(s)")
