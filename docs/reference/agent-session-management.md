# Agent session management: on-disk state vs. the resume picker

How each agent CLI stores sessions, how it decides what to show in its resume picker, and what
commands it offers for forking / renaming / archiving / deleting. Written for ringleader, which
edits and deletes this state from the outside and therefore has to know what the agent will notice.

Surveyed on 2026-08-15 against: Claude Code 2.1.232, codex-cli 0.147.0, opencode 1.18.15, pi 0.84.1.
Facts below were verified against local state and CLI help unless marked *(unverified)*.

OpenCode storage: verified 2026-08-31 against opencode 1.18.25.

Droid storage and CLI: verified 2026-09-23 against droid 0.218.2.

Cursor CLI storage, CLI and hooks: verified 2026-11-11 against cursor-agent 2026.11.05-86047df.

For worktree creation, setup, isolation, session binding, and cleanup, see
[Agent worktree management](agent-worktrees.md), surveyed separately on 2026-11-01.

## Summary

| | Claude Code | Codex | Droid | OpenCode | Pi | Cursor CLI |
|---|---|---|---|---|---|---|
| Transcript | `~/.claude/projects/<enc-cwd>/<uuid>.jsonl` | `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl` | `~/.factory/sessions/<enc-cwd>/<uuid>.jsonl` + `.settings.json` sidecars | rows in `~/.local/share/opencode/opencode.db` | `~/.pi/agent/sessions/--<enc-cwd>--/<ts>_<uuid>.jsonl` | `~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl` + `~/.cursor/chats/<md5(cwd)>/<id>/` store |
| Picker index | none — scans the project dir | `~/.codex/state_5.sqlite` `threads` | none — scans the session dir | same DB (`session` table) | none — scans the session dir | none — scans the chats dir for the cwd |
| Scoped to cwd | yes | yes (`--all` to disable) | yes (`--cwd` to point elsewhere) | yes (by `project_id`) | yes | yes (`chats/<md5(cwd)>`) |
| Fork | `--fork-session`, `forkSession` API | `codex fork` | `--fork <id>`, `resume --fork` | `--fork` | `--fork`, `/fork`, `/clone`, `/tree` | — |
| Rename | `custom-title` record | `threads.name`, `session_index.jsonl` | — (title is on line 1) | `session.slug` / `title` | `--name`, `/name` | — (`title` in `meta.json`) |
| Archive | — | `codex archive` / `unarchive` | — | `session.time_archived` | — | — |
| Delete | `claude project purge <path>` (whole project) | `codex delete <id\|name>` | — | `opencode session delete <id>` | Ctrl+D in `/resume` | — |
| Index drifts from files | n/a (no index) | **yes — badly** | unverified (see Droid) | n/a (DB is the store) | n/a (no index) | n/a (no separate index) |

The headline for ringleader: **Claude Code and Pi have no index, so deleting a file is a
complete delete. Codex has one, and deleting the rollout file leaves a live picker entry behind.
Droid is unverified: its binary ships a session index this machine's sandbox cannot read.**

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

## Droid

Surveyed 2026-09-23 against droid 0.218.2 and 257 local session files.

### Storage and the cwd encoding

Transcripts live at `<factory-home>/.factory/sessions/<encoded-cwd>/<uuid>.jsonl`, each with two
sidecars beside it: `<uuid>.settings.json` and `<uuid>.settings.json.bak`. `FACTORY_HOME_OVERRIDE`
replaces **HOME**, not the `.factory` name, so under an override the tree is
`$FACTORY_HOME_OVERRIDE/.factory/sessions/`.

**Droid has its own directory encoding**, not Claude Code's — corrected after a later survey
against 438 local sessions (434/438 matched Claude Code's `slugify`, but all 438 matched droid's
own encoder, which is what `slug.droid_slug()` now implements). Droid's encoder
calls `realpathSync`, then replaces only `/` with `-`, keeping `_`, `.`, and spaces that Claude
Code's `slugify` would also replace with `-` — the 4-session gap is exactly the launch
directories whose name uses one of those characters. `slug.py` is still load-bearing for two
agents, each with its own encoder now, so an upgrade to *either* CLI can change its own output
without affecting the other.

