# Claude Limits Capture — Design

**Goal:** Make `rl usage windows` report Claude's real 5-hour and 7-day subscription
windows alongside Codex's, by capturing the `rate_limits` block Claude Code pipes to a
`statusLine` command and reading it back from disk.

**Scope:** Sub-project 2 of the usage feature. Sub-project 1 (`rl usage`, shipped) reads
token usage from local transcripts for four harnesses and already reports Codex's
rate-limit windows from disk. Claude is the only harness still reporting nothing, so this
spec is Claude-only by necessity, not by omission:

| Harness | Subscription windows | Position after this spec |
|---|---|---|
| Codex | 5h + weekly | **Already shipped.** Codex persists `rate_limits` to its rollout files; the reader consumes them directly. No capture step, no change here. |
| Claude | 5h + 7-day | **The gap this spec closes.** Not persisted anywhere on disk; requires the statusLine capture below. |
| OpenCode | — | Not applicable. BYO-key/OpenRouter model, no subscription-window concept exists. |
| Pi | — | Not applicable. Same. |

Nothing in this spec modifies the Codex path.

## The data, and why a capture step is needed

Claude Code pipes a session JSON to the `statusLine` command on stdin. Since
**v2.1.80**, and **only for Claude.ai Pro/Max subscribers**, that payload carries:

```json
"rate_limits": {
  "five_hour": { "used_percentage": 23.5, "resets_at": 1738425600 },
  "seven_day": { "used_percentage": 41.2, "resets_at": 1738857600 }
}
```

`used_percentage` is 0-100; `resets_at` is Unix epoch seconds.

This is the **only** local mechanism that exposes the data. Verified against the official
docs and confirmed by survey:

- **Hooks do not carry it.** No hook event (~30 of them: SessionStart, UserPromptSubmit,
  PreToolUse, PostToolUse, Stop, PreCompact, …) includes rate-limit, quota, or cost
  fields. Their common payload is session/cwd/permission-mode/effort only.
- **OTLP telemetry does not carry it.** `CLAUDE_CODE_ENABLE_TELEMETRY` exports token and
  cost metrics; the docs state "No rate limit capture".
- **No file under `~/.claude` holds it.** Not settings, caches, state, or the transcript
  JSONLs. The state lives in memory for the session's lifetime.

Hence: a `statusLine` writer captures the block to a file; ringleader reads that file.

### Prior art

Three approaches exist in the wild, and only the first is both official and offline:

