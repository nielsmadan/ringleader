# Agent session management: on-disk state vs. the resume picker

How each agent CLI stores sessions, how it decides what to show in its resume picker, and what
commands it offers for forking / renaming / archiving / deleting. Written for ringleader, which
edits and deletes this state from the outside and therefore has to know what the agent will notice.

Surveyed on 2026-08-15 against: Claude Code 2.1.232, codex-cli 0.147.0, opencode 1.18.15, pi 0.84.1.
Facts below were verified against local state and CLI help unless marked *(unverified)*.

## Summary

| | Claude Code | Codex | OpenCode | Pi |
|---|---|---|---|---|
| Transcript | `~/.claude/projects/<enc-cwd>/<uuid>.jsonl` | `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` | rows in `~/.local/share/opencode/opencode.db` | `~/.pi/agent/sessions/--<enc-cwd>--/<ts>_<uuid>.jsonl` |
| Picker index | none — scans the project dir | `~/.codex/state_5.sqlite` `threads` | same DB (`session` table) | none — scans the session dir |
| Scoped to cwd | yes | yes (`--all` to disable) | yes (by `project_id`) | yes |
| Fork | `--fork-session`, `forkSession` API | `codex fork` | `--fork` | `--fork`, `/fork`, `/clone`, `/tree` |
| Rename | `custom-title` record | `threads.name`, `session_index.jsonl` | `session.slug` / `title` | `--name`, `/name` |
| Archive | — | `codex archive` / `unarchive` | `session.time_archived` | — |
| Delete | `claude project purge <path>` (whole project) | `codex delete <id\|name>` | `opencode session delete <id>` | Ctrl+D in `/resume` |
| Index drifts from files | n/a (no index) | **yes — badly** | n/a (DB is the store) | n/a (no index) |

The headline for ringleader: **Claude Code and Pi have no index, so deleting a file is a complete
delete. Codex has one, and deleting the rollout file leaves a live picker entry behind.**

## Claude Code

### Storage and the cwd encoding

Transcripts live at `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`. The filename is always the
session UUID — across 112 local transcripts, zero had a `sessionId` differing from their filename.

The directory name is produced by (decompiled from the 2.1.232 binary):

```js
function encode(cwd) {
  const t = cwd.replace(/[^a-zA-Z0-9]/g, "-");
  return t.length <= 200 ? t : `${t.slice(0, 200)}-${hash36(cwd)}`;
}
```

Two consequences:

- **The encoding is lossy and not invertible.** `/` and `.` both become `-`, so
  `/Users/x/loadout/.claude/worktrees/extraction` becomes
  `-Users-x-loadout--claude-worktrees-extraction`. You cannot recover the real cwd from the
  directory name — read `cwd` off a record inside the transcript instead.
- **Paths over 200 encoded chars collide.** They are truncated and suffixed with a hash of the
  full path. Claude handles this by scanning every sibling directory sharing the 200-char prefix
  and matching each transcript's recorded cwd; a naive `encode(cwd)` lookup would miss those.

Alongside the transcript, `~/.claude/projects/<enc-cwd>/<sessionId>/` holds `subagents/` and
`tool-results/`. `ringleader/prune.py` already removes this sidecar directory with the transcript.

### Relocation

A session's project directory is fixed by the cwd *at startup* — but not permanently. When the cwd
changes mid-session (`/cd`, `EnterWorktree`), Claude **moves the transcript file** into the new
project directory and appends a marker:

```json
{"type":"relocated","sessionId":"626106a1-…","relocatedCwd":"/Users/nielsmadan/wrksp/incubation/loadout/.claude/worktrees/extraction"}
```

Matching then reads `relocatedCwd` from the tail of the transcript in preference to `cwd` from the
head. Three local transcripts carry this marker. **A transcript path is therefore not stable** — a
registry keyed on file path can go stale without the session being deleted or renamed.

### What the picker shows

`claude --resume` scans the project directory for the current cwd (plus prefix-collision siblings)
and reads a set of sidecar records that the session appends as it runs — no separate index file:

| Record type | Field | Meaning |
|---|---|---|
| `custom-title` | `customTitle` | user/tool-assigned name — what ringleader writes |
| `ai-title` | `aiTitle` | Claude's own generated title |
| `agent-name` | `agentName` | agent identity for the session |
| `last-prompt` | `lastPrompt` | preview line in the picker |
| `relocated` | `relocatedCwd` | cwd match override (above) |
| `tag`, `prNumber`, `prUrl`, `gitBranch` | — | extra picker metadata |

Later records win, so ringleader's rename works by *appending* a `custom-title` line rather than
rewriting the file (`ringleader/naming.py:title_line`).

`--resume <id>` is resolved within the current directory's project dir — the binary carries the
error string `Session <id> not found in project directory for <cwd>`. There is no global by-id
lookup; a session id from another project is not resumable from here *(unverified: only the error
string was inspected, not exercised)*.