A launch-directory mismatch is still safe in the same direction: `storage_for()` then names a
directory that does not exist, which surfaces as a `drifted` badge and a `relocate_storage()`
that returns `{}` rather than as anything being overwritten. *(Unverified: whether droid also
implements a 200-char truncate-and-hash branch like Claude Code's. No local droid path was long
enough to exercise it.)*

**There are three layouts, not one.** Droid's `findSessionFilePath` knows the global root
(`sessions/<uuid>.jsonl`), a btw fork (`sessions/btw/<uuid>.jsonl`) and a project directory
(`sessions/<encoded-cwd>/<uuid>.jsonl`). The local store holds 257 project-directory sessions and
none of the other two. Ringleader's Source and usage reader read the global root alongside project
directories, and skip `btw`: a fork filed there is never re-filed under a launch-directory slug,
so skipping it excludes it for good rather than deferring it.

### The transcript

The first line is always a `session_start` record carrying `id`, the real `cwd`, `title`,
`version: 2`, `owner` and `callingSessionId`. The record types that follow are `session_start`,
`message`, `todo_state`, `compaction_state` and `agent_turn_outcome`.

Messages share **Pi's** shape — `{"type":"message","message":{"role":…,"content":[…]}}` — which is
why the generic JSONL exporter renders a droid session with no droid-specific code, and why
`DroidSource` declines `renders_conversation`.

A subagent transcript carries a `callingSessionId`; top-level runs omit it or set it to null.
Top-level runs also include non-interactive `droid exec` invocations. Their `.settings.json`
sidecar carries `"tags": [{"name": "exec"}]`, including the read-only consultation runs
observed in the local store on 2026-11-02 (transcript format version 2; originating CLI
versions are not recorded). `interactionMode: "auto"` appears in both interactive and exec
sessions, so it does not distinguish them. Ringleader excludes child sessions and explicitly
exec-tagged runs; missing or malformed settings do not establish an exec run.

### What the picker shows

`.favorites` is an empty JSON array, there is no sqlite database **beside the sessions**, and
`droid resume --last` is documented as resuming the most recent session *in the current folder* —
all consistent with the picker being a directory scan, as Claude Code's and Pi's are.

*(Unverified, and load-bearing. Droid 0.218.2's bundle builds a session-index path,
`join(<factory>, "cache", "session-index", "index.db")`, and carries its baseline migration: a
`CREATE TABLE sessions` with `session_id`, `project_key`, `cwd`, `title`,
`transcript_messages_count` and `settings_mtime_ms`, plus a migrations table keyed by `version`,
`name`, `checksum` and `applied_at`, and gate strings such as
`session-index-gate-recheck-failed` and `session-index-write-cap`. Whether that index is
populated on any given machine could not be checked here: everything under `~/.factory` except
`sessions/` is unreadable under this machine's sandbox profile, and the index sits one level
above the directory this survey could read. If it is live, deleting or archiving a droid
transcript strands a row naming a missing file — the Codex failure mode `hides_from_picker`
exists to prevent — and `DroidSource` would need to implement `PickerHidingSource`. It
deliberately does not: the schema is unverified and unreachable, and writing another program's
private SQLite format on inference is exactly the hazard `slug.py`'s docstring warns about. This
is a recorded risk, not a settled fact.)*

`droid search` does maintain a cache of its own — `--reindex` drops and rebuilds it. *(Unverified:
where that cache lives and whether deleting a transcript strands an entry in it were not checked;
everything under `~/.factory` except `sessions/` is unreadable under this machine's sandbox
profile.)*

### Renaming

There is nothing to append to. `title` and `isSessionTitleManuallySet` sit on **line 1**, in
`session_start`, and no later record overrides them — unlike Claude Code's appended `custom-title`
or Codex's `threads.name`. Renaming a droid session from outside would mean rewriting the first
line of a file the agent may still be appending to, which is why `DroidSource` declines
`applies_names`.

### Tokens

