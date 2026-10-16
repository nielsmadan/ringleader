# Enrich model benchmark

Run this when changing the enrich prompt (`enrichment.enrich_prompt`), the transcript tail
(`enrichment.TAIL_CHARS`) or the default enrich models (`models.ENRICH_DEFAULT_MODELS`). It answers
one question: does a cheap model classify dormant sessions (status, `whatItIs`, name) well enough to
be the default for `rl roundup` and the inbox?

[The script](../../../scripts/benchmark_enrich.py) is read-only on live data. It copies the
registry (`--registry`, default the live one) into the work dir and reads only the copy. It reads
transcripts in place and writes only to the work dir (`--work`, default `$TMPDIR/rl-enrich-bench`)
and to `--out`. Model calls go through the same trimmed adapters `rl` uses, so they leave no
session behind.

## Sample and splits

`--phase sample` draws the sample once with a fixed seed (`--seed`, default `20261015`) and writes
it to `work/sample.json`. Every later run reuses that file, even when `--seed` or the registry
changes; delete it to draw again.

- **Labelled** sessions have a `status` other than `unclassified` and a non-empty `whatItIs`. They
  are stratified by (stored status, source, age bucket ≤ 7 d / ≤ 30 d / older) and split about
  40/60 into `dev` and `heldout`. Every stratum with two or more members contributes to both; a
  stratum of one goes to `heldout`.
- **`dev`** is for prompt revisions. **`heldout`** is scored once, with the final prompt.
- **`backlog`** is 40 unlabelled sessions still pending classification, stratified by source and
  message-count bucket (< 12, 12–49, ≥ 50). Every stratum gets one, then the rest go
  proportionally to stratum size. These are judged without a reference.
- A session whose transcript tail cannot be read is dropped and counted (`dropped` in the
  record).

The stored labels came from earlier triage passes, so they can be stale or wrong. The judge sees
them only as a reference.

## Phases

