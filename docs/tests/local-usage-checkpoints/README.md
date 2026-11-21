# Local usage checkpoints

Measure repeated `rl usage local` calls after changes to parsing or checkpoint storage.
The benchmark compares the checkout with a named Git revision, checking exact JSON parity.

## Procedure

Use the checkout's Python environment (`uv sync`), Git and `timeout`. No credentials are
needed. Run from the repository root on an otherwise idle machine:

```sh
timeout -k 10 240 uv run python scripts/benchmark_usage_checkpoint.py --baseline 588ae960d6e6ae88668a4aa6325955c1719173fe
```

The script archives the baseline package into a temporary directory and generates 50 Claude
transcripts with 2,000 lines and 10 usage events each. The 2,048-character filler messages bring
the fixture to about 208 MB. Both versions use the same Python interpreter and fixture, with
isolated HOME, XDG state and Claude profile paths. Temporary code, transcripts and state are
removed on exit. [Benchmark script](../../../scripts/benchmark_usage_checkpoint.py)

One baseline warmup precedes three timed baseline runs. The first checkpoint run is measured
separately; three warm runs follow. Finally one event is appended to one file and an
incremental run must include its additional 670 tokens. Runs are sequential, concurrency one;
each CLI process has a 120-second deadline. Wall time includes Python startup and CLI rendering.
Compare medians, require identical JSON before the append and the expected token increase
after it, and expect warm checkpoint time below the baseline median. There is no absolute
latency threshold. Preserve raw JSON and code identity with each dated run.

This measures a synthetic Claude workload with warm filesystem caches. It does not predict
latency for a particular user's machine, network filesystem, or OpenCode database. File
metadata discovery and loading normalized rows still cost time as history grows.

## Dense OpenCode comparison

[The OpenCode probe](../../../scripts/benchmark_usage_opencode.py) creates a 52.4 MiB
SQLite fixture with 100,000 assistant and 100,000 user messages across 500 sessions and
50 projects. It runs the baseline and checkout through the local CLI handler in fresh
isolated processes, with no live stores or credentials:

```sh
timeout -k 10 120 uv run python scripts/benchmark_usage_opencode.py --baseline 588ae960d6e6ae88668a4aa6325955c1719173fe
```

It records three baseline all-time runs, one current cold run and three repeats, then
measures the seven-day report cold, warm and under a concurrent cache read transaction.
There is no separate warmup; the generated fixture is already in the filesystem cache.
Each child has a 30-second deadline, and temporary stores and copied code are removed on
exit. `--initial DIR` optionally adds an earlier source snapshot containing `ringleader/`.

Compare exact JSON, peak resident memory in MiB, wall time in seconds, source query counts
and cache allocation. The oversized all-time result exceeds the 1 MiB entry cap and should
remain uncached; use it to check memory overhead, not warm speedup. The seven-day result
should reuse its checkpoint, including under the reader lock, without changing the cache's
mtime. The script checks JSON parity; inspect the other metrics rather than assuming success
from exit status alone. This synthetic message distribution does not predict a real user's
OpenCode workload.

## Recorded runs

The November 20 runs used the script's former name, `scripts/bench_usage_checkpoint.py`.

- [Initial comparison](runs/2026-11-20-initial.md): first implementation measurement.
- [Review build](runs/2026-11-20-review.md): later implementation, with concurrent test load.
- [Verified build](runs/2026-11-20-verified.md): earlier implementation, without concurrent test load.
- [Dense OpenCode review](runs/2026-11-21-review-opencode.md): memory improvement and the warm-write regression found during completion review.
- [Final comparison](runs/2026-11-21-final.md): bounded v2, source-recovery and concurrency fixes; use this as the comparison baseline for later changes.
