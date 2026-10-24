from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from .. import export, harvesting, registry
from ..config import load_config
from ..models import active_model
from ..models.base import ModelError
from ..session_env import current_session
from ..stateio import publish_lock
from . import default_registry


def _now() -> str:
    return datetime.now(UTC).isoformat()


def prepare_harvest(
    session_id: str, registry_path: str, *, prog: str = "rl-harvest"
) -> tuple[int, str | None]:
    model = active_model(load_config())
    if model is None:
        print(
            "no model configured — set one with `rl config`, or harvest via the triage/cleanup skill"
        )
        return 0, None

    reg = registry.load_registry(registry_path)
    thread = registry.find_session(reg, session_id)
    if thread is None:
        print(
            f"{prog}: unknown session {session_id} — run `rl session refresh` "
            "first if it started since the last scan",
            file=sys.stderr,
        )
        return 1, None
    try:
        condensed = export.render_conversation(thread)
    except (OSError, RuntimeError) as error:
        print(f"{prog}: {error}", file=sys.stderr)
        return 1, None
    try:
        data = model.complete_json(
            harvesting.build_prompt(thread, condensed), harvesting.HARVEST_SCHEMA
        )
    except (ModelError, KeyError) as error:
        print(f"{prog}: {error}", file=sys.stderr)
        return 1, None
    return 0, harvesting.render_markdown(thread, data)


def _read_from_file(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def cmd_harvest(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session harvest")
    ap.add_argument("id", nargs="?")
    ap.add_argument("--current", action="store_true", help="the session rl is running inside")
    ap.add_argument("--from-file", help="markdown to store as-is ('-' for stdin); skips the model")
    ap.add_argument("--registry", default=str(default_registry()))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    session_id = args.id
    if args.current:
        found = current_session()
        if found is None:
            print("rl-harvest: not inside a recognised agent session", file=sys.stderr)
            return 1
        session_id = f"{found[0]}:{found[1]}"
    if not session_id:
        print("rl-harvest: need a session id, or --current", file=sys.stderr)
        return 1

    markdown: str | None
    if args.from_file is not None:
        markdown = _read_from_file(args.from_file)
        if not markdown.strip():
            print("rl-harvest: refusing to store an empty harvest", file=sys.stderr)
            return 1
    else:
        rc, markdown = prepare_harvest(session_id, args.registry)
        if rc != 0 or markdown is None:
            return rc

    if args.dry_run:
        sys.stdout.write(markdown + "\n")
        return 0

    with publish_lock():
        try:
            thread, path = harvesting.save_harvest_unlocked(
                session_id, args.registry, markdown, now_iso=_now()
            )
        except KeyError:
            print(
                f"rl-harvest: unknown session {session_id} — run `rl session refresh` "
                "first if it started since the last scan",
                file=sys.stderr,
            )
            return 1
    print(f"harvested {thread['sessionId']} -> {path}")
    return 0
