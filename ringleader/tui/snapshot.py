from __future__ import annotations

import io
from typing import Any

from rich.console import Console
from textual.app import App
from textual.widgets import Footer


def normalize_text(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def screen_text(app: App[Any]) -> str:
    width, height = app.size
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        record=True,
        legacy_windows=False,
        safe_box=False,
    )
    console.print(
        app.screen._compositor.render_update(full=True, screen_stack=app._background_screens)
    )
    return normalize_text(console.export_text(styles=False))


def footers_drawn(app: App[Any]) -> bool:
    return all(footer.children for footer in app.screen.query(Footer))
