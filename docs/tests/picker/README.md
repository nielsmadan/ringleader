# Picker result replacement

Run this when changing the resume picker's result widget or ranking path. From the repository
root after `uv sync`:

```sh
uv run python scripts/benchmark_picker.py
```

[The script](../../../scripts/benchmark_picker.py) creates a temporary registry with 1,834
Claude sessions. The query `session` matches 1,322 names. A headless 80×24 picker uses an
injected refresher and empty presence result; it never scans or changes live session data.
After mount and background convergence, one result replacement warms up the path, then three
sequential replacements are timed with `perf_counter`. Report each sample and their median
in seconds. The script prints Python, OS and Textual versions too.

Compare on the same machine, interpreter, Textual version, workload and repetition count.
This measures `_render`, including ranking and replacing result content, not terminal painting,
startup, or background scan time. Aim for a median below 100 ms on the recorded workload;
use the picker tests to check selection, scrolling, truncation and shutdown behavior.

Runs:

- [2026-10-12 baseline](runs/2026-10-12-before.md)
- [2026-10-12 OptionList](runs/2026-10-12-after.md)
