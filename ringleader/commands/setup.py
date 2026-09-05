from __future__ import annotations

import sys

from . import hooks, skills, statusline

# noun -> verb -> command. A noun simply omits a verb it has no meaning for
# (the statusline has nothing to re-sync), and `all` skips it with a note.
NOUNS = {
    "skills": {
        "install": skills.cmd_install,
        "update": skills.cmd_update,
        "uninstall": skills.cmd_uninstall,
    },
    "statusline": {
        "install": statusline.cmd_install,
        "uninstall": statusline.cmd_uninstall,
    },
    "hooks": {
        "install": hooks.cmd_install,
        "update": hooks.cmd_update,
        "uninstall": hooks.cmd_uninstall,
    },
}

NOUN_HELP = {
    "skills": "the bundled ringleader-* skills, linked into each agent's skills dir",
    "statusline": "the Claude Code statusLine snippet that captures rate-limit windows",
    "hooks": "the session-end hook that archives finished sessions",
}


def _print_help(verb: str, *, default_all: bool) -> None:
    print(f"usage: rl {verb} <noun> [options]\n")
    print("nouns:")
    for name, verbs in NOUNS.items():
        if verb in verbs:
            print(f"  {name:<11} {NOUN_HELP[name]}")
    print(f"  {'all':<11} every noun above")
    lead = f"`rl {verb}` with no noun does `all`.\n" if default_all else ""
    print(f"\n{lead}Run `rl {verb} <noun> --help` for that noun's own options.")


def _run(verb: str, argv: list[str], *, default_all: bool) -> int:
    if argv and argv[0] in ("-h", "--help"):
        _print_help(verb, default_all=default_all)
        return 0
    noun = argv[0] if argv and not argv[0].startswith("-") else None
    rest = argv[1:] if noun else argv

    if noun is None and not default_all:
        print(
            f"rl {verb}: say which — {'  '.join(NOUNS)}  all",
            file=sys.stderr,
        )
        return 2
    if noun is not None and noun != "all" and noun not in NOUNS:
        print(f"rl {verb}: unknown '{noun}' — {'  '.join(NOUNS)}  all", file=sys.stderr)
        return 2

    targets = [noun] if noun and noun != "all" else list(NOUNS)
    rc = 0
    for name in targets:
        run = NOUNS[name].get(verb)
        if run is None:
            # only worth saying when the user asked for everything
            if noun is None or noun == "all":
                print(f"{name}: nothing to {verb}, skipping")
            continue
        rc = run(rest) or rc
    return rc


def cmd_install(argv: list[str]) -> int:
    return _run("install", argv, default_all=True)


def cmd_update(argv: list[str]) -> int:
    return _run("update", argv, default_all=False)


def cmd_uninstall(argv: list[str]) -> int:
    return _run("uninstall", argv, default_all=False)


def nouns_for(verb: str) -> tuple[str, ...]:
    """Which targets a verb actually supports — `rl update statusline` does not exist."""
    return tuple(name for name, verbs in NOUNS.items() if verb in verbs)
