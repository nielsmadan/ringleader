# Agent worktree management

Surveyed 2026-11-01. CLI help and versions were checked locally: Claude Code 2.1.316,
codex-cli 0.166.0, Droid 1.1.0, OpenCode 2.2.5, Pi 1.4.0. The lifecycle details below
are **upstream-documented**, not end-to-end verification of those installed versions.

Contents: [ownership](#entry-points-and-ownership) · [Claude](#claude-code) ·
[Codex](#codex) · [Droid](#droid-and-factory-app) · [OpenCode](#opencode-v2) · [Pi](#pi)

## Entry points and ownership

| Agent | Locally checked interface | Worktree owner |
|---|---|---|
| Claude Code | `--worktree [name]`, `--resume ID --fork-session` | Claude |
| Codex | `fork ID --worktree` | Codex; CLI does not accept a worktree name |
| Droid | `resume ID --fork --worktree [name]` | Droid |
| OpenCode v2 | `api OPERATION --data JSON`, no top-level worktree flag | OpenCode server/selected strategy |
| Pi | `--fork PATH_OR_ID`, no core worktree creation flag | External Git workflow or extension |

Forking conversation history and creating a checkout are separate capabilities. Launching an
agent in an externally created checkout does not establish native worktree ownership. A common
ringleader interface should preserve the native owner where possible, and disclose the smaller
feature set of an external fallback. Desktop behavior must not be inferred from a CLI flag.

## Claude Code

[Worktree documentation](https://code.claude.com/docs/en/worktrees) describes:

- Creation under `.claude/worktrees/`, named branches, generated names, and reopening by name.
  `worktree.baseRef` selects remote-default-branch state (`fresh`) or local `HEAD` (`head`).
  Reopening a clean, completed worktree can reset it under the `fresh` policy.
- `.worktreeinclude` copies matching ignored files. `WorktreeCreate` and `WorktreeRemove`
  hooks replace the default lifecycle, including for non-Git version control.
- `EnterWorktree`/`ExitWorktree` move session context; resume restores its worktree after
  identity checks. Forks start in the launch directory. Git-created worktrees move the
  transcript with the session; hook-created worktrees retain the launch-directory transcript.
- Worktree-specific checks block direct edits into the protected checkout and certain shell
  and Git redirects. These checks do not track every shell write: they are not an OS sandbox.
- `isolation: worktree` gives subagents their own checkout. Active worktrees are locked;
  unchanged temporary worktrees can be removed. Interactive cleanup prompts protect work,
  and background/subagent retention sweeps have additional dirty/unpushed/submodule checks.
- Plugins, approvals, and some untracked project resources are shared across linked worktrees.
  Desktop project configuration lookup differs from CLI lookup.

[Large-codebase settings](https://code.claude.com/docs/en/large-codebases) additionally document
`worktree.sparsePaths` and `symlinkDirectories`, allowing small checkouts and shared dependency
directories. Starting in a repository subdirectory does not preserve that cwd: creation enters
the worktree root.

**Integration consequence:** native creation preserves hooks, isolation state, and cleanup
ownership. A plain `git worktree add` followed by a conversation fork is not equivalent.

**Verified 2026-11-01, Claude Code 2.1.316:** `--resume ID --fork-session --worktree`
created a managed checkout and a new transcript whose cwd identified it. Fixture history
reached a loopback provider; source bytes were unchanged. A controlled provider error ended
the offline test; this verifies startup and history transfer, not a successful model turn.

## Codex

[CLI commands](https://developers.openai.com/codex/developer-commands) document managed worktrees
for new sessions and explicit-ID forks. `resume --worktree` is unsupported. `/worktree` can
start or continue a chat in a new worktree and browse existing worktrees and their owner chats.

[Managed worktrees](https://developers.openai.com/codex/environments/git-worktrees) use detached
HEAD by default. The desktop workflow can select a starting branch and include local changes,
copy ignored files through `.worktreeinclude`, and copy an ignored `AGENTS.override.md`.
Handoff moves a chat and Git state between Local and Worktree while keeping the chat's
worktree association. Desktop cleanup protects pinned/running chats, snapshots work before
deletion, and offers restoration; its retention limit is configurable.

[Local environments](https://developers.openai.com/codex/environments/local-environment) are
explicitly desktop-only: setup scripts, platform overrides, and actions for builds, tests,
or development servers. The app also provides diff/stage/commit/PR controls.

**Integration consequence:** delegate the CLI fork to preserve managed-worktree identity;
do not promise desktop setup scripts or impose ringleader's branch/path naming on Codex.

**Verified 2026-11-01, Codex 0.166.0:** interactive `fork ID --worktree --no-daemon`
restored fixture history, created a detached managed checkout under isolated `CODEX_HOME`,
and recorded the new cwd and `forked_from_id`. Source bytes stayed unchanged; no prompt was
submitted. The interactive Droid combination remains help-checked, not startup-verified.

## Droid and Factory App

[CLI worktrees](https://docs.factory.com/droid-cli/cli-reference#git-worktrees) create or reuse
branches and checkouts. Interactive worktrees persist. `droid exec` removes clean checkouts
on exit and preserves dirty ones; branch deletion is separate and Droid keeps the branch.

[Factory App worktrees](https://docs.factory.com/factory-app/worktrees) add setup profiles
(setup script, initial Droid prompt, cleanup script), ignored-file copying with
`.worktreeinclude`, persistent versus ephemeral worktrees, pinning, and inventory/size controls.
Cleanup considers active sessions, dirty/untracked files, unpublished commits, PR status, and
Git locks. Archiving the last session offers removal. A restored session whose checkout was
deleted returns to the main checkout. Profiles can be shared in `.factory/worktree-setups/`.

**Integration consequence:** use Droid's native fork/worktree flags, but do not claim Factory
App profiles and retention apply to CLI-created worktrees without a separate probe.

## OpenCode v2

[Tools](https://opencode.ai/v2/docs/tools#worktrees) expose creation, listing, removal, and
`session_move` at a safe boundary. [The API](https://opencode.ai/v2/docs/api#worktree.create)
creates through the selected strategy and runs the project setup script.
[Configuration](https://opencode.ai/v2/docs/config#worktrees) selects storage and plugin
strategies; Git is the built-in default. Configuration comes from the canonical checkout.
Inventory refresh discovers and reconciles worktrees across known checkout roots.

**Integration consequence:** use the native server API rather than substituting Git creation,
which would bypass setup and strategy selection. Worktree management does not itself establish
a filesystem sandbox. Automatic merge-back was not established by this survey.

**Verified 2026-11-01, OpenCode 2.2.5:** an isolated loopback server created a native worktree,
forked an imported fixture, and moved the fork while preserving the source directory.
`worktree.create` requires `projectID`; `from` alone fails. `session.move` returns 204 before
the directory update completes, so read `session.get` until `data.location.directory` matches.
Separate `api --standalone` invocations can terminate before queued moves finish; use the
normal persistent service for this sequence. The v2 database uses `session_v2`, not the
v1 `session` table; successful API support does not establish v2 scanner compatibility.

## Pi

The [core CLI reference](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md)
and installed help expose no worktree creation/lifecycle flag. `--fork` copies conversation
history into the current directory; resume restores the saved directory. Extensions may add
worktree behavior, but that is not a core guarantee.

**Integration consequence:** an external checkout followed by `pi --fork <absolute-transcript>`
can supply isolation and conversation continuity. Setup, copying ignored files, cleanup, and
merge-back remain external responsibilities. Using an absolute transcript avoids cwd-scoped
session-ID lookup after changing directories.

**Verified 2026-11-01, Pi 1.4.0:** an offline RPC startup with a fixture and isolated Pi
directories created a fresh session whose cwd was the external worktree, preserved all four
history messages including a tool exchange, and left the original transcript bytes unchanged.
No model request was made.
