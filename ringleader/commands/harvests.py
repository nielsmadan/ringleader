from __future__ import annotations

import argparse
import json
import sys

from .. import harvests as store


def cmd_harvests(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl harvests")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rows = store.load_harvests()
    if args.json:
        json.dump(
            [
                {
                    "sessionId": h.session_id,
                    "name": h.name,
                    "folderPath": h.folder_path,
                    "repoUrl": h.repo_url,
                    "harvestedAt": h.harvested_at,
                    "path": str(h.path),
                    "content": h.content,
                }
                for h in rows
            ],
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 0

    if not rows:
        print("no harvests yet")
        return 0
    for h in rows:
        preview = " ".join(h.content.split())[:80]
        print(f"{h.name or h.session_id}  [{h.repo_url or h.folder_path or '?'}]")
        print(f"  {h.folder_path or ''}  — {preview}")
    print(f"\n{len(rows)} harvest(s)")
    return 0