Not in the transcript. Each `<uuid>.settings.json` sidecar holds `tokenUsage`,
`inclusiveTokenUsage`, `childInclusiveTokenUsageBySessionId`, `model`, and
`effectiveFactoryRouterModel` — an **object**, whose `modelId` is the model string. Every usage
value is shaped `{inputTokens, outputTokens, cacheCreationTokens, cacheReadTokens, thinkingTokens,
factoryCredits}`.

There are no per-turn counts anywhere, only per-session totals, and `factoryCredits` is the unit
droid actually bills in. Both facts constrain what `rl usage` can report — see
[tech/usage.md](../tech/usage.md).

### Commands

- `droid [prompt]` — start interactive, optionally with an opening prompt.
- `droid resume [sessionId]` / `droid -r [sessionId]` — picker without an id, direct resume with one.
- `droid resume --last` — "skip the picker and resume the most recent session **in the current
  folder**", so the picker is cwd-scoped.
- `droid resume --fork` / `droid --fork <sessionId>` — fork a session and resume the fork.
- `droid --cwd <path>` — the working directory the above resolve against.
- `droid exec [prompt]` — non-interactive, for scripts.
- `droid search <query> [--json] [--reindex]` — full-text across local sessions.
- `droid doctor` — configuration, connectivity, auth and daemon diagnostics.

`droid --help` and `droid resume --help` list **no archive, unarchive, delete or rename verb**, so
curating droid sessions from outside is entirely a filesystem job — and the whole job only if its
session index turns out not to be live (see "What the picker shows").

## OpenCode

There is no per-session file to curate: everything is rows in
`~/.local/share/opencode/opencode.db` (`session`, `message`, `part`, `project`, `workspace`, …). The
legacy `~/.local/share/opencode/storage/` tree is nearly empty (1 session directory) — migrated.

`session` columns that matter: `id` (`ses_…`), `project_id`, `parent_id` (non-null for child /
sub-agent sessions — 7 of 158 locally), `slug` (human-friendly, e.g. `shiny-star`), `title`
(generated), `directory`, `time_created` / `time_updated` / `time_archived` (0 archived locally),
`agent`, `model`, `cost`, and a token breakdown.

Assistant `message.data` rows carry the per-response `time.created`, `modelID`, `providerID`,
`cost`, and token buckets. Their sums matched the corresponding `session` totals exactly on the
local database. `tokens.reasoning` is separate from visible `tokens.output`, not a subset; both
must be counted in total output usage.

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

Branching has three distinct verbs, which is the richest vocabulary of the five:

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

## Cursor CLI

Surveyed 2026-11-11 against `cursor-agent` 2026.11.05-86047df, its bundled JavaScript under
`~/.local/share/cursor-agent/versions/`, and 13 local chats. This is the CLI agent (`cursor-agent`,
also installed as `agent`), not the Cursor IDE, whose chats live elsewhere.

### Storage: a chat store and a transcript, in two roots

Each chat is kept twice, and the agent's own `cursor-config/dist/paths.js` names both roots:

- **The chat store**, `<config>/chats/<md5(cwd)>/<chatId>/`, where `<config>` is
  `$CURSOR_CONFIG_DIR`, else `$XDG_CONFIG_HOME/cursor`, else `~/.cursor`. The directory name is
  the md5 of `path.resolve(cwd)` (13 of 13 local chats). It holds `meta.json`, and for a chat with
  a conversation also `store.db` (with `-wal`/`-shm` while live) and sometimes
  `prompt_history.json`.
- **The transcript**, `<data>/projects/<slug>/agent-transcripts/<chatId>/<chatId>.jsonl`, where
  `<data>` is `$CURSOR_DATA_DIR`, else `~/.cursor`. The agent renders it from the store "for
  citation" (its own `.gitignore` wording), so it is derived, but it is the only plain-text copy.
  Present for 9 of 9 chats with a conversation.

