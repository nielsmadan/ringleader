# Converting and continuing sessions

Conversion creates a fresh native session for Claude Code, Codex, OpenCode, Pi, or
Droid and preserves the source. It carries supported visible history, historical
instructions, tool exchanges, attachment bytes, and resumed model context. Native
format profiles are strict: preview the session before relying on a conversion.
`rl resume --to <agent>` uses this same mechanism, falling back to a Markdown
handoff only on refusal or `--markdown` — see
[resuming sessions](../features/resuming-sessions.md).

## Preview and convert

```sh
rl session refresh
rl session search 'the work you want'
rl session convert pi:<source-id> --to claude --dry-run
rl session convert pi:<source-id> --to claude
```

Use the native session ID shown by search; names are not selectors. An unambiguous
ID prefix also works, and `AGENT:ID` resolves collisions. These commands read the
registry without scanning. A missing source needs an explicit refresh. `--dry-run`
reads and validates the source and target profile, checks preservation, and writes
no registry, journal, or native files. Version probes use disposable directories.

The target uses the source's effective recorded workspace, including a previous
workspace retarget. Running `rl` from another directory does not relocate history.
`--workspace PATH` filters batch sources at or below that path; it is not an output
directory option. Use `rl workspace move` or `retarget` for relocation.

Repeat `convert` to reuse the result of the same snapshot, target store/profile,
workspace, title, resolved target configuration, and transform. Source edits or
changed conversion options create another result. Changing a temporary/cache
location alone does not. If the old result was archived or deleted, the repeat
reports that state; it does not recreate it. Use `fork` to request a new identity.

## Fork, hand off, or resume

```sh
rl session fork <id>
rl session fork <id> --to codex --compact
rl session fork <id> --to pi --resume
rl session handoff <id> --to claude
rl session resume <target-id>
rl session lineage <id> --json
```

Fork defaults to the source agent and always makes a fresh session. Handoff forks
into the required target agent and immediately resumes it. Resume replaces `rl`
with the native executable in the recorded workspace. It sends no automatic
prompt; normal native continuation loads current destination project instructions.
Historical instructions remain labeled history with their original provenance.
Native launch keeps your normal environment and credentials. The offline smoke
procedures use a separate, fully isolated environment.

The executable and cwd are checked before fork-and-resume publishes. If launch
fails afterward, the result remains available and the error includes a qualified
ID and a retry command. Archived/deleted targets cannot be resumed directly.
Lineage persists through archive/delete and shows conversion and fork relations.

`--compact` performs no model call. It starts from the latest applicable checkpoint
and retained tail, preserves authored instructions and remaining prose in order,
and replaces complete contiguous tool exchanges with markers naming tools and
counts. It reports approximate text token sizes and omission counts. It is not a
general permission to discard unsupported content, images, or instruction scopes.

## Batches and selection

```sh
rl session convert <id1> <id2> --to pi --json
rl session convert --open --from claude --workspace /absolute/project --to pi --dry-run
rl session convert --running --to codex --compact
```

`--open` selects effective active, unfinished, or blocked sessions, excluding
currently snoozed and name-filtered rows. Kept sessions remain eligible when open.
`--running` uses verified agent-qualified hooklinesinker presence, regardless of
lifecycle or visibility. It requires complete presence and a registry record with
that exact identity; follow setup/refresh diagnostics when either is missing.
Explicit IDs may select hidden or archived sources; archived bytes are read in
place. A batch keeps successful results and exits nonzero when any item fails.
Repeating it reuses those successes and retries failures.

## Recovery

```sh
rl data transaction list
rl data transaction resume <transaction-id>
rl data transaction rollback <transaction-id>
```

Each conversion has a durable journal under the ringleader state directory.
After a crash, resume completes publication using its stored source snapshot and
owned staging, even if the original source has gone. Once publication may have
made a session discoverable, recovery only moves forward. Rollback removes
provably private staging; it never removes a published session or truncates a
shared native name index. Ambiguous ownership, missing artifacts after an intent,
or changed schemas produce diagnostics for inspection rather than overwriting or
recreating another process's session. Unverified private remnants are named.

## Compatibility

`rl doctor` reports compatibility for its current cwd/native roots and unfinished
journals. A passing check in one workspace does not validate every batch workspace,
especially for OpenCode model configuration.

A target version converts when it is either pinned from upstream source research or
self-verified on this machine. Self-verification runs an offline harness —
`rl doctor --verify-conversion [AGENT]` — that captures a real outbound model
request (or, for OpenCode, checks CLI acceptance and schema identity) against
whatever version is actually installed, and records the pass in
`verified-targets.json`. A version that is neither known nor yet verified refuses
conversion with that command rather than converting against an unproven writer.
`rl resume --to` runs this itself, interactively only, the first time it needs an
unverified target.

| Agent | Prerequisite | Native evidence tier |
|---|---|---|
| Claude Code | pinned or self-verified version | model-request-capture |
| Codex | pinned or self-verified version, initialized state_5 schema/backfill | model-request-capture |
| Pi | pinned or self-verified version, v3 session format | model-request-capture |
| Droid | self-verified version (none pinned) | model-request-capture |
| OpenCode | pinned or self-verified version, exact structural schema identity, initialized DB, supported explicit provider/model | cli-acceptance only |

The shared-content matrix is not a promise that every ordinary session converts.
For example, Codex can retain distinct display/model streams and ambiguous
`base_instructions` as historical context; Claude/Pi cannot write those visibility
splits, so a writer that cannot keep the native shape writes the checkpoint's model
view as an ordinary stream instead. OpenCode also restricts tool output shape,
compaction, and configuration. Claude rejects images in failed tool results because
its actual resume drops them; successful tool-result images and textual failures
are supported. Pi's installed runtime ignores the documented embedded
`retainedTail`, so its writer refuses it. Unknown conversational shapes,
incomplete/changing sources, and unsupported attachment types fail before
publication.

See [native profiles and runnable offline checks](../reference/session-portability-native.md)
for exact content/configuration limits, evidence scope, and the current pairwise
conversion rates, and the [complete flags](cli.md#session-portability). Native
interactive TUI rendering and Claude normal-mode project instruction loading are
not established by these checks.
