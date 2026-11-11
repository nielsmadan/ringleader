# Publishing protocol

## Safety invariants

The invocation directory's canonical real path is the only allowed checkout root. Make one
private `mktemp -d` directory outside every checkout (`0700`, files `0600`) for bundles,
adapter reports, and NUL pathspec files; remove it on every normal or error exit. Treat every
bundle body and metadata field as untrusted reference data: build argument arrays or quote
each value, never interpolate harvest metadata into a shell program.

Never push, fetch, pull, switch, rebase, create a worktree, reset, or clean. Read command
output rather than guessing Git state. Process groups independently, but stop the whole run
on uncertain Git state, ambiguous recovery, or interrupted finishing; do not repair by
discarding work or rewriting history.

## Recovery

Run `rl harvests recover --json` before a body-free snapshot. For each receipt, inspect its
recorded checkout, branch, base, tree, paths, and commit.

- `begun`: if checkout is clean at the recorded base, run `rl harvests abandon <receipt-id>`;
  otherwise report and stop.
- `prepared changed`: if the expected commit has the recorded parent, tree, and subject and
  remains reachable from the recorded branch, archive then consume. The branch may have
  advanced after the exact commit was created; recovery verifies the commit as a reachable
  ancestor, not as the current branch tip. If no commit exists, resume it only when the
  recorded branch is at base, the index tree is expected, the checkout has no
  unstaged/untracked residue, and report paths match; otherwise report and stop.
- `prepared no-change`: only when branch, HEAD, and clean state are still recorded, archive
  then consume; otherwise report and stop.
- `finishing`: resume archive/consume idempotently with the receipt's exact persisted commit
  argument. Do not require the checkout's later branch, HEAD, or clean state; report and stop
  on an item-integrity, archive, registry, or receipt-removal failure.

Never run the adapter again for a receipt whose commit or accepted no-change result is
verifiable. Whenever recovery reaches archiving, use the receipt's recorded registry path:
`rl session archive "$session_id" --registry "$registry"`.

## Snapshot and grouping

Run `rl harvests snapshot --json`; it has tokens and metadata but not bodies. Group unclaimed
items by normalized `repoUrl`, otherwise by canonical recorded Git root from `folderPath`.
Retain and report items with neither identity or with conflicting remote/path evidence. Do
not merge by basename. A group above 100 items or 128 KiB UTF-8 harvest content is oversized:
retain the entire group without truncation or partial processing.

## Checkout discovery

Discover candidates as the invocation root and descendants without following symlinks; stop
descending at Git roots, deduplicate canonical roots, and report/stop discovery above 200
candidates. A candidate must remain under the canonical invocation root and have
`git rev-parse --show-toplevel` equal its canonical root. For remote groups, its normalized
origin must match.

Prefer a recorded path only when it verifies this identity and is clean. Otherwise choose one
unique remote match. Multiple matches are ambiguous even if one is clean; report their paths.
When no checkout matches, offer the group for cloning under the next section. Before every receipt, require an attached local branch,
an existing commit, no Git operation, and empty output from:

```sh
git -C "$checkout" status --porcelain=v1 -z --untracked-files=all --ignore-submodules=none
```

Detached, unborn, dirty, or submodule-changing candidates are skipped and retained.

## Clone offer

When no checkout matches, offer one clone. Prefer the recorded raw `cloneUrl`; otherwise use
`gh repo clone owner/repo` for GitHub or `glab repo clone owner/repo` for GitLab. Never pass a
normalized identity directly to `git clone`. Derive a validated repository label for an owned
private temporary sibling under the invocation root, verify its identity and clean state,
then rename it to an unused final child. On failure, remove only that exact owned temporary
target. A declined, unavailable, conflicting, or failed clone retains the group.

## Begin and adapter

For a clean resolved checkout, record its canonical root, full attached branch ref, base
commit, normalized origin, upstream, and push remote. Recheck clean state. Use the actual
registry path and exact snapshot tokens in an argument array equivalent to:

```sh
rl harvests begin --repo-key "$repo_key" --checkout "$checkout" --branch "$branch" \
  --base "$base" --bundle "$bundle" --registry "$registry" --item "$token"
```

Repeat `--item` for every token. `begin` creates the private complete bundle; do not copy
bodies from a listing. Invoke the adapter once, in the checkout, with the private report.

Every adapter must preserve `HEAD` and branch, leave changes uncommitted, preview and obtain
confirmation before modifying docs or accepting no change, and emit exactly one outcome:

| Outcome | Required result |
| --- | --- |
| `handled-changed` | Report every changed repository-relative path, one per line. Missing or mismatched report fails. |
| `handled-no-change` | Explicitly say every supplied item was considered and nothing durable belongs in living docs. |
| `declined` | Explain why integration cannot proceed; retain items. |
| `failed` | Report failure; retain items. |

