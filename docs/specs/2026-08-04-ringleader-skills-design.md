# ringleader Skill Layer — Design

**Goal:** Replace the ad-hoc session skills with a coherent, portable-by-design skill layer — `init` · `triage` · `cleanup` · `overview` — that drives ringleader's three use cases (first audit, end-of-day re-triage, morning overview) through the `rl` CLI, with the deterministic work in `rl` and only agent-specific glue in per-agent wrappers.

**Architecture:** Each skill is an **agent-neutral playbook** (the WHAT) plus a thin **per-agent wrapper** (the HOW). Two named enhancement points — `[SELECT]` (batch selection) and `[FAN-OUT]` (parallel dispatch) — are mapped by the wrapper to that agent's mechanism (`AskUserQuestion` / subagents on Claude), each with a documented fallback so a bare agent still works. Ships the Claude wrapper now; other-agent wrappers are thin additions once that agent's *source* lands.

**Tech stack:** Markdown skills (playbook + Claude-format `SKILL.md`) packaged as a `ringleader` Claude Code plugin; supporting additions to the stdlib Python `rl` CLI.

## Context — where this sits

- Builds on the shipped grouped `rl` surface (`session refresh/set/archive/…`, `mem …`, `data …`).
- **Independent of** the pending Codex-source spec and the LLM spec: these skills are the *agent-driven* path and work against the current `rl` today.
- Relationship to future work: the deterministic logic pushed into `rl` here is the same logic the **TUI (Spec 3)** will drive; tool-native-LLM harvest (**Spec 2**) and a **Codex wrapper** (after the Codex source) are later, not here.

## Cross-agent model

- **`PLAYBOOK.md`** (one shared file) holds agent-neutral conventions: how to call `rl`, the strategic-ordering rules, progress reporting, the harvest sub-protocol, and the two enhancement points with their fallbacks.
  - **`[SELECT]`** — "present this batch, get the user's per-item choice." Claude → `AskUserQuestion`. Fallback → print a numbered list, ask the user to reply with the numbers/letters.
  - **`[FAN-OUT]`** — "run these independent read-only extractions concurrently." Claude → parallel subagents. Fallback → do them sequentially.
- **Per-agent wrapper** — each skill is a thin `SKILL.md` (Claude format) that states its flow and names the Claude mechanism for each enhancement point. A future Codex/other wrapper is the same flow in that agent's skill format, pointing at the same `PLAYBOOK.md`.
- **Push logic into `rl`.** Wherever orchestration is deterministic (ordering, batching, recency filtering, progress counts, snoozed listing), it lives in `rl`, keeping wrappers thin and reusable by the TUI.

## Packaging & fate of the existing skills

Ships as a `ringleader` plugin with **four** skills. The six current skills reconcile:

| Existing | Fate |
|---|---|
| `session-triage` | **replaced** by `triage` + `cleanup` |
| `session-overview` | **replaced** by `overview` |
| `session-sync` | **folded in** as an inline step (auto-refresh + classify) |
| `session-name` | **folded in** as an inline step (name open sessions) |
| `session-harvest` | **folded in** to the harvest phase of `triage`/`cleanup` |
| `memory-triage` | **out of scope** — left as-is; memory is a separate track (future `ringleader:mem`) |

## Shared conventions (`PLAYBOOK.md`)

