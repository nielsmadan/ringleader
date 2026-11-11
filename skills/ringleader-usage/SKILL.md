---
name: ringleader-usage
description: Explain where an agent session's tokens went and how to spend fewer — reads local usage across Claude Code, Codex, Droid, OpenCode, and Pi. Use when the user asks "what used all my tokens", "why was that session so expensive", "token usage", "usage report", or invokes usage.
---

# Usage

Everything about calling `rl` and JSON conventions is in `references/playbook.md`; this
file adds the flow. Read-only — this skill never deletes or edits sessions.

## Flow

1. **Survey.** Run `rl usage --breakdown --json`. Note the heaviest projects and the
   input/cache_read/output split. A very low `cache_read` share relative to `input` means
   the prompt cache is missing.
2. **Windows.** If the user is asking about limits, run `rl usage windows`. It reports
   Codex's 5h/weekly consumption directly (no setup needed), and Claude's too once
   captured. If Claude shows as uncaptured, tell the user to run `rl install statusline`
   (one-time, wires a capture snippet into their statusLine command) rather than falling
   back to `claude /usage`.
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

- Tokens are the unit of truth. Cost is an estimate — present it as one, and omit it when
  `rl` reports none.
- Per-tool token attribution does not exist on disk. Do not invent it; reason from turn
  totals and the transcript instead.
- **Droid is never priced** — Factory bills its own credits, so `rl` reports no cost for it and
  neither should you. Its day and model groupings are approximations (one per-session record,
  stamped at the last write, credited to the model its router last chose); say so rather than
  presenting them as measured.
