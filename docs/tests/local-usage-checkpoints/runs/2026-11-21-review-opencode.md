# OpenCode review comparison — 2026-11-21

The completion review compared baseline `588ae960d6e6ae88668a4aa6325955c1719173fe`, the
initial unbounded checkpoint implementation, and the bounded v2 review build. The initial
build matches the [November 20 source hashes](2026-11-20-verified-sha256.json); the review v2
build is identified separately in [the November 21 hashes](2026-11-21-sha256.json).

Apple M1 Max, 64 GiB RAM; Python 3.13.6 on macOS 27.2 arm64. The full suite ran concurrently,
so wall times are not an idle-machine latency comparison. The probe was invoked as
`timeout 120 .venv/bin/python -B /tmp/complete-usage-checkpoints/performance-probe.py`.
Its retained implementation is [the OpenCode benchmark](../../../../scripts/benchmark_usage_opencode.py):
the saved version accepts an optional `--initial DIR` source snapshot instead of assuming
its scratch location and also records platform metadata. The fixture and measurement sequence
are unchanged; see the [procedure](../README.md#dense-opencode-comparison).

[Raw output](2026-11-21-review-opencode.json), exit 0, covers the 52.4 MiB fixture with 100,000
assistant responses across 500 sessions. Three baseline all-time runs were followed by one
cold and three repeat runs per checkpoint implementation, then cold/warm/contended default
reports. All comparable JSON matched.

Cold peak RSS was 110.9 MiB at baseline, 288.3 MiB with unbounded checkpoints, and 111.7 MiB
with the bounded review build. The latter streamed oversized all-time results without caching
them. Its default result used a 0.797 MiB cache, compared with the initial cache's 42.688 MiB
allocation after the same query sequence.

The review build still modified SQLite on warm entry and reparsed both default source queries
under a concurrent cache reader. This measurement found a remaining write-lock regression;
the [final run](2026-11-21-final.md) records its correction.