Sub-agent transcripts live under `<sessionId>/subagents/` and never appear as top-level sessions.
Records also carry `isSidechain`, and compaction writes a `system` / `compact_boundary` record with
`logicalParentUuid` pointing at the pre-compaction turn (21 local transcripts have one) — compaction
continues in the same file rather than starting a new session.

### Commands

- `claude -c` / `--continue` — most recent session in this directory.
- `claude -r [id|search]` / `--resume` — picker, or resume a specific id.
- `claude --fork-session` — with `-r`/`-c`, continue under a **new** session id instead of appending
  to the original. Internally tracked as `forkSessionId` / `forkParentSessionId` / `forkBoundaryAt`.
- `claude --session-id <uuid>` — force a specific id (creates it if absent).
- `claude project purge <path>` — deletes *all* state for a project: transcripts, tasks, file
  history, config entry. Project-wide, not per-session; there is no `claude session delete`.
- `claude agents --json` — active interactive and background sessions.

Live sessions also register in `~/.claude/sessions/<pid>.json` with `sessionId`, `cwd`, `name`,
`status`, `updatedAt`. That is the liveness signal — a session with a matching entry is *running*,
not dormant.

## Codex

### Storage: an append-only rollout plus a SQLite index

- Transcript: `~/.codex/sessions/YYYY/MM/DD/rollout-<ISO-ts>-<uuid>.jsonl`, first line
  `session_meta` (this is what `ringleader/sources/codex.py` parses).
- Picker index: `~/.codex/state_5.sqlite`, table `threads` — one row per session with `id`,
  `rollout_path`, `cwd`, `title`, `preview`, `first_user_message`, `archived`, `archived_at`,
  `is_pinned`, `name`, `git_branch`, `git_sha`, `tokens_used`, `recency_at_ms`, `thread_source`.
- `thread_spawn_edges` (parent → child) records sub-agent lineage; `thread_history_1.sqlite`
  holds a paginated projection (`thread_turns`, `thread_items`) of the rollout.
- `~/.codex/session_index.jsonl` maps `id` → `thread_name` for named sessions.

The picker's own indexes state the filter directly:

```sql
CREATE INDEX idx_threads_visible_recency_at_ms
  ON threads(archived, recency_at_ms DESC, id DESC) WHERE preview <> '';
CREATE INDEX idx_threads_archived_cwd_recency_at_ms
  ON threads(archived, cwd, recency_at_ms DESC, id DESC);
```

So: `archived = 0`, `preview <> ''` (a session that never got a user turn is invisible), filtered by
`cwd` unless `--all`, ordered by `recency_at_ms`. `--include-non-interactive` widens it to `exec`
sessions. Locally: 204 `thread_source='user'`, 483 `subagent`, 313 unset; 373 threads from `codex
exec`, 430 spawned as subagents.

### The drift problem

**Deleting a rollout `.jsonl` does not remove the thread row.** Of 1000 local `threads` rows, 454
point at a `rollout_path` that no longer exists — and 438 of those still satisfy `preview <> ''`,
i.e. they are still picker-visible. (Every one of the 454 has `has_user_event = 0`; there were no
orphan files in the other direction.) `ringleader/prune.py:41` unlinks transcript files directly,
which is exactly the operation that produces this.

### What `codex archive` actually does (verified by round-trip)

Archiving one live thread and restoring it:

```
codex archive 019fdab8-…      # threads.archived 0 → 1, archived_at set
                              # rollout file MOVED sessions/2026/08/07/… → ~/.codex/archived_sessions/
                              # threads.rollout_path rewritten to the new location
codex unarchive 019fdab8-…    # both reversed exactly
```

So `codex archive` already *is* ringleader's archive: it takes the transcript out of the scan path
**and** clears the picker entry, reversibly.

The constraint: **it needs the rollout file to still be there.** Run against one of the 454 dangling
rows it fails —

```
$ codex archive 9ccbc341-…
Error: failed to archive session       # exit 1, message on stderr
```

— which means the ordering is fixed: call `codex archive`/`delete` *first*, move or unlink second
(or not at all). It also means the 438 already-dangling visible rows **cannot be repaired through
the CLI**; only a direct write to `threads` will clear them.

`codex delete <id|name>` is the destructive counterpart, presumably index-aware in the same way
*(unverified — not run, since it permanently destroys a session)*.

### Commands

- `codex resume [SESSION_ID|name] [PROMPT]`, `--last`, `--all`, `--include-non-interactive`.
- `codex fork [SESSION_ID] [PROMPT]`, `--last`, `--all` — new session seeded from an existing one.
- `codex archive <SESSION>` / `codex unarchive <SESSION>` / `codex delete <SESSION>` — each takes a
  UUID **or** a session name, UUID winning if the argument parses as one.

Codex is the only surveyed agent whose CLI offers a first-class archive/unarchive pair, which is
worth noting given ringleader's own one-way `rl session archive`.

## OpenCode

