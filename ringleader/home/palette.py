from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.markup import escape

from .base import ScreenTarget
from .screens.output import OutputScreen

if TYPE_CHECKING:
    from .app import InboxApp


@dataclass(frozen=True)
class VerbCommand:
    argv: tuple[str, ...]
    help: str
    takes_registry: bool


PALETTE_VERBS: tuple[VerbCommand, ...] = (
    VerbCommand(("usage",), "token usage across agents, last 7 days", False),
    VerbCommand(("usage", "--breakdown"), "token usage broken down per session", False),
    VerbCommand(("usage", "windows"), "5-hour and weekly limit windows", False),
    VerbCommand(("harvests",), "pending harvests waiting to be published", False),
    VerbCommand(("overview", "--sessions", "--no-scan"), "every session, from the registry", True),
    VerbCommand(
        ("triage", "--unclassified", "--no-scan"),
        "sessions never classified, from the registry",
        True,
    ),
    VerbCommand(("doctor",), "check tools, hooks and agent wiring", True),
    VerbCommand(("config", "show"), "the engine and model settings", False),
)

PALETTE_EXCLUDED: dict[str, str] = {
    "prune": "use the Prune screen",
    "roundup": "deletes — use the Roundup screen",
    "resume": "interactive — use the Resume screen",
    "start": "launches an interactive agent — run it from a shell",
    "inbox": "this app",
    "analyze": "builds an index on first run — run it from a shell",
    "install": "changes agent configuration on this machine",
    "update": "changes agent configuration on this machine",
    "uninstall": "changes agent configuration on this machine",
}

_TARGET_TITLES = {
    ScreenTarget.HOME: "Home",
    ScreenTarget.PROJECTS: "Projects",
    ScreenTarget.RESUME: "Resume",
    ScreenTarget.ROUNDUP: "Roundup",
    ScreenTarget.PRUNE: "Prune",
    ScreenTarget.HARVESTS: "Harvests",
    ScreenTarget.USAGE: "Usage",
}


@dataclass(frozen=True)
class Entry:
    title: str
    help: str
    command: Callable[[], None]


def verb_argv(verb: VerbCommand, registry_path: str) -> tuple[str, ...]:
    return (*verb.argv, "--registry", registry_path) if verb.takes_registry else verb.argv


def verb_title(verb: VerbCommand) -> str:
    return "rl " + " ".join(verb.argv)


def _open_output(app: InboxApp, verb: VerbCommand) -> None:
    app.push_screen(OutputScreen(verb_argv(verb, app.registry_path), verb_title(verb)))


def entries(app: InboxApp) -> list[Entry]:
    return [
        *(
            Entry(f"Go to {_TARGET_TITLES[target]}", "jump to this screen", partial(app.go, target))
            for target in ScreenTarget
        ),
        *(
            Entry(f"Next: {step.title}", step.detail, partial(app.activate, step))
            for step in app.home_steps(app.home_state())
        ),
        *(
            Entry(verb_title(verb), verb.help, partial(_open_output, app, verb))
            for verb in PALETTE_VERBS
        ),
    ]


class InboxCommands(Provider):
    @property
    def inbox(self) -> InboxApp:
        return cast("InboxApp", self.app)

    async def discover(self) -> Hits:
        for entry in entries(self.inbox):
            yield DiscoveryHit(
                Text(entry.title), entry.command, text=entry.title, help=escape(entry.help)
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for entry in entries(self.inbox):
            score = matcher.match(entry.title)
            if score > 0:
                yield Hit(
                    score,
                    Text(entry.title),
                    entry.command,
                    text=entry.title,
                    help=escape(entry.help),
                )
