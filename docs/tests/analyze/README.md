# Analyze labelling benchmark

**Status: the run is pending on the user.** The harness is built and tested against a fake
model, but nothing here has been measured. A nested `claude -p` cannot authenticate in the
sandboxed session that built it, so no real model has been called and no record exists yet.

Run this when changing the episode label prompt (`labels.episode_prompt`), its taxonomy
(`EPISODE_CATEGORIES`, `FRICTION_TAGS`), `EPISODE_BATCH` or `LABEL_PROMPT_VERSION`, or the default
analyze model (`models.ANALYZE_DEFAULT_MODELS`). It answers one question: does a cheap model label
flagged episodes (category and friction) well enough to be the default for `rl analyze --label`?

[The script](../../../scripts/benchmark_analyze.py) is read-only on live data. It copies the
analysis index (`--index`, default the live one; it takes the index dir as `rl analyze --index`
does, or the index file) into the work dir with the SQLite backup API
through a read-only connection, and reads only the copy. The index file is never written; SQLite
may create its `-wal` and `-shm` sidecars beside it, as any reader of a WAL database does. It
writes to the work dir (`--work`, default `$TMPDIR/rl-analyze-bench`) and to `--out`. Model calls
go through the same trimmed adapters `rl` uses, so they leave no session behind.

## Sample and splits

`--phase sample` draws the sample once with a fixed seed (`--seed`, default `20261018`) and
writes it to `work/sample.json`. Every later run reuses that file and recopies
`work/index.sqlite` if it is missing. A run that passes a different `--seed` or `--index` than the
one recorded in it fails with a message; delete `sample.json` and `work/index.sqlite` to draw again.

- 40 episodes, stratified by (first signal kind, agent, thread kind main / subagent). Every
  stratum gets one, then the rest go proportionally to stratum size. Only the first signal kind
  is used: stratifying by the full combination of kinds would scatter 40 episodes over too many
  strata.
- Split about 40/60 into **`dev`** and **`heldout`** within each stratum. A stratum of one goes to
  `heldout`. The two never overlap.
- **`dev`** is for prompt revisions. **`heldout`** is scored once, with the final prompt.

## Phases

`--phase sample | annotate | generate | report | all` (default `all`), for `--candidates` (a comma
list of `engine:model`; `engine:` means the engine's own default model; default `claude:haiku`).

- **annotate**: prompts for the gold `category` and `friction` of each sampled episode, showing
  its signals, call count and excerpt, and never a model output. Answers are saved one at a time
  to `work/gold.json`, so quitting (`Ctrl-D`) and rerunning resumes. `s` skips an episode.
  `--gold PATH` imports a gold file instead (`{episode_id: {"category", "friction"}}`), keeping
  only valid entries for sampled ids.
- **generate**: batches of `EPISODE_BATCH` episodes per split, one call per batch with
  `episode_prompt` and `EPISODE_LABEL_SCHEMA`, parsed with `parse_batch`. A batch that does not
  return a valid label for every id counts as unparseable. Calls run on `--concurrency` daemon
  threads (default 4).
- **report**: per candidate and split, n (episodes with gold), outputs, category and friction
  accuracy, a confusion matrix for each field, median latency per call, summed cost where the
  adapter reports it, and the verdict on `heldout`. An episode with no output counts as wrong.
  The verdict stays `—` until every held-out episode has gold. The JSON goes to stdout and the
  markdown to `--out`.

## Bar

On the held-out split: category accuracy ≥ 80 % **and** friction accuracy ≥ 70 %
(`BAR_CATEGORY`, `BAR_FRICTION`). The default `ANALYZE_DEFAULT_MODELS["claude"]` stays `"haiku"`
only if haiku meets it; the run's result, not this file, decides any change.

## Caching

Generate results are cached in `work/generate/` by (engine, model, `LABEL_PROMPT_VERSION`, sha256
of the prompt, batch ids), with the call's latency, tokens and cost. Only complete, parseable
batches are cached; a failed or partial call is retried on the next run. A prompt or version
change calls the model again; earlier outputs stay cached, and a new candidate calls only itself.

The work dir holds episode excerpts, gold labels and model causes about private transcripts, and
lives under `$TMPDIR`. **The record written to `--out` holds only aggregates**: no excerpt, cause,
path or session id. Never copy work-dir content into `docs/`.

## Running it

From the repository root after `uv sync`. **`claude -p` cannot authenticate inside the nono
sandbox**; run these in your own terminal. The default `--index` is the live index, whose file is only
copied, never written.

1. Draw the sample and annotate it (40 episodes, by hand, no model call):

   ```sh
   uv run python scripts/benchmark_analyze.py --phase sample
   uv run python scripts/benchmark_analyze.py --phase annotate
   ```

2. Generate and report on dev and heldout with the current prompt (10 haiku calls):

   ```sh
   uv run python scripts/benchmark_analyze.py --phase generate --candidates claude:haiku
   uv run python scripts/benchmark_analyze.py --phase report --candidates claude:haiku --out docs/tests/analyze/runs/$(date +%F)-haiku-v1.md
   ```

   `--phase all` does the same in one go. Never revise the prompt on held-out results: revise on
   dev, bump `LABEL_PROMPT_VERSION`, rerun.

3. Optionally compare more candidates, for example `--candidates claude:haiku,claude:sonnet`.
   Only the new candidate calls its model.

**Cost.** About 10 calls per candidate, each with four episode excerpts of at most 4,000
characters. The report sums `total_cost_usd` where the adapter provides it; on a subscription that
number is notional.

## Runs

None yet. The first run is pending on the user.
