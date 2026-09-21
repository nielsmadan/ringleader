from __future__ import annotations

import argparse
import sys

from .. import registry, scanner
from ..paths import archive_root
from ..stateio import publish_lock
from . import archive as archive_cmd
from . import default_registry
from . import harvest as harvest_mod


def cmd_complete(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session complete")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    initial = registry.load_registry(args.registry)
    initial_thread = registry.find_session(initial, args.id)
    if initial_thread is not None:
        try:
            scanner.filesystem_path(initial_thread, "complete")
        except RuntimeError as error:
            print(f"rl-complete: {error}", file=sys.stderr)
            return 1

    rc, markdown = harvest_mod._prepare_harvest(args.id, args.registry)
    if rc != 0:
        return rc
    if markdown is None:
        print("not completed: nothing was harvested (configure a model with `rl config`)")
        return 0

    with publish_lock():
        try:
            harvested_thread, path = harvest_mod._save_harvest_unlocked(
                args.id, args.registry, markdown
            )
        except KeyError:
            print(f"rl-harvest: unknown session {args.id}", file=sys.stderr)
            return 1
        print(f"harvested {harvested_thread['sessionId']} -> {path}")

        reg = registry.load_registry(args.registry)
        thread = registry.find_session(reg, args.id)
        if thread is None or thread.get("decision") != "harvested":
            print("not completed: nothing was harvested (configure a model with `rl config`)")
            return 0
        dest = archive_cmd.archive_thread(reg, thread, archive_root_path=str(archive_root()))
        registry.commit_registry(args.registry, reg)
    print(f"completed {args.id} (archived -> {dest})")
    return 0
