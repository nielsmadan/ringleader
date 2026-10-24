from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets import Footer, Header, Static, TextArea

from ... import export, overview, resume
from ...agents import source_of
from ...tui.background import CancelToken, Done, Failed, start_daemon
from ..base import InboxScreen
from ..state import short_path

TAIL_CHARS = 200_000


def detail_lines(thread: dict[str, Any], now_iso: str) -> list[str]:
    snoozed = (
        f"snoozed until {thread['snoozedUntil'][:10]}"
        if overview.snooze_active(thread, now_iso)
        else None
    )
    kept = f"kept since {str(thread['keptAt'])[:10]}" if thread.get("keptAt") else None
    fields = [
        ("project", short_path(thread.get("project"))),
        ("agent", resume.agent_name(source_of(thread))),
        ("status", overview.effective_status(thread, now_iso)),
        ("what it is", thread.get("whatItIs") or "—"),
        ("last prompt", thread.get("lastPrompt") or "—"),
        ("branch", thread.get("gitBranch") or "—"),
        ("session", thread.get("sessionId") or "—"),
        ("file", short_path(thread["file"]) if thread.get("file") else "—"),
        ("size", overview.human_size(thread.get("sizeBytes"))),
        ("messages", str(thread.get("messageCount") or 0)),
        ("activity", overview.age_human(thread.get("mtime"), now_iso)),
        *([("parked", " · ".join(p for p in (kept, snoozed) if p))] if kept or snoozed else []),
    ]
    return [f"{name:<12} {value}" for name, value in fields]


def tail_heading(thread: dict[str, Any]) -> str:
    return (
        f"showing the last {TAIL_CHARS // 1000} KB — rl data export "
        f"{thread.get('sessionId')} writes the whole conversation"
    )


def unavailable_reason(error: BaseException) -> str:
    if isinstance(error, FileNotFoundError):
        return "transcript not found"
    return f"could not read the transcript ({type(error).__name__})"


def conversation_job(
    thread: dict[str, Any],
) -> Callable[[CancelToken, Callable[[None], None]], str]:
    def job(_token: CancelToken, _progress: Callable[[None], None]) -> str:
        return export.render_tail(thread, TAIL_CHARS)

    return job


class ConversationLoaded(Message):
    def __init__(self, result: Done[str] | Failed) -> None:
        super().__init__()
        self.result = result


class ConversationView(TextArea):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "scroll_up", show=False),
        Binding("down", "scroll_down", show=False),
        Binding("pageup", "page_up", show=False),
        Binding("pagedown", "page_down", show=False),
        Binding("home", "scroll_home", show=False),
        Binding("end", "scroll_end", show=False),
    ]

    DEFAULT_CSS = """
    ConversationView {
        border: none;
        padding: 0 1;
        background: $background;
    }
    ConversationView:focus {
        border: none;
    }
    """


class DetailScreen(InboxScreen):
    DEFAULT_CSS = """
    DetailScreen #detail-title, DetailScreen #detail-head, DetailScreen #tail-heading {
        padding: 0 1;
    }
    DetailScreen #detail-head {
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    DetailScreen #tail-heading {
        margin-top: 1;
        color: $text-muted;
    }
    DetailScreen #conversation {
        height: 1fr;
    }
    """

    def __init__(self, thread: dict[str, Any]) -> None:
        super().__init__()
        self.thread = thread

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(Text(resume.label(self.thread), style="bold"), id="detail-title")
        yield Static("", id="detail-head")
        yield Static("", id="tail-heading")
        yield ConversationView(
            id="conversation",
            read_only=True,
            soft_wrap=True,
            show_cursor=False,
            highlight_cursor_line=False,
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#detail-head", Static).update(
            Text("\n".join(detail_lines(self.thread, self.inbox.now_iso())))
        )
        self._show_conversation("loading conversation…")
        start_daemon(
            conversation_job(self.thread),
            on_progress=lambda _update: None,
            on_result=self._loaded,
            name="rl-detail",
        )

    def _loaded(self, result: Done[str] | Failed) -> None:
        self.post_message(ConversationLoaded(result))

    def _show_conversation(self, text: str) -> None:
        view = self.query_one("#conversation", ConversationView)
        view.load_text(text)
        view.scroll_home(animate=False)

    def on_conversation_loaded(self, message: ConversationLoaded) -> None:
        match message.result:
            case Done(value=text):
                self.query_one("#tail-heading", Static).update(Text(tail_heading(self.thread)))
                self._show_conversation(text or "(empty conversation)")
            case Failed(error=error):
                self._show_conversation(f"conversation unavailable: {unavailable_reason(error)}")
