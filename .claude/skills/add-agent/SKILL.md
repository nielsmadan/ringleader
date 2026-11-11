---
name: add-agent
description: Add support for another coding agent CLI to ringleader — verify how the agent stores sessions on this machine, choose which tiers to support (presence, session scanning and lifecycle, usage, hooks and skills install, native conversion, analysis), then implement them through the Source seam with isolated tests and verified docs. Use when the user says "add support for X", "support agent X in ringleader", "add a new agent", "add a new source", "make rl work with X sessions", or "wire up X like droid". Do NOT use for fixing a bug in an existing source, or for adding an enrich model backend (rl config ai).
---

# Add an agent to ringleader

Ringleader edits and deletes the agent's real session state from outside, so every
capability rests on facts observed on this machine, not on the agent's docs. The droid
addition is the worked precedent: read `docs/plans/2026-09-22-droid-support.md` and its
`-ledger.md` before starting. Its review overturned two rulings and one "verified" fact, and
those corrections are the lessons this skill encodes.

`references/integration-points.md` lists every file and symbol to touch, by tier.

## Instructions

### Step 1: Pick the tiers with the user

Ask which tiers to support, after a quick look at what the agent offers:

| Tier | Gives the user | Needs |
|---|---|---|
| 0 Presence | live-session dots in the inbox | hooklinesinker already reports the agent |
| 1 Sessions | scan, triage, inbox, resume, start, archive, prune, workspace move | readable session store |
| 2 Usage | `rl usage` totals | token counts somewhere on disk |
| 3 Hooks and skills | `rl install hooks` / `rl install skills` | a hooks config and a skills dir |
| 4 Native conversion | `rl resume --to` continues the session natively | a writable format and an offline harness |
| 5 Analysis | `rl analyze` failure signals | a transcript with tool calls and results |

Tier 1 is the usual minimum, and tiers 2 and 3 are cheap once it exists. Tier 4 is the size of
the other tiers combined; offer it as separate work. Record the user's choice, and give a reason
for each tier left out.

### Step 2: Verify the facts on this machine

Survey the installed agent and its real sessions, read-only. Record the agent's version and
today's date. Establish, with counts ("40 of 254 sessions"):

- **Storage root and layout.** Every layout the agent uses, not just the common one. Check its
  `--help`, its docs, and strings in its binary (droid has three transcript layouts, named in
  its own schema comment, and the first survey found one).
- **Home override.** The env var the agent itself reads to relocate its store. Tests and the
  sandbox isolate through it.
- **Directory encoding.** If storage is keyed by cwd, reproduce the encoding and check it
  against every session. Never assume it matches Claude Code's.
- **Record shapes.** The first line, the message records, and where cwd, title and timestamps
  live.
- **Subagent and exec runs.** How to tell them from top-level sessions.
- **Picker index.** Look in caches and the binary (`strings <binary> | grep -i 'CREATE TABLE'`),
  not only beside the sessions. Droid's index lived in `cache/session-index/`, which a survey of
  `sessions/` missed.
- **CLI verbs.** Resume, fork, start with a prompt, rename. Note how each takes a session id
  and a directory.
- **Session id env var.** Whether a running session exports its own id.
- **Tokens.** Which file holds them and in what shape.

On `Operation not permitted`, or an `ls` that shows `total 0`, load the `nono-sandbox` skill and
run `nono why --self --path <dir> --op read` before concluding the directory is empty. A denied
path is unknown, not empty: mark what you could not read as *(unverified)*. Trust observed
files over any write-up; droid's detailed third-party report gave a path that did not exist.

Success: a facts list in which every item is either observed (with evidence) or marked
unverified.

### Step 3: Write the plan

Write `docs/plans/YYYY-MM-DD-<agent>-support.md` in the shape of the droid plan:

- Goal, and the tiers in scope.
- Verified facts.
- The capabilities the source declares, plus those declined, each with its evidence. Decline
  anything that would write an unverified format: an unknown index schema means no
  `hides_from_picker`.
- Out of scope, one reason each.
- Numbered tasks following the tier order of `references/integration-points.md`.

Show the plan to the user and wait for approval before implementing. For a run of several
hours, offer `longshot` with this plan as its brief.

### Step 4: Isolate the tests first

Before any source code, add the agent's home override to `AGENT_STORE_VARS` in
`ringleader/devtools/isolation.py`. The test suite, the sandbox, `tui_shot`, the synthetic TUI and
GUI worlds and the TUI end-to-end harness all take their isolation from it. Otherwise, once the
source is registered, they scan the real store on any machine that has the agent installed, and
`rl` run inside the GUI world can archive or delete real sessions. Add the agent's home directory
to `gui_world.FORBIDDEN_HOME_DIRS` too.

