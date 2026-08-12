# Multi-Agent Skill Packaging — Design

**Goal:** Ship ringleader's four skills to Claude Code, Codex, OpenCode, and Pi from the one repo, version-locked to the `rl` CLI, installed by a consent-first `rl skills` command — no per-agent manifests or separate marketplace.

**Architecture:** The skills become agent-neutral, spec-conformant `SKILL.md` folders under `.agents/skills/`, each self-contained (its own bundled `references/playbook.md`). A hand-rolled `rl skills install/update/status/uninstall` command detects installed agents and symlinks the `ringleader-*` skill dirs into the shared `~/.agents/skills/` (read by Codex/OpenCode/Pi) and `~/.claude/skills/` (Claude). No new runtime dependencies.

**Tech stack:** stdlib Python 3.13 (`rl skills`); Markdown skills in the `SKILL.md` open format (agentskills.io); `pytest`, `ruff`.

## Context & rationale

- `SKILL.md` is a shared open standard; Codex/OpenCode/Pi all natively read `~/.agents/skills/`, and Claude reads `~/.claude/skills/`. So one agent-neutral copy per skill, symlinked into two dirs, covers all four agents.
- The skills are the CLI's agent interface and are **version-coupled** to `rl` (they call `rl session set`, `rl overview --recent`, …), so they ship *with* the CLI and are installed *by* it — the shell-completions / git-hooks pattern. No standalone marketplace (dropped; see below).
- Research found no reusable Python installer library worth depending on; the fan-out is the easy subset (local, bundled skills — no remote fetch/trust/registry), so it is hand-rolled in stdlib.

## Decisions (settled)

- **Canonical location + namespace.** Skills move to `.agents/skills/ringleader-{init,triage,cleanup,overview}/`. The `ringleader-` prefix avoids collisions in the shared `~/.agents/skills/` and scopes install/uninstall so the command only ever touches `ringleader-*` links.
- **Agent-neutral bodies.** Remove the "This is the Claude wrapper: map `[SELECT]` to `AskUserQuestion`…" framing. Replace with neutral phrasing that names Claude only as an example: `[SELECT]` → your native structured-choice tool (Claude Code: `AskUserQuestion`; otherwise a numbered list); `[FAN-OUT]` → run in parallel if your agent supports it, else sequentially. Generalize "Claude Code sessions" → "agent sessions" (Codex sessions are in scope). One copy serves all four agents.
- **PLAYBOOK bundled per skill.** Each skill dir is self-contained: `SKILL.md` + `references/playbook.md`. One canonical `PLAYBOOK.md` is the source; a dev sync step copies it into each skill's `references/playbook.md`, and a test guards against drift. Skills reference `references/playbook.md` (a skill-relative path that always resolves after any install or copy).
- **Install by symlink**, not copy, so skills track the installed `rl` version.
- **Drop the Claude `.claude-plugin/` marketplace channel** and the `just install-plugin` target. `rl skills install` is the single uniform path (it covers Claude via `~/.claude/skills/`). A marketplace can be added later if non-`rl` users ever need it.

## Components

### 1. Relocated, neutralized skills — `.agents/skills/ringleader-*/`

- `SKILL.md` frontmatter uses only the open-spec fields (`name`, `description`); `name` matches the dir (`ringleader-triage`, …).
- Bodies neutralized per the decision above; every `rl …` reference stays valid against the current CLI surface.
- `references/playbook.md` in each skill (the bundled playbook copy).

### 2. Canonical playbook + sync

- One source-of-truth `PLAYBOOK.md` in the repo (not itself a skill, not shipped to agents).
- A `just sync-playbook` (or equivalent) copies it verbatim into each `ringleader-*/references/playbook.md`.
- A pytest (`test_playbook_in_sync`) asserts each skill's `references/playbook.md` is byte-identical to the canonical — drift fails CI.

### 3. `rl skills` command (`ringleader/commands/skills.py`)

An **agent registry** maps each supported agent to its skill directory and a detection check:

| Agent | Detected by | Installs into |
|---|---|---|
| claude | `~/.claude/` exists | `~/.claude/skills/` |
| codex / opencode / pi | any of `~/.codex/`, `~/.config/opencode/`, `~/.pi/` exists | `~/.agents/skills/` (shared) |

(The exact per-agent dirs are verified against the live CLIs in the implementation plan; the registry is a small table so adding/adjusting a dir is trivial. `~/.agents/skills/` is the shared dir Codex/OpenCode/Pi read.)

Subcommands:
- **`rl skills install [--yes]`** — detect agents, compute the plan (which `ringleader-*` skills → which dirs), **print it and prompt `[y/N]` before writing** (never silently touches `$HOME`). `--yes` skips the prompt (for CI and for `ringleader-init`, which asks the user via `[SELECT]` first). Idempotent: creates missing links, repairs broken ones, skips correct ones.
- **`rl skills update [--yes]`** — reconcile wired links with the current bundled set: add newly-shipped skills, remove links for skills no longer bundled, repair broken links. Same confirm-first behavior. (Shares the reconcile core with `install`.)
- **`rl skills status`** — list every bundled `ringleader-*` skill, where each is linked, which agents see it, and flag broken/missing links.
- **`rl skills uninstall [--yes]`** — remove ringleader's links (confirm-first). Only removes symlinks whose name is `ringleader-*` and whose target is inside ringleader's bundled skills dir — never other skills sharing the dir.

Where the bundled skills live at runtime: the installed package's data dir (source checkout: the repo's `.agents/skills/`; `uv tool install`: the package location). `rl skills` resolves that path and symlinks from it.

### 4. `ringleader-init` integration

The `init` skill's onboarding gains a step: after `rl doctor`, offer to wire the skills — `[SELECT]` consent, then run `rl skills install --yes`.

### 5. Docs

- `README.md` "Skills" section: the four skills + `rl skills install`/`update`/`status`/`uninstall` (replacing `just install-plugin`).
- `AGENTS.md`: note the `.agents/skills/` layout + the `rl skills` install path; that it ships to Claude/Codex/OpenCode/Pi.
- `plugin/SMOKE.md` → moves alongside the skills (e.g. `.agents/skills/SMOKE.md`), updated for the new command.

## Testing

- **`rl skills`**: install into a temp `$HOME` (monkeypatched) → asserts the right symlinks in the right dirs; idempotent re-run is a no-op; `--yes` bypasses the prompt; `update` adds/removes/repairs; `uninstall` removes only `ringleader-*` links and leaves a foreign skill dir untouched; `status` reports linked/broken correctly; the confirm prompt aborts cleanly on `n`.
- **Playbook sync**: `test_playbook_in_sync` (byte-identical copies).
- **Skill hygiene**: frontmatter uses only spec fields; each skill body defines the `[SELECT]`/`[FAN-OUT]` fallbacks and contains no "Claude wrapper"-style hard mapping (Claude may appear only as an example); every `rl …` referenced resolves to a real command.

## Out of scope

- A Claude (or any) plugin **marketplace** channel — dropped; revisit only on demand.
- Non-Claude agent *plugins/extensions* (`.codex-plugin/`, OpenCode JS, Pi TS) — unnecessary for plain skills, which all four read natively.
- Any change to what the skills *do* — this is packaging + neutralization only.
- The memory track (`memory-triage`) — unchanged, stays in `.claude/skills/`.