There is no per-session file to curate: everything is rows in
`~/.local/share/opencode/opencode.db` (`session`, `message`, `part`, `project`, `workspace`, …). The
legacy `~/.local/share/opencode/storage/` tree is nearly empty (1 session directory) — migrated.

`session` columns that matter: `id` (`ses_…`), `project_id`, `parent_id` (non-null for child /
sub-agent sessions — 7 of 133 locally), `slug` (human-friendly, e.g. `shiny-star`), `title`
(generated), `directory`, `time_created` / `time_updated` / `time_archived` (0 archived locally),
`agent`, `model`, `cost`, and a token breakdown.

`project.id` is an opaque hash of `project.worktree`; it is **not** SHA-1 of the path (checked), so
resolve a directory to a project by querying the `project` table rather than recomputing a hash.
`worktree = "/"` is the `global` project.

Commands: `opencode session list`, `opencode session delete <sessionID>`, `opencode export
[sessionID] [--sanitize]`, `opencode import <file>`, `-c/--continue`, `-s/--session <id>`, and
`--fork` (used with `--continue` or `--session`) to branch instead of appending. Sub-agent lineage is
`parent_id`; archival is a timestamp column with no CLI verb exposed for it.

Note: `opencode --help` crashes with `EEXIST … mkdir '~/.local/state/opencode'` in this sandbox;
setting `XDG_STATE_HOME` to a writable path works around it.

## Pi

Storage is `~/.pi/agent/sessions/--<encoded-cwd>--/<ISO-ts>_<uuidv7>.jsonl` — cwd with `/` → `-`,
wrapped in double dashes. Unlike Claude, `_` and other characters survive
(`--Users-nielsmadan-wrksp-_archive-nono-smoke--`). The first line is a `session` header carrying
`version`, `id`, `timestamp` and the real `cwd`; there is no index file, so the picker is a directory
scan for the current project.

Pi's model is a **tree inside one file**: every entry has `id` / `parentId`, and the current position
is the active leaf. Session versions: v1 linear (auto-migrated), v2 tree, v3 renamed `hookMessage`
→ `custom`.

Branching has three distinct verbs, which is the richest vocabulary of the four:

| | `/tree` | `/fork` | `/clone` |
|---|---|---|---|
| Output | same session file | new session file | new session file |
| View | full tree | user-message selector | current active branch |
| Use | explore alternatives in place | restart from an earlier prompt | duplicate before continuing |

Commands: `pi -c`, `pi -r`, `pi --session <path|id>` (partial UUID accepted), `pi --fork <path|id>`,
`pi --session-id <id>`, `pi --session-dir <dir>`, `pi --no-session`, `pi --name/-n <name>`,
`pi --export <file>`. In-session: `/resume`, `/new`, `/name`, `/session`, `/tree`, `/fork`,
`/clone`, `/compact`, `/export`, `/share`. The `/resume` picker supports rename (Ctrl+R), delete
(Ctrl+D), and a named-sessions-only filter (Ctrl+N); deletion prefers the `trash` CLI over `unlink`.

## Implications for ringleader

1. **Never derive a cwd from a directory name.** Claude's encoding is lossy and truncating; Pi's
   differs; OpenCode has none. Always read the cwd from inside the session (Claude: `relocatedCwd`
   from the tail, else `cwd` from the head).
2. **A Claude transcript's path is mutable.** Relocation moves the file between project directories
   mid-session, so a path-keyed registry entry can dangle without any user action.
3. **File deletion is not delete-everywhere.** It is for Claude Code and Pi. It is not for Codex —
   `rl prune` on Codex sessions leaves picker-visible `threads` rows pointing at missing files, and
   ~450 such rows already exist locally. Codex pruning should go through `codex delete`/`archive`,
   and must do so **before** touching the file, or the CLI refuses.
4. **Ringleader owns archive; only index-clearing is per-source.** `codex archive` would do the
   whole job for Codex — it moves the transcript to `~/.codex/archived_sessions/` and flips the
   index bit reversibly — but delegating to it makes the archive location and semantics diverge per
   agent, adds a CLI dependency, and can't touch rows whose file is already gone. Instead
   `hides_from_picker` on the `Source` seam clears the agent's own index entry (Codex: set
   `threads.archived = 1`; Claude Code and Pi: no-op), and ringleader's own move stays the
   implementation everywhere.
5. **Fork/branch is universal but shaped differently** — Claude and OpenCode fork the whole session
   under a new id, Codex forks from a picker, Pi distinguishes in-file branching (`/tree`) from new
   files (`/fork`, `/clone`). A ringleader "branch this session" command would need per-source
   verbs rather than one shared implementation.
6. **Liveness has a source of truth per agent**: Claude `~/.claude/sessions/<pid>.json`
   (`status`, `updatedAt`), Codex `threads.recency_at_ms`, OpenCode `session.time_updated`. Useful
   for not curating a session someone is currently sitting in.
