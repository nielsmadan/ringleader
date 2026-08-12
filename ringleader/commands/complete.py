from __future__ import annotations

import argparse

from .. import registry
from ..paths import archive_root
from . import archive as archive_cmd
from . import default_registry
from . import harvest as harvest_mod


def cmd_complete(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl session complete")
    ap.add_argument("id")
    ap.add_argument("--registry", default=str(default_registry()))
    args = ap.parse_args(argv)

    # Harvest first (writes the store + marks harvested + saves the registry).
    rc = harvest_mod.cmd_harvest([args.id, "--registry", args.registry])
    if rc != 0:
        return rc

    reg = registry.load_registry(args.registry)
    t = next((x for x in reg["threads"] if x["sessionId"] == args.id), None)
    if t is None or t.get("decision") != "harvested":
        # harvest was a no-op (no model configured) — do not archive
        print("not completed: nothing was harvested (configure a model with `rl config`)")
        return 0

    dest = archive_cmd.archive_thread(reg, t, archive_root_path=str(archive_root()))
    registry.save_registry(args.registry, reg)
    print(f"completed {args.id} (archived -> {dest})")
    return 0