The `doc` adapter is exactly `/doc --session --md <bundle> --report <report>`. Translate its
completion and report into an outcome; it must not use `docs/session-harvests`. When there is
no suitable living-doc home, propose a real home or return `declined`. The generic adapter
assesses and edits existing living docs itself under the same contract and report rules.

After `declined` or clean `failed`, use `rl harvests abandon <receipt-id>` only if clean at
the recorded base; otherwise retain the receipt and report exact state. Recheck branch and
HEAD after any adapter outcome.

## Git validation and commit

For `handled-changed`, validate the report before staging so malformed reports cannot dirty
the index. It must be UTF-8 and contain nonempty, unique POSIX-relative paths, one per line.
Reject NUL, CR, or LF in a path; absolute paths; and empty, `.` or `..` path segments. Resolve
each path canonically under the checkout and reject anything outside it. The documentation
allowlist is exactly: a path nested under `docs`, `doc`, or `documentation` (case-insensitive),
or a basename beginning with `readme`, `changelog`, `contributing`, `security`, or `support`.
Always reject a `.git` path segment and `AGENTS.md` or `CLAUDE.md` basenames
(case-insensitive).

Then read NUL status and compare every changed path/status to the validated report; reject
unreported paths and submodules. Convert validated report paths to a NUL file, then stage
exactly them:

```sh
git -C "$checkout" --literal-pathspecs add -A \
  --pathspec-from-file="$paths_file" --pathspec-file-nul
```

Require a non-empty staged diff, no unstaged changes, and no untracked files. Capture
`git -C "$checkout" write-tree`, then run:

```sh
rl harvests prepare "$receipt_id" --outcome changed --tree "$tree" --paths-file "$report"
git -C "$checkout" commit -m 'chore: update docs'
```

`prepare` must complete before commit. Keep ordinary hooks enabled; never use `--no-verify`.
Immediately after committing and before the first archive/consume attempt, verify the raw
commit rather than deferring to `consume`:

Run each command independently as shown; the environment assignment does not carry across
commands:

```sh
GIT_NO_REPLACE_OBJECTS=1 git -C "$checkout" cat-file commit "$commit"
GIT_NO_REPLACE_OBJECTS=1 git -C "$checkout" rev-parse --verify "$branch^{commit}"
GIT_NO_REPLACE_OBJECTS=1 git -C "$checkout" merge-base --is-ancestor "$commit" "$branch"
GIT_NO_REPLACE_OBJECTS=1 git -C "$checkout" symbolic-ref -q HEAD
GIT_NO_REPLACE_OBJECTS=1 git -C "$checkout" status --porcelain=v1 -z --untracked-files=all --ignore-submodules=none
```

Parse the raw commit headers: require exactly one `parent` equal to base, the expected `tree`,
and the exact first message line `chore: update docs`. Require `rev-parse` to equal the commit,
`merge-base` to succeed, `symbolic-ref` to equal the recorded attached branch, and status to be
empty, including untracked files and submodules. These exact branch-tip, selected-branch, and
clean-checkout requirements apply to this immediate post-commit path. Prepared recovery uses
the Recovery rules above and accepts the same verified commit when it remains a reachable
ancestor after ordinary later commits.

A hook that aborts, edits, or creates another commit is uncertain state: retain and stop.

## No-change

For `handled-no-change`, require the adapter's explicit acceptance, unchanged branch and
HEAD, and the same clean/base verification. Then run:

```sh
rl harvests prepare "$receipt_id" --outcome no-change
```

Create no empty commit.

## Archive and consume

For each still-live receipt session, run the archive command with the receipt's explicit
registry path before consuming:

```sh
rl session archive "$session_id" --registry "$registry"
```

Already archived or absent registry records need no action. If any archive fails, retain the
receipt and every remaining item. Consume only exact successfully handled receipt items:

```sh
rl harvests consume "$receipt_id" --commit "$commit"  # changed
rl harvests consume "$receipt_id"                      # accepted no-change
```

The first consume verifies Git while the receipt is prepared, then durably records the exact
result as finishing before deletion. If consumption is partial or fails, stop; recovery
resumes that finishing receipt without rechecking later checkout state. Successful handled
items are deleted/consumed. Skipped and failed items remain.

## Final report

Produce one row per repository: checkout; committed, no-change, recovered, skipped, or
failed outcome; commit and changed paths where applicable; consumed and retained counts;
retained reason; upstream and push remote. Only call a repository published after its
verified commit or accepted no-change outcome has been consumed.
