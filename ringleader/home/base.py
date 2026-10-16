from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Literal, cast

from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import Static

from ..enrichment import EnrichOutcome
from ..harvests import Harvest
from ..revalidate import Identity
from ..roundup import EnrichScope, PhasesResult, RoundupPlan, RoundupProgress
from .overlay import Hidden
from .writer import Finished, Snapshot

if TYPE_CHECKING:
    from .app import InboxApp


class ScreenTarget(Enum):
    HOME = "home"
    PROJECTS = "projects"
    RESUME = "resume"
    ROUNDUP = "roundup"
    PRUNE = "prune"
    HARVESTS = "harvests"
    USAGE = "usage"


@dataclass(frozen=True)
class AppState:
    snapshot: Snapshot
    overlay: Mapping[Identity, Hidden]
    live_ids: frozenset[Identity] | None
    harvests: tuple[Harvest, ...] | None
    harvest_problems: int
    enrich_model: str | None
    harvest_model: str | None
    scan_note: str | None
    version: int


type RoundupStage = Literal["phases", "enrich", "stamp", "done", "cancelled", "failed"]


@dataclass(frozen=True)
class RoundupRun:
    plan: RoundupPlan
    scope: EnrichScope
    stage: RoundupStage
    progress: RoundupProgress | None
    phases: PhasesResult | None
    enrich: EnrichOutcome | None


class InboxScreen(Screen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "back", "back")]

    DEFAULT_CSS = """
    InboxScreen #leaving {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $warning-muted;
        display: none;
    }
    """

    _seen_version = 0

    @property
    def inbox(self) -> InboxApp:
        return cast("InboxApp", self.app)

    def on_mount(self) -> None:
        self.mount(Static("", id="leaving"))

    def render_data(self) -> None:
        return None

    def op_progress(self, ticket: int, progress: RoundupProgress) -> None:
        return None

    def op_finished(self, event: Finished) -> None:
        return None

    def ops_dropped(self, tickets: Sequence[int]) -> None:
        return None

    def sync(self) -> None:
        self._seen_version = self.inbox.state.version
        self.render_data()

    def on_screen_resume(self) -> None:
        if self._seen_version != self.inbox.state.version:
            self.sync()
        self.inbox.show_leaving(self)

    def action_back(self) -> None:
        self.app.pop_screen()