The slug (`utils/dist/workspace-paths.js`) replaces every non-alphanumeric character with `-`,
collapses runs and trims both ends, with no `realpath`: `/var/folders/v_/…` becomes
`var-folders-v-…`. It is lossy, so `/a/b-c` and `/a/b/c` share a slug; the md5 key does not
have that problem. *(Unverified: whether a chat started in a repo's subdirectory files its
transcript under the cwd's slug or the git root's. Ringleader finds a transcript by chat id when the
cwd's slug misses.)*

`meta.json` is `{schemaVersion: 1, createdAtMs, updatedAtMs, hasConversation, cwd, title?}`. A chat
with `hasConversation: false` is empty, from `create-chat` or an agent opened and closed (4 of 13
here). `title` is generated after the first turn (6 of 9 real chats had one).

`store.db` is SQLite with two tables: `meta(key, value)`, holding one hex-encoded JSON row
(`agentId`, `latestRootBlobId`, `name`, `mode`, `createdAt`, `lastUsedModel`, `subagentInfo`,
`blobEncryptionKey`), and `blobs(id, data)`, a content-addressed store of JSON messages and
protobuf state rooted at an `agent.v1.ConversationStateStructure`. Open it with `?mode=ro`, which
reads a live store's WAL; `?immutable=1` reads only the main file and can see no tables at all.

### The transcript

Records are `{role, message: {content: [...]}}` with `text` and `tool_use` blocks, plus
`{type: "turn_ended", status}`. There are no timestamps, no ids and no tool results. A user turn
wraps the prompt as `<timestamp>…</timestamp>\n<user_query>\n…\n</user_query>`, sometimes
after attached-skill text.

A subagent chat carries `subagentInfo` in the store's meta row (the agent's own constructor
defaults it to `undefined`). None of the 9 local stores had one. *(Unverified: what a subagent
chat looks like on disk.)* A `--print` run may persist no chat at all: persistence is
conditional in the bundle (`… || CURSOR_AGENT_PERSIST_SESSION ? initAndLoad : …`).

### What the picker shows

There is no index apart from the chat store. `agent-cli-state.json` holds UI flags, and
`ai-tracking/ai-code-tracking.db` is code-attribution tracking (its `conversation_summaries`
table was empty). `agent ls` lists the chats under the cwd's md5 directory, so moving a chat
directory away is the whole of hiding it. *(Unverified: whether the picker reads `meta.json`
or opens `store.db`.)*

### Tokens