`--phase sample | generate | judge | report | all` (default `all`), over `--split dev |
heldout | backlog | all` (or a comma list), for `--candidates` (a comma list of `engine:model`;
`engine:` means the engine's own default model; default `claude:haiku`).

- **generate**: one call per candidate and session with `enrich_prompt` and the last
  `--tail-chars` of the transcript (default `TAIL_CHARS`), parsed with `parse_classification`.
  Calls run on a pool of `--concurrency` daemon threads (default 4).
- **judge**: `--judge` (default `claude:sonnet`) grades every generated output against the rubric
  below. The judge sees project, branch, title, AI title, message count, the last
  `--judge-tail-chars` of the transcript (default 24,000), the candidate output and, on labelled
  splits only, the stored label marked as possibly stale or wrong. It returns `status_ok`,
  `what_ok`, `name_ok`, `label_disagrees` and a one-sentence `rationale`.
- **report**: per candidate and split, it gives n, outputs, errors, per-field pass rates, the
  headline (status rate and the mean of the three fields), and whether the bar is met. It adds
  a finished-vs-not confusion matrix, latency median and p90, tokens and cost where the
  adapter reports them, and the projected time and cost for the pending backlog (pending count ×
  median latency ÷ concurrency), and the span of dates the outputs were generated on. `meets bar`
  stays `—` until every row of the split is judged. It lists every label disagreement and failed
  field by session identity. A 15-row spot-check table, chosen with a seed, has an empty
  `human agrees?` column for a person to fill in. The JSON goes to stdout. The markdown goes to
  `--out`, and carries no session content. A full local report, with the spot-check table, the
  candidate outputs, stored labels, judge verdicts and rationales (cut to 200 characters), goes to
  `work/reports/<prompt-label>-<splits>.md`.
- **`--smoke`** makes one call per candidate and one judge call on the first sampled session,
  prints the result and stops. It first draws and freezes the sample and the 6,000-character
  tails if the work dir has none, and freezes the judge tail of the session it uses. Every later
  run reuses them. It writes no generate or judge results to the cache.

The confusion matrix uses judge-corrected truth: the stored label when the judge agrees with
it. Otherwise, and on the backlog, it uses the judge's implied call: the candidate's
finished/open call if `status_ok`, else the opposite. That is an approximation. A failed
non-finished call (`active` judged wrong where `blocked` was right) counts as "truth: finished".

## Rubric and bar

- **status**: `active` means work in progress with momentum. `unfinished` means stopped
  mid-task with more to do. `blocked` means waiting on the user or something external.
  `finished` means the task is complete with nothing pending. An adjacent call passes when the
  tail supports it (`active`↔`unfinished`, `unfinished`↔`blocked`). `finished` on a session that
  ended mid-task fails. A live status on a visibly completed task fails.
- **whatItIs** must be accurate, one line, with no invented specifics.
- **name** must be short kebab-case, specific enough to recognise among the project's sessions.

**Bar**, on the held-out labelled split: status pass rate ≥ 90 % **and** mean of the three
per-field pass rates ≥ 90 %. The defaults follow from it:

- `ENRICH_DEFAULT_MODELS["claude"]` is `"haiku"` if haiku meets the bar, else `"sonnet"` if
  sonnet does. If neither does, it stays `"haiku"` and the shortfall is stated prominently in the
  record and in [tech/models.md](../../tech/models.md).
- `"codex"` stays `None` (the codex default model). If it fails, the failure is recorded; no other
  model name is guessed.
- `enrichment.ESTIMATED_SECONDS_PER_CALL` comes from the measured medians, with `"llm"` taking the
  haiku median.

**The judge is sonnet, so sonnet's own row is self-judged.** Read it as a ceiling, not as an
independent score.

## Caching

Generate results are cached in `work/generate/` by (engine, model, sha256 of the prompt, session
identity). Judge verdicts are cached in `work/judge/` by (judge, sha256 of the judge prompt). Only
successes are cached. A failed or unparseable generate or judge call is recorded in
`work/errors/` and retried on the next run. Two candidates with identical output for a session
share one judge call.

A prompt revision changes the prompt hash, so a rerun calls the model again only for that
prompt. Outputs for earlier prompts stay cached. Transcript tails are frozen in `work/tails/` on
first read. A later run therefore sees the same text, even for a session that has been active
since. The work dir holds transcript excerpts, candidate outputs and the registry copy. It lives
under `$TMPDIR`, which macOS cleans after a few days of disuse. Once it is gone, the next run draws a new sample
and repeats every call.

**Records never quote transcript text or `lastPrompt`.** The record written to `--out` holds only
aggregates and non-content metadata: sample stats, pass rates, confusion counts, latency, tokens,
cost, error counts by kind, and for each disagreement or failed field the short session id, the
source and the field. It holds no candidate output, stored label, rationale, title, project path
or branch. The spot check lives in the work dir because it is derived from private transcripts.
Open it locally, for example:

```sh
open "$TMPDIR/rl-enrich-bench/reports/r0-heldout-backlog.md"
```

The record names that path in one line. Never copy its content into `docs/`.

## Running it

From the repository root after `uv sync`.

**`claude -p` cannot authenticate inside the nono sandbox**, because the login keychain is denied.
Run the Claude legs in your own terminal or in an unsandboxed agent session. You can also
prefix them with `AGENT_REQUIRE_SECRETS=1 sops-exec`, but only if your secrets store carries a
Claude credential. `codex exec` works inside the sandbox, so the codex generate leg already ran
there ([2026-10-16 record](runs/2026-10-16-codex-generate-r0.md)). Its outputs are cached in the
work dir, and the held-out run below reuses them while that dir survives.

1. Smoke test: one haiku call and one sonnet judge call (about 30 s):

   ```sh
   uv run python scripts/benchmark_enrich.py --smoke --candidates claude:haiku
   ```

2. Dev split, current prompt (`r0`): 37 haiku calls and 37 sonnet judge calls:

   ```sh
   uv run python scripts/benchmark_enrich.py --phase all --split dev --candidates claude:haiku --prompt-label r0 --out docs/tests/enrich/runs/$(date +%F)-haiku-dev-r0.md
   ```

   Prompt revisions belong to an unsandboxed agent session, which reviews each one. A user-run
   pass does not revise: it stays on `r0` and records whatever dev shows. If an agent session
   finds dev below the bar, it revises `enrich_prompt` or `TAIL_CHARS` and reruns the same command
   with `--prompt-label r1` (then `r2`, `r3`). Levers: status definitions in the prompt,
   title/AI title in the prompt, tail 6,000 vs 12,000, `whatItIs` ≤ 15 words, names of 2–5
   kebab-case words. Stop after three revisions or as soon as dev meets the bar. Never revise
   on the held-out split.

3. Held-out and backlog, final prompt, all three candidates: 104 haiku and 104 sonnet calls
   (codex is already cached), then 312 sonnet judge calls:

   ```sh
   uv run python scripts/benchmark_enrich.py --phase all --split heldout,backlog --candidates claude:haiku,claude:sonnet,codex: --prompt-label r0 --out docs/tests/enrich/runs/$(date +%F)-heldout-r0.md
   ```

   Use the final revision's label in place of `r0`; a user-run pass keeps `r0`. If the prompt changed after the codex leg,
   codex generates again (104 calls).

4. Set the defaults from step 3's held-out rows per the bar above. Update
   `ESTIMATED_SECONDS_PER_CALL`, add the runs below, and mark the record "user-run, `r0` only"
   when no revision was made.

**Cost.** The Claude legs spend the account's Claude quota: about 140 haiku calls with a 6,000
character tail, and about 104 sonnet generate calls plus about 350 sonnet judge calls with a
24,000 character tail. `claude -p` reports `total_cost_usd` per call, which the record sums. On a
subscription that number is notional. For scale, the codex leg took 6 minutes for 141 calls
at concurrency 4. Judge calls carry four times the tail, so expect them to be slower.

## Runs

- [2026-10-16 codex generate, r0](runs/2026-10-16-codex-generate-r0.md): sandboxed, generate
  only. The judged numbers are pending the Claude runs.