Success: `just test` passes with the agent's real store present, including
`tests/test_devtools_isolation.py`.

### Step 5: Implement tier by tier

Follow `references/integration-points.md` for each tier in scope, one commit per tier
(`feat: scan <agent> sessions`, `feat: count <agent> usage`, ...). After each tier:

- Regrep for an existing agent's name (`grep -rn -i droid ringleader scripts tests`) and
  decide on every hit that does not yet name the new agent.
- Run `just check`. The gates are strict: no `type: ignore`, no relaxed config.

Build the tests on synthetic fixture stores shaped like the real records, including a
malformed-file case.

### Step 6: Verify the lifecycle end to end

On a synthetic store under a temp `HOME` and `XDG_DATA_HOME` (never the real one), run each
supported path and record the result in a table, as the droid ledger does:

| Step | Check |
|---|---|
| scan | thread count, title, cwd, message count |
| `rl usage` | the total matches the hand-summed fixture |
| `rl resume <id> --dry-run` | the argv and the directory |
| `rl start <agent> --dry-run` | the argv |
| archive, then unarchive | transcript and every companion file move both ways |
| delete | nothing orphaned beside the transcript |
| `rl workspace move` (if relocating) | storage follows the directory |

Never run `rl prune --delete`, archive or delete against the real `~/.<agent>` store.

### Step 7: Update the docs

Follow the docs section of `references/integration-points.md`: the dated reference section, the
summary-table column, every agent list and count, `PLAYBOOK.md` with `just sync-playbook`, and
a `SMOKE.md` row. Mark the plan **Status: Complete** with the date.

## Examples

### Example 1: a full first-class agent

User says: "Add support for Qwen Code sessions."

Actions:
1. Qwen is already at tier 0 (`presence.AGENTS`). Survey `~/.qwen` and propose tiers 1 to 3.
   The user agrees and leaves tier 4 for later.
2. Verify: storage layout, `QWEN_HOME`, record shapes, subagent marker, CLI resume and fork
   verbs, token location, and any index in the binary. Mark a sandbox-denied cache as
   unverified.
3. Write `docs/plans/2026-11-10-qwen-support.md`; the user approves it.
4. Add `QWEN_HOME` to `AGENT_STORE_VARS`.
5. Add `QwenSource` with `launches_sessions`, `filesystem_lifecycle` and `relocates_workspace`.
   Decline `applies_names`, since there is no rename record. Then the usage reader, then hooks
   and the skills dir.
6. Run the lifecycle table on a synthetic store, then update the docs.

Result: three `feat:` commits, a dated reference section, `just check` green.

### Example 2: presence only

User says: "Gemini CLI just needs to show up as live in the inbox."

Actions: confirm the vendored hooklinesinker reports gemini (`rl data presence status`), add it
to `presence.AGENTS` and the `homes` map in `detected_agents()`, routing a documented
home-override variable through `_agent_home_override()`, clear that variable in conftest, and
add a presence test.

Result: one `feat:` commit; no source, usage or reference section.

## Troubleshooting

### The suite fails only on machines with the agent installed
**Cause:** the source is registered but the tests read the real store.
**Solution:** add the agent's home override to `AGENT_STORE_VARS` in
`ringleader/devtools/isolation.py`; `tests/test_devtools_isolation.py` fails until every default
source resolves inside the isolated root.

### Archiving or deleting leaves files behind in the agent's store
**Cause:** the agent keeps per-session sidecars that the lifecycle verbs do not know about.
**Solution:** declare `companion_files` and implement both `companion_paths()` and
`companion_destination()`; a source missing either fails the protocol check and its companions
are silently skipped. Every archive, delete, prune and unarchive path goes through
`scanner.companion_paths()` and `scanner.companion_moves()`. State kept in a second tree also
needs `store_roots` (`SplitStoreSource`), or `delete` and `prune` refuse the session.

### The agent's picker still lists a session ringleader archived
**Cause:** the agent keeps an index separate from the transcript files.
**Solution:** verify the index schema from the real database. If it is readable, implement
`hides_from_picker` and `shows_in_picker` returning `IndexUpdate`. If it is not, document the
gap as the largest known risk rather than writing the format on inference.

### Inbox goldens all changed
**Cause:** `tui_fixture.SOURCE_WEIGHTS` gained the agent, so the synthetic world changed.
**Solution:** expected. Regenerate with `uv run pytest <test> --update-goldens`, read the diff,
and commit the goldens with the source.

### Changed parsing does not show up in the registry
**Cause:** cached parses are keyed by `parser_version`.
**Solution:** bump the source's `parser_version`.