| Approach | Used by | Assessment |
|---|---|---|
| statusLine `rate_limits` | claude-pace, Claude-Code-Usage-Monitor, token-oracle, tokenfold | Official, no network. **Adopted.** |
| JSONL 5-hour block inference | ccusage | No setup, but does not match `/usage` (anthropics/claude-code#20636). Rejected — ringleader already reports tokens from JSONL and must not present an inferred number as an official limit. |
| Undocumented `/api/oauth/usage` | ccseva, TokenEater, Maciek's opt-in `--api` | Exposes more (extra-usage credits, per-model weekly) but is undocumented, needs the OAuth token, and reportedly rate-limits aggressively (#31637). Rejected — ringleader stores no secrets. |

`Claude-Code-Usage-Monitor` (8.6k stars) implements the adopted approach and its
`official.py` encodes three hazards this design inherits: a Claude Code bug where
`used_percentage` carries the `resets_at` epoch instead of a percentage
(anthropics/claude-code#52326), `NaN`/`Infinity` values that Python's `json` accepts by
default, and a freshness TTL (theirs: 600s).

## Architecture

Two halves, deliberately decoupled — the writer must not depend on ringleader.

```
writer  (pure jq, inserted into the user's statusLine command)
          -> $XDG_DATA_HOME/ringleader/claude-limits.json   (atomic write)
reader  ringleader/usage/claude_limits.py
          -> WindowSnapshot[]  -> rl usage windows
installer ringleader/commands/statusline.py
          -> rl statusline install | status | uninstall
```

**The writer is pure `jq`, not `rl`.** `statusLine` runs on every render; spawning Python
each time would add ~150ms of interpreter startup to every prompt. `jq` is already
required by the typical statusLine script, so this adds no dependency.

## Components

### 1. The writer snippet

Inserted into the user's existing `statusLine` command, immediately after it captures
stdin:

```sh
printf '%s' "$input" | jq -c '{captured_at: (now|floor), rate_limits}' \
  > "$RL_DIR/claude-limits.json.tmp" 2>/dev/null \
  && mv -f "$RL_DIR/claude-limits.json.tmp" "$RL_DIR/claude-limits.json"
```

- **Atomic** (temp + rename): the file is rewritten on every render, so a torn read is a
  live risk for a reader that runs concurrently.
- **`captured_at`** is stamped by the writer, because the payload carries no timestamp of
  its own and freshness cannot otherwise be judged.
- **Silent on failure** (`2>/dev/null`, no `set -e` interaction): a capture problem must
  never break the user's status line.
- Writes `rate_limits: null` when the key is absent — a fresh session before its first
  API response — rather than writing nothing, so the reader can distinguish
  "captured, not yet populated" from "never captured".

### 2. The reader — `ringleader/usage/claude_limits.py`

`read_windows(path, now) -> list[WindowSnapshot]`, emitting the same `WindowSnapshot`
dataclass the Codex reader produces (`source="claude"`, `window` in `{"5h", "weekly"}`),
so the existing rendering and staleness logic apply unchanged.

Hardening, each guarding a known failure:

- **Epoch-shaped percentage** (#52326): `used_percentage` sometimes carries the
  `resets_at` epoch (~1.7e9) instead of a percentage. Concretely: a value `> 1000` is
  treated as the leaked epoch and the window is **dropped**, rather than rendered as a
  bogus 100%.
- **Rounding artifact**: a value `> 100` but `<= 1000` is a rounding overshoot and is
  **clamped** to 100. The two thresholds are disjoint, so every out-of-range value has
  exactly one defined outcome.
- **Non-finite / wrong-typed numbers**: reject `NaN`, `Infinity`, and `bool` before any
  arithmetic or comparison.
- **Freshness TTL**: a capture older than **600 seconds** is stale. This is separate from
  and additional to the existing `resets_at` expiry check.
- Missing file, unreadable file, malformed JSON, `rate_limits: null`, or one window
  absent — all degrade to "no data" for the affected window; never raise.

### 3. The installer — `rl statusline install | status | uninstall`

A sibling of `rl skills`, mirroring its verbs and its confirm-before-touching contract.
Named `statusline`, not `hooks`, because a hook cannot carry this data and the name
should not imply otherwise.

`~/.claude/settings.json` is a hand-maintained file. The installer therefore:

- **Shows the exact diff and asks** before writing (`--yes` to skip, matching
  `rl skills install`).
- **Backs up** the original next to it before modifying.
- **Inserts only**: it adds the snippet to the existing `statusLine` command and changes
  nothing else. It never rewrites, reformats, or replaces the user's script.
- **Refuses rather than guesses** when no `statusLine` is configured or the command's
  shape is unrecognized: it prints the snippet and where to paste it.
- **`uninstall`** removes only the inserted snippet, leaving the rest intact.
- **`status`** reports whether the snippet is installed, whether a capture file exists,
  its age, and whether the windows in it are current.

### 4. `rl usage windows`

Gains a Claude section beside the Codex one, replacing today's hardcoded
`{"available": false, "reason": "not persisted to disk"}`. Same shape and same staleness
treatment as Codex, so a stale or expired Claude window reads
`no current snapshot`, never a silently old number.

When nothing has been captured, the command says so **and** points at
`rl statusline install` — the actionable next step — rather than repeating that the data
is unavailable.

## Honesty rules (inherited, and extended)

Sub-project 1 established that ringleader never fabricates a number: unknown cost is
`null`, not `0.0`, and an expired window says so. This spec extends that to provenance:

- A displayed Claude window is **official** — captured from Claude Code — or it is not
  displayed. No inference from JSONL, and no estimate presented as a limit.
- The **capture age is surfaced**, because a statusLine-sourced number is only as fresh
  as the last render of a Claude session.

## Error handling / degradation

| Condition | Behaviour |
|---|---|
| Snippet not installed / no capture file | "no claude window data" + pointer to `rl statusline install` |
| Capture exists, `rate_limits` null | Reported as captured-but-not-yet-populated (fresh session) |
| Capture older than the TTL | Window reported as stale, not current |
| `resets_at` in the past | `no current snapshot` (existing behaviour) |
| Malformed JSON / unreadable / bad types | Treated as no data; never raises |
| Free tier or pre-v2.1.80 Claude Code | `rate_limits` never appears; reported as unavailable |

## Testing

Hermetic throughout: the reader takes its file path as a parameter and `now` as an
argument, so no test reads a real capture or calls `datetime.now()`.

- Reader: well-formed capture → two windows; epoch-shaped `used_percentage` dropped;
  `NaN`/`Infinity`/bool rejected; >100 clamped; stale-by-TTL flagged; expired
  `resets_at`; missing file; malformed JSON; `rate_limits: null`; one window absent.
- Installer: inserts into a representative `statusLine` command in a tmp settings file;
  is idempotent; `uninstall` restores the original byte-for-byte; refuses when no
  `statusLine` exists; never touches unrelated settings keys. Never reads or writes the
  real `~/.claude/settings.json`.
- Command: Claude and Codex sections render together; Claude-absent path; `--json` shape.
- Writer snippet: executed as a shell fixture against a sample payload, asserting the
  file contents and that a missing `rate_limits` yields `null` — this is the one piece
  ringleader ships but does not itself run.

## Out of scope

- The `/api/oauth/usage` endpoint, and anything requiring the OAuth token or a stored
  credential.
- Inferring Claude's windows from JSONL when no capture exists.
- Per-model weekly limits and extra-usage credit balance — not in the statusLine payload.
- Installing a statusLine for a user who has none configured beyond printing the snippet.

## Known limitations (to document, not solve)

- **Multi-account ambiguity.** The statusLine payload carries no account identity, so
  with several Claude accounts the capture reflects whichever account last rendered a
  status line, with no way to tell which. A reading can be wrong and look normal
  (anthropics/claude-code#13585). Document it; do not pretend otherwise.
- **Freshness is bounded by usage.** The numbers only update while a Claude session is
  running. After a quiet day the last capture is stale by definition — hence the TTL.
- **Requires Claude Code v2.1.80+ and a Pro/Max subscription.**
- **Codex's 5-hour window is dead data upstream, and stays that way.** Codex now
  populates only the weekly (`primary`) slot; the last 5h (`secondary`) snapshot on this
  machine is from 2026-06-05. `rl usage windows` already reports it honestly as
  `no current snapshot` rather than showing the stale value. This is not fixable from
  disk — if Codex stops writing a window, there is nothing to read — and Codex offers no
  statusLine equivalent to capture it live. Out of scope, recorded so it is not mistaken
  for a regression.
