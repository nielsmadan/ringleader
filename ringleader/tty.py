from __future__ import annotations


def want_color(mode: str, stream, env) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return bool(getattr(stream, "isatty", lambda: False)()) and not env.get("NO_COLOR")
