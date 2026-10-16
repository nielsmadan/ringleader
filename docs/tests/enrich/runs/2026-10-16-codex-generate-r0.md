# Enrich benchmark — 2026-10-16-codex-generate-r0

Date: 2026-10-16. Commit: `78fa8c555d25`. Prompt: `r0` (hash `51bf21ae6f65`), tail 6000 chars; judge `claude:sonnet` (tail 24000 chars), 0 verdicts.

**Generate only. The judged numbers are pending the Claude runs.** This leg ran inside the
nono sandbox, where `codex exec` works but a nested `claude -p` cannot authenticate, so no
sonnet judge has seen these outputs yet. The pass rates, confusion matrix, disagreements and spot
check come from [step 3 of the procedure](../README.md#running-it), which judges these cached
outputs without calling codex again. This record therefore holds counts, latency and errors
only, and no model outputs.

Commands, from the repo root:
`uv run python scripts/benchmark_enrich.py --smoke --candidates codex: --judge codex:`. That
made one candidate call (9.4 s) and one codex judge call (10.1 s), both of which parsed. A codex
judge was used only to prove the judge path end to end. Then
`uv run python scripts/benchmark_enrich.py --phase generate --split all --candidates codex: --prompt-label r0 --concurrency 4`
made 141 calls with 0 errors: median 9.1 s, p90 13.8 s overall, about 6 minutes wall time.
This record was rendered with `--phase report --split all --candidates codex: --prompt-label r0`.

Candidate `codex:` is codex-cli 0.162.1 with no `--model`. The user's codex config resolved it to
`gpt-6-astra` (reasoning effort medium) at the time of the run. Codex reports no tokens or cost
through `codex exec`. Live registry `stat -f '%m %z'` was `1792117028 4773721` before and after;
only the copy in the work dir was read.

Sample: seed 20261015, drawn 2026-10-16T07:12:06+00:00; 101 labelled sessions in the registry copy, 1840 pending classification. Dropped for an unreadable transcript: 0 labelled, 0 backlog.

| split | n | by source | by stored status |
|---|---|---|---|
| dev | 37 | claude-code 19, codex 18 | active 1, blocked 1, finished 2, unfinished 33 |
| heldout | 64 | claude-code 30, codex 32, opencode 2 | active 3, blocked 5, finished 4, unfinished 52 |
| backlog | 40 | claude-code 24, codex 5, droid 3, opencode 5, pi 3 | unclassified 40 |

## Results

Bar (held-out labelled split): status ≥ 90% and mean of the three fields ≥ 90%.

| candidate | split | n | outputs | errors | judged | status | whatItIs | name | mean | meets bar |
|---|---|---|---|---|---|---|---|---|---|---|
| codex: | dev | 37 | 37 | 0 | 0 | — | — | — | — | — |
| codex: | heldout | 64 | 64 | 0 | 0 | — | — | — | — | — |
| codex: | backlog | 40 | 40 | 0 | 0 | — | — | — | — | — |

## Latency and cost

| candidate | split | median s | p90 s | input tokens | output tokens | cost $ | outputs generated |
|---|---|---|---|---|---|---|---|
| codex: | dev | 8.9 | 10.8 | — | — | — | 2026-10-16 |
| codex: | heldout | 9.4 | 15.1 | — | — | — | 2026-10-16 |
| codex: | backlog | 8.9 | 11.9 | — | — | — | 2026-10-16 |

Projected backlog (pending * median latency / concurrency):

| candidate | pending | concurrency | hours | cost $ |
|---|---|---|---|---|
| codex: | 1840 | 4 | 1.1 | — |

## Finished vs not (judge-corrected truth)

Pending: no judge verdicts yet.

## Errors

None.

## Label disagreements and failed fields

Pending: no judge verdicts yet.

## Spot check

The spot-check table is derived from private transcripts and is not committed. It is in `reports/r0-dev-heldout-backlog.md` in the `--work` dir (default `$TMPDIR/rl-enrich-bench`).
