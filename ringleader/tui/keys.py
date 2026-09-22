from __future__ import annotations

from textual.binding import Binding, BindingType

# vim's own directions: j down, k up. ctrl- rather than bare, so a TUI whose list is
# filtered by typing can offer the same motions without stealing them from its input.
NAV_BINDINGS: list[BindingType] = [
    Binding("ctrl+j", "cursor_down", "down", show=False),
    Binding("ctrl+k", "cursor_up", "up", show=False),
    Binding("down", "cursor_down", "down", show=False),
    Binding("up", "cursor_up", "up", show=False),
]
