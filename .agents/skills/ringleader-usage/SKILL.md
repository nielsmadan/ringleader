---
name: ringleader-usage
description: Explain where an agent session's tokens went and how to spend fewer — reads local transcript usage across Claude Code, Codex, OpenCode, and Pi. Use when the user asks "what used all my tokens", "why was that session so expensive", "token usage", "usage report", or invokes usage.
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
   full token split, cache hit rate, the top turns, and context growth.
4. **Explain.** Read the session transcript around the top turns (`rl data export <id>`)
   and name the *specific* cause — a file re-read many times uncached, a huge tool result
   pasted back each turn, a long session whose context grew monotonically.
5. **Recommend.** Give concrete, checkable advice tied to what you found: narrow the reads,
   summarize and restart the thread, avoid re-reading a large file, keep the stable prefix
   stable so the cache holds.

## Notes

- Tokens are the unit of truth. Cost is an estimate — present it as one, and omit it when
  `rl` reports none.
- Per-tool token attribution does not exist on disk. Do not invent it; reason from turn
  totals and the transcript instead.
