# Native auto-memory in agent CLIs

Research checked 2026-11-17. This inventory covers native CLI auto-memory. Instructions,
rules, skills, compaction state, third-party plugins, IDE/cloud memories and scheduled
automation storage have separate lifecycles.

## Claude Code

**Documented.** Claude Code saves topic files under a project memory directory and uses
`MEMORY.md` to index them. Git worktrees share project memory. The
`autoMemoryDirectory` setting can change the location. Repository-supplied settings
require native workspace trust, and `permissions.blockReadsOutsideWorkingDirectories`
can block their choice of directory. Claude supports persistent subagent notes in user
`~/.claude/agent-memory/`, project `.claude/agent-memory/` and local
`.claude/agent-memory-local/` directories. Its default native trust document records
`projects[path].hasTrustDialogAccepted`. The trust-file location for a custom
`CLAUDE_CONFIG_DIR` was not verified. [Auto-memory](https://code.claude.com/docs/en/memory)
· [Subagent memory](https://code.claude.com/docs/en/sub-agents#enable-persistent-memory)
· [Workspace trust](https://code.claude.com/docs/en/permissions#what-runs-before-you-trust-a-folder).

**Isolated adapter fixtures, 2026-11-18.** Topic removal and restoration preserve
unrelated index links and prose. Unsupported inline forms and ambiguous index edits
are refused. No cooperative Claude native write lock was verified. Presence checks and
exact-file staging reduce exposure to concurrent native writes without excluding them.

## Codex

**Pinned source, 2026-11-17.** Codex CLI **0.167.1**, tag `rust-v0.167.1`,
commit `0a94398fa60c4fac0e7659e4213a0a36e613dd46`. Native v1 uses
`memories_1.sqlite` and v2 uses `memories_v2_1.sqlite` under the effective SQLite
home. Artifact workspaces `memories/` and `memories_v2/` live under `CODEX_HOME`.
Effective Codex settings can select a SQLite home independently of session storage.
The native unit is a `stage1_outputs` row keyed by thread ID. Raw memory and rollout
summary are its two text fields. The source timestamp uses seconds. Mixed consolidated
outputs and `memories_extensions/` are outside this entry inventory. The public
read/control interface provided no verified per-entry removal.
[Pinned config](https://github.com/openai/codex/blob/0a94398fa60c4fac0e7659e4213a0a36e613dd46/codex-rs/core/src/config/mod.rs)
· [Read interface](https://github.com/openai/codex/blob/0a94398fa60c4fac0e7659e4213a0a36e613dd46/codex-rs/memories/read/src/lib.rs)
· [Control interface](https://github.com/openai/codex/blob/0a94398fa60c4fac0e7659e4213a0a36e613dd46/codex-rs/memories/write/src/control.rs).

Both versions use the same `memory_migrations` loader. The full native schema includes
the `consolidation_progress` singleton in addition to stage-1 outputs and jobs.
Ringleader checks successful SQLx entries for both migrations and a SHA-256 digest
of normalized SQLite schema definitions. The expected schema digest is
`d856ae02990ec8f90f222c821fcf876c2c278509ce4aacb45bba66a74305ac27`.
[Pinned migration loader](https://github.com/openai/codex/blob/0a94398fa60c4fac0e7659e4213a0a36e613dd46/codex-rs/state/src/migrations.rs).

| Migration source | SQLx SHA-384 |
| --- | --- |
| [`0001_memories.sql`](https://github.com/openai/codex/blob/0a94398fa60c4fac0e7659e4213a0a36e613dd46/codex-rs/state/memory_migrations/0001_memories.sql) | `a1af50da50775a70f98da680006b7df4501956753ae98511ab727aa295915d9ae6f4190b4eaa5abc4fd7891642f7d21d` |
| [`0002_consolidation_progress.sql`](https://github.com/openai/codex/blob/0a94398fa60c4fac0e7659e4213a0a36e613dd46/codex-rs/state/memory_migrations/0002_consolidation_progress.sql) | `18f0a8dd7fe9a847b30d719029066d7a78e0bc64310dc66e4f7708bad6f1a0a594c0dbd14fec1ccb410cd7ae75dd7b10` |

The canonical rollout artifact is `rollout_summaries/TIMESTAMP-BASE62[-slug].md`.
`TIMESTAMP` is UTC `YYYY-MM-DDTHH-MM-SS` from the UUID timestamp or source update
time. `BASE62` is four digits using `0–9a–zA–Z` from the low 32 UUID bits modulo
`62^4`. The optional slug is capped at 60 characters, lowercased ASCII alphanumerics
with other characters mapped to underscores and trailing underscores removed. A summary
has the native thread ID, update time, rollout path and cwd headers, optional Git branch,
a blank line and its summary body. The v1 raw file has a fixed heading and
``## Thread `UUID``` sections with update time, cwd, rollout path, summary filename and
raw text. Ringleader verifies matching artifacts and refuses ambiguous layouts. v2
removal does not rebuild `raw_memories.md`.
[Pinned artifact writer](https://github.com/openai/codex/blob/0a94398fa60c4fac0e7659e4213a0a36e613dd46/codex-rs/memories/write/src/storage.rs).

Codex holds an exclusive Rust `File::try_lock` on the workspace sibling
(`memories.lock` or `memories_v2.lock`) and keeps the lock inode. Stage-1 completion
requires ownership of the running job; a successful no-output watermark suppresses
that revision while leaving newer inputs eligible. Consolidation uses the existing Git
baseline and job progress. The adapter preserves both and the native singleton; it
never applies a native migration. A later native consolidation can update derived
summaries and may leave material drawn from other sources.
[Pinned workspace lock](https://github.com/openai/codex/blob/0a94398fa60c4fac0e7659e4213a0a36e613dd46/codex-rs/memories/write/src/workspace_lock.rs)
· [Pinned consolidation](https://github.com/openai/codex/blob/0a94398fa60c4fac0e7659e4213a0a36e613dd46/codex-rs/memories/write/src/phase2.rs).

**Isolated adapter verification, 2026-11-18.** The sanitized v1/v2 fixtures in
`tests/memory_helpers.py` supply the native stores used by the lock probe and by
`tests/test_memory_sources.py` and `tests/test_memory_sources_codex.py`. They cover the
pinned schema, native jobs, WAL reads, selected artifacts, refusal and recovery.
On macOS, the actual adapter's lock and a Rust `File::try_lock` helper exclude each
other in both directions, then reacquire the same inode. Linux interoperability remains
unverified. On a POSIX host with the project dev environment, GNU `timeout` and
`rustc` 1.89 or newer, reproduce the isolated check from the repo root:

```sh
timeout -k 5 90 uv run --no-sync python tests/probe_codex_memory_lock.py
```

The procedure compiles the helper in a disposable directory, exercises both adapter
workspace versions and reports the platform and compiler. It needs no Codex process
or live database and is separate from ordinary suite runs.

### Codex completion tools

On 2026-11-18, an isolated loopback provider captured the request made by
the `CodexCliModel` completion invocation on **codex-cli 0.170.0**. Its
`codex exec --ephemeral --ignore-rules --skip-git-repo-check --sandbox read-only`
invocation included an output schema. The request advertised tools including shell
execution and web search, with `tool_choice` set to `auto`. The provider returned HTTP 400
before inference or tool execution. Automatic memory drafting therefore requires
a different completion backend. Native memory mutation remains pinned to 0.167.1.

## Other supported CLI harnesses

| Harness | Evidence and scope |
| --- | --- |
| OpenCode | Core 2.3.0 source reviewed at `9b57cc05cb15a190480f4436a8ffff9e333d27f9`. No verified built-in auto-memory store in the CLI core or config. [Pinned source](https://github.com/anomalyco/opencode/tree/9b57cc05cb15a190480f4436a8ffff9e333d27f9). |
| Pi | Core 1.5.2 source reviewed at `0826f9f39f42bb252e4b6ea51015b63f00f63deb`. No verified built-in auto-memory store in the CLI core or config. [Pinned source](https://github.com/earendil-works/pi/tree/0826f9f39f42bb252e4b6ea51015b63f00f63deb). |
| Droid | Factory's 2026-11-17 memory guide described manual files, references and hooks. Its former URL now opens the AGENTS.md guide. Custom automations have separate state. [AGENTS.md guide](https://docs.factory.com/harness/agents-md) · [Custom automations](https://docs.factory.com/automations/custom). |
| Cursor CLI | No supported local entry interface verified for `2026.11.13-eacade1`. IDE and cloud memory behavior were outside this review. |

See [discovery and mutation](../tech/memory.md) for the adapter contracts and limits.