- **Calling `rl`:** installed `rl`, or `uv run rl` from a source checkout. Structured data comes from `rl … --json` (which includes `sessionId`); never hand-edit `registry.json`.
- **Strategic ordering (volume):** auto-prune the tiny/stale junk first, then work **project-by-project in bounded chunks**, oldest activity first.
- **Progress always visible:** a running tally each step, e.g. `Project 3/12 · 47/210 triaged · 12 harvested`, so the user doesn't give up mid-run.
- **Harvest sub-protocol** (used by `triage`/`cleanup`):
  1. **Integration target is prompted, not hardcoded.** Ask the user how they want durable knowledge integrated; default and this user's answer: the `/doc` skill, keeping its layout. Stored as the run's harvest method so the skill stays distributable.
  2. **Where to write — reuse-clean-else-fresh.** For a finished session's repo, locate a checkout (registry `cwd`/`gitRoot`, `rl overview --checkouts`). If its working tree is **clean**, write the doc edits there. If **dirty or absent**, create an isolated **git worktree** in a temp dir, edit there, and report the path. Always confirm before writing; leave edits uncommitted for review.
  3. **Two phases.** `[FAN-OUT]` **extract** durable knowledge per session (read-only, into temp nuggets files), then **synthesize once per repo** (single writer) via the integration target, then `rl session mark harvested <id> --paths …` + `rl session archive <id> --summary-file …`.

## The four skills

### `ringleader:overview` — morning read
1. `rl overview` (auto-refresh).
2. If any untriaged session is **older than 24h**, surface the count and `[SELECT]` offer to drop into `triage`.
3. Collect **open sessions from the last 3 days** (`rl overview --recent 3d --json`); summarize each and what was last worked on.
4. List **snoozed** sessions with wake times (`rl overview --snoozed`).
5. `[SELECT]` pick next / snooze (`rl session snooze`, with a duration) / send to triage. Read-only except snooze.

### `ringleader:triage` — recurring inbox (scope: recent)
1. Auto-prune junk (`rl prune`; preview, confirm, `--delete`).
2. Build the inbox recent-bound, grouped by project (`rl triage --recent <Nd> --json`).
3. Per project (progress tally): `[SELECT]` **delete / finished / open** per session.
4. Apply — delete now; *finished* → harvest queue; *open* → classify (`rl session set …`).
5. **Harvest** the finished set via the harvest sub-protocol.
6. Name + review the open set (`rl session name`, then `rl session mark reviewed <id>`).
7. Record the run (`rl session mark cleaned <project>`), report the tally.

### `ringleader:cleanup` — the big backlog (scope: all)
Same shared playbook as `triage` with `scope: all` — unbounded, tuned for volume: full inbox, project-by-project in bounded chunks, heavy progress, and **resumable** (the registry already records what's triaged, so re-running continues where it stopped). Two entry points, one core.

### `ringleader:init` — onboarding
1. Verify `rl` is installed (else note `uv run rl`).
2. **Settings check** (`rl doctor`): each installed harness's session-retention setting so sessions aren't GC'd (Claude's `cleanupPeriodDays`); offer guidance to bump it.
3. Kick off a **bounded first triage** (last 7 days) via the triage playbook.
4. Point the user at `cleanup` for the backlog.

## `rl` additions (this spec includes code, not just skills)

- **`--json` includes `sessionId`** on `triage`/`overview` (verify/ensure) so skills stop parsing `registry.json`.
- **`--recent <Nd>`** recency filter on `triage`/`overview` (filters threads by activity mtime within N days).
- **`rl overview --snoozed`** — list snoozed threads with wake times.
- **`rl doctor`** — check each installed harness's session-retention setting and report; cross-agent and reusable (used by `init`). For Claude, read `cleanupPeriodDays` from `~/.claude/settings.json` (default 30) and warn if low/unset.

## Testing

- **`rl` additions** get pytest: `--recent` filtering boundaries, `--json` includes ids, `--snoozed` output, `rl doctor` reads/reports the retention setting (against a temp settings file).
- **Skills** (markdown) get a documented smoke checklist run against the live registry: `overview` renders + offers triage when stale; `triage` completes a small recent scope end-to-end; harvest writes to a clean checkout and to a worktree when dirty; `init` reports settings + runs a bounded pass. No unit framework for markdown; correctness is the `rl` tests plus the smoke run.

## Out of scope

- `memory-triage` / the memory track (future `ringleader:mem`).
- The Codex (or any non-Claude) skill wrapper — follows that agent's source.
- The TUI (Spec 3) and tool-native-LLM harvest (Spec 2).
