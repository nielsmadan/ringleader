---
name: ringleader-usage
description: Explain where an agent session's tokens went and how to spend fewer — reads local usage across Claude Code, Codex, Droid, OpenCode, and Pi. Use when the user asks "what used all my tokens", "why was that session so expensive", "token usage", "usage report", or invokes usage.
---

# Usage

Everything about calling `rl` and JSON conventions is in `references/playbook.md`; this
file adds the flow. Read-only — this skill never deletes or edits sessions.

`rl usage local` is the explicit offline entry point. `rl usage local session`,
`rl usage local windows` and `rl usage local export` retain the same arguments and output
as their root aliases; bare `rl usage` also remains local.

## Flow

1. **Survey.** Run `rl usage --breakdown --json`. Note the heaviest projects and the
   input/cache_read/output split. A very low `cache_read` share relative to `input` means
   the prompt cache is missing.
2. **Windows.** If the user is asking about limits, run `rl usage windows`. It reports
   Codex's 5h/weekly consumption directly (no setup needed), and Claude's too once
   captured. `rl install statusline` enables local Claude captures with a one-time
   statusLine snippet. To consult saved remote reports, use `rl usage subscription
   --cached`. For a requested live check, use `rl usage subscription`, optionally
   selecting each `--source claude` or `--source codex`. Use `--refresh` to bypass
   freshness checks. Keep stale or expired quotas clearly labeled.
   For OpenRouter spend, activity, or credits, use `rl usage token --cached` for saved
   results and `rl usage token` for a requested live check. Select an individual
   `--report key`, `--report activity`, or `--report credits` as needed. Keep API key
   caps, management-visible activity, and the authenticated user's balance separate.
3. **Drill in.** For each heavy session, run `rl usage session <id> --json`: turns, the
   full token split, cache hit rate, the top turns, and context growth. `turns` counts every
   turn, subagents included; `main_turns` counts the main thread, and `top_turns` and
   `context_growth` cover the main thread only, indexed into its turns. A **droid** session
   reports a single turn holding the whole spend — droid records no per-turn counts — so skip
   the top-turns analysis there and reason from the transcript instead.
4. **Where it went wrong.** To see which tools, skills and MCP servers a spend belongs to,
   and where sessions hit errors, retries or cache rebuilds, run `rl analyze --json` for the
   corpus and `rl analyze session <id> --json` for one session. Both read a local index that
   the first run builds, so say so before a cold run on a large history. For errors that
   recur across sessions, run `rl analyze issues --json`: each issue groups one normalized
   error signature (`command class|category|first error line`, where the first field falls back
   to the tool name when the call ran no command; user-caused categories such as
   `user_rejected` or `permission_denied` are not agent failures) and reports how many
   distinct sessions hit it. The `embed` key says whether near-duplicate signatures were
   merged; without the optional `embed` extra only exact signatures group, and `reason` says
   why. `--download-embed-model` fetches the model once, only if the user asks for it.
   To turn that into a written report of what went wrong, hand off to `ringleader-analyze`;
   `ringleader-recommend` then proposes edits from its findings.
5. **Explain.** Read the session transcript around the top turns (`rl data export <id>`)
   and name the *specific* cause — a file re-read many times uncached, a huge tool result
   pasted back each turn, a long session whose context grew monotonically.
6. **Recommend.** Give concrete, checkable advice tied to what you found: narrow the reads,
   summarize and restart the thread, avoid re-reading a large file, keep the stable prefix
   stable so the cache holds.

## Notes

- Use tokens as the primary unit for local usage. Label local price estimates and omit cost
  when `rl` reports none. OpenRouter spend and credit figures come from provider-reported data.
  Include the report's scope and fetch time when summarizing them.
- Per-tool token attribution does not exist on disk. Do not invent it; reason from turn
  totals and the transcript instead.
- **Droid is never priced** — Factory bills its own credits, so `rl` reports no cost for it and
  neither should you. Its day and model groupings are approximations (one per-session record,
  stamped at the last write, credited to the model its router last chose); say so rather than
  presenting them as measured.
- **Cursor CLI has no usage on disk** — it keeps only the context-window fill, so `rl usage`
  never lists it. Say that rather than estimating its spend.
