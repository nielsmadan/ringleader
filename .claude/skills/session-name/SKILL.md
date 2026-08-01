---
name: session-name
description: Use to give dormant Claude Code sessions good, accurate names so the picker is readable and `claude --resume <name>` works. Names newly-classified sessions and re-names ones whose focus drifted. Scope to a repo/dir to roll out gradually.
---

# session-name

Applies the registry's `suggestedName`s to the real sessions by appending a
`custom-title` record to each transcript. It does NOT decide what a session is
about — that's `session-sync`'s classification. This skill only applies/refreshes
names, and only ever touches names *it* generated (never a name you set by hand).

## Steps

Work from the repo root (`~/wrksp/incubation/ringleader`). Scope everything
to one repo/dir at a time with `--project <selector>` (a git-project label, or a
cwd path; repeatable).

1. **See which repos/dirs have sessions** (to pick a scope):

   ```bash
   cd ~/wrksp/incubation/ringleader && .venv/bin/python bin/rl-overview
   ```

2. **Make sure that scope is classified/fresh** — run the `session-sync` skill
   scoped to it (`rl-sync scan --project <selector>`), classifying any pending
   sessions. Re-naming depends on fresh classifications.

3. **Preview** what would be named/re-named in that scope:

   ```bash
   cd ~/wrksp/incubation/ringleader && .venv/bin/python bin/rl-name --project <selector> --dry-run
   ```

   `new:` rows are first-time names; `rename:` rows show `old -> new` where a
   session we previously named has drifted. Use `--limit N` to try a few first.

4. **Apply:**

   ```bash
   cd ~/wrksp/incubation/ringleader && .venv/bin/python bin/rl-name --project <selector>
   ```

5. **Report** the summary (named / renamed / skipped).

## First-use check
After the first apply, open the picker (or `claude --resume <name>`) for one
named session and confirm the new name shows. This verifies the appended record
is surfaced by CC (we couldn't test the interactive picker during the build).

## Notes
- Idempotent: re-running only touches sessions that are newly named or have
  drifted since we named them; converged ones are skipped.
- Never hand-edit `registry.json` or the `.jsonl` transcripts.
- Recently-active sessions (modified in the last ~10 min) are skipped to avoid
  racing CC writing the same file.
