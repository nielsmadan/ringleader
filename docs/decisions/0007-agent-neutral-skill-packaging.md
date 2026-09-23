# 0007: Ship agent-neutral skills, not a Claude-only plugin

## Context

The skills started as a Claude Code plugin with a Claude-specific manifest and installer. But
ringleader reads sessions from Claude Code, Codex, OpenCode and Pi — Factory Droid joined later
— and its usage analytics span all of them. A tool that curates every agent's sessions but can
only be *driven* from one of them is wired backwards.

## Decision

Skills live at `.agents/skills/ringleader-*`, each shipping an agent-neutral `SKILL.md`, and
each that needs the shared protocol also bundles a `references/playbook.md` synced from the
canonical `.agents/skills/PLAYBOOK.md` (`just sync-playbook`). `rl install skills` symlinks them into `~/.agents/skills` (Codex,
OpenCode, Pi) and `~/.claude/skills` (Claude Code).

The Claude-only plugin manifest and its installer were removed.

## Consequences

- One skill body, every supported agent; the playbook is written once and synced rather than
  duplicated.
- **Adding an agent applies this decision rather than reopening it.** Droid joined in 2026-09 and
  took `~/.factory/skills`, its own native personal skills directory, so the rule the installer
  follows is: an agent's native personal skills dir when it has one (Claude Code, Droid), the
  shared `~/.agents/skills` otherwise (Codex, OpenCode, Pi).
- `rl install` / `rl update` / `rl uninstall` / `rl doctor` became the install surface, so
  wiring is inspectable and reversible.
- Symlinks mean a repo edit is live in every agent immediately — good for iteration, and the
  reason `rl doctor` reports what is linked where.
- The earlier single-purpose skills (sync, overview, name, harvest, triage) were folded into
  the task-shaped set. See [features/agent-integration.md](../features/agent-integration.md).