Not stored as spend. The root state's `token_details` is `ConversationTokenDetails{used_tokens,
max_tokens, breakdown}`, the context-window fill (for example 181216 of 300000), not input,
output or cache counts. Per-call usage (`inputTokens`, `outputTokens`, `cacheReadTokens`,
`cacheWriteTokens`) reaches only `--print --output-format json` stdout and telemetry, gated by
`CURSOR_EMIT_MODEL_CALL_USAGE`. `rl usage` therefore reports nothing for Cursor.

### Hooks and skills

User hooks are read from `$HOME/.cursor/hooks.json` whatever `CURSOR_CONFIG_DIR` says:
`{version: 1, hooks: {<camelCaseEvent>: [entry, …]}}`, each entry a bare
`{type, command, timeout, failClosed?}` with no matcher group. The session-end event is
`sessionEnd`. Cursor also loads Claude Code's hooks, from `~/.claude/settings.json` and a
project's `.claude/settings.json` and `.claude/settings.local.json`. *(Unverified: whether a
Claude `SessionEnd` hook runs under Cursor's `sessionEnd`, so ringleader's sweep may run twice
on a machine with both installed. The sweep is idempotent.)*

Skills are read from `.cursor/skills` and `.agents/skills` as first-party, from `.claude/skills`,
`.codex/skills` and `.grok/skills` as third-party, and from the built-in `.cursor/skills-cursor`.
*(Unverified: how it resolves one name present in two of them.)*

### Commands

- `cursor-agent [prompt]`: start interactive, optionally with an opening prompt.
- `cursor-agent --resume [chatId]`: picker without an id, direct resume with one. `resume`
  resumes the latest chat, and `--continue` continues the previous one.
- `cursor-agent ls`: the resume picker.
- `cursor-agent create-chat`: an empty chat, printing its id.
- `cursor-agent --workspace <path>`: the workspace to use instead of the cwd.
- `cursor-agent -p`: non-interactive (`--output-format text|json|stream-json`).
- `cursor-agent -w [name]`: start in a git worktree under `~/.cursor/worktrees/<repo>/<name>`.

There is **no fork, rename, archive or delete verb**, and the agent exports no chat id into its
shells (`CURSOR_AGENT` and `CURSOR_PROJECT_DIR` are set; `CURSOR_CONVERSATION_ID` is read only
on the cloud path). Hooklinesinker reports live Cursor sessions as `cursor`.

## Implications for ringleader

1. **Never derive a cwd from a directory name.** Claude's encoding is lossy and truncating; Droid
   has its own, similarly lossy encoding; Pi's differs; OpenCode has none. Always read the cwd
   from inside the session (Claude: `relocatedCwd` from the tail, else `cwd` from the head).
   `slug.py` now holds both Claude's and Droid's encoders, reached through each source's
   `storage_for()`: each is its own caller to break, so re-verify both independently when
   either CLI ships.
2. **A Claude transcript's path is mutable.** Relocation moves the file between project directories
   mid-session, so a path-keyed registry entry can dangle without any user action.
3. **File deletion is not delete-everywhere.** It is for Claude Code and Pi. It is not for Codex —
   `rl prune` on Codex sessions leaves picker-visible `threads` rows pointing at missing files, and
   ~450 such rows already exist locally. Codex pruning should go through `codex delete`/`archive`,
   and must do so **before** touching the file, or the CLI refuses. **Droid is unresolved**: nothing
   beside its transcripts points at them, but its binary ships a gated session index at
   `<factory>/cache/session-index/index.db` that this machine's sandbox cannot read (see "What the
   picker shows"). Treat droid as Codex-shaped until someone can read that file.
4. **Ringleader owns archive; only index-clearing is per-source.** `codex archive` would do the
   whole job for Codex — it moves the transcript to `~/.codex/archived_sessions/` and flips the
   index bit reversibly — but delegating to it makes the archive location and semantics diverge per
   agent, adds a CLI dependency, and can't touch rows whose file is already gone. Instead
   `hides_from_picker` on the `Source` seam clears the agent's own index entry (Codex: set
   `threads.archived = 1`; Claude Code, Droid and Pi: no-op), and ringleader's own move stays the
   implementation everywhere.
5. **Fork/branch is universal but shaped differently** — Claude and OpenCode fork the whole session
   under a new id, Codex and Droid fork from a picker or an id, Pi distinguishes in-file branching
   (`/tree`) from new files (`/fork`, `/clone`). `rl resume` offers a same-agent fork through
   per-source native CLI commands; see [Resuming sessions](../features/resuming-sessions.md).
   Pi’s CLI `--fork` copies the session entries to a fresh file and continues at its current
   leaf, unlike the earlier-prompt selector in interactive `/fork`.
6. **Liveness has a source of truth per agent**: Claude `~/.claude/sessions/<pid>.json`
   (`status`, `updatedAt`), Codex `threads.recency_at_ms`, OpenCode `session.time_updated`. Useful
   for not curating a session someone is currently sitting in. *(Unverified for Droid and Pi — no
   liveness signal was surveyed for either; ringleader gets both from hooklinesinker instead.)*

7. **Droid needs no per-agent write path, unless its session index is live.** No verified index
   to clear, no append surface to rename through, no archive verb to delegate to — the only
   droid-specific knowledge ringleader carries is where the files are, that the first line holds
   the identity, and that two `.settings.json` sidecars travel with each transcript.

8. **A Cursor session is two directories in two roots.** The transcript is the session file and
   the chat directory its companion, so archive and unarchive ask the source where a companion
   goes (`companion_destination`) rather than assuming it sits beside the transcript. Workspace
   moves rename both `chats/<md5>` and `projects/<slug>` and rewrite `meta.json`'s `cwd`. The
   protobuf state's `previous_workspace_uris` keeps the old path; whether the agent cares is
   *(unverified)*, as no real chat was resumed after a move.
