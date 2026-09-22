from __future__ import annotations

from textual.binding import Binding, BindingType

_NAV_KEYS: tuple[tuple[str, str, str], ...] = (
    ("ctrl+j", "cursor_down", "down"),
    ("ctrl+k", "cursor_up", "up"),
    ("down", "cursor_down", "down"),
    ("up", "cursor_up", "up"),
)


# vim's own directions: j down, k up. ctrl- rather than bare, so a TUI whose list is
# filtered by typing can offer the same motions without stealing them from its input.
def nav_bindings(*, priority: bool = False) -> list[BindingType]:
    """`priority=True` where an Input holds focus: Input binds ctrl+k to
    delete_right_all and would eat the query instead of moving the cursor."""
    return [
        Binding(key, action, description, show=False, priority=priority)
        for key, action, description in _NAV_KEYS
    ]
