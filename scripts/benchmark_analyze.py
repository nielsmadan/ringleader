from __future__ import annotations

import argparse
import dataclasses
import json
import os
import random
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ringleader import paths, stateio
from ringleader.analysis import labels, store
from ringleader.analysis.episodes import Episode
from ringleader.analysis.taxonomy import printable, printable_lines
from ringleader.config import load_config
from ringleader.models import CALL_TIMEOUT_SECONDS, with_cancel
from scripts.benchmark_enrich import (
    Candidate,
    ModelFactory,
    allocate,
    call_with_stats,
    dev_count,
    error_text,
    latency,
    model_builder,
    parse_candidates,
    read_json,
    run_pool,
    sha,
    stats_dict,
)

SPLITS = ("dev", "heldout")
PHASES = ("sample", "annotate", "generate", "report")
DEFAULT_SEED = 20261018
SAMPLE_SIZE = 40
BAR_CATEGORY = 0.8
BAR_FRICTION = 0.7
NONE_LABEL = "(none)"
FIELDS = (
    ("category", labels.EPISODE_CATEGORIES),
    ("friction", labels.FRICTION_TAGS),
)
ALLOWED = dict(FIELDS)
RESULT_HEADER = (
    "candidate",
    "split",
    "n",
    "outputs",
    "category",
    "friction",
    "median s/call",
    "cost usd",
    "meets bar",
)

type Gold = dict[str, dict[str, str]]
type PromptFn = Callable[[Sequence[Episode]], str]
type Ask = Callable[[str], str]
type Show = Callable[[str], None]
type Progress = Callable[[str, int, int], None]


def default_work() -> Path:
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / "rl-analyze-bench"


def work_index(work: Path) -> Path:
    return work / "index.sqlite"


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f"{path} is not a directory you own; pass --work to choose another")
    path.chmod(0o700)
    return path


def write_json(path: Path, data: Any) -> None:
    private_dir(path.parent)
    stateio.atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


def copy_index(source: Path, dest: Path) -> None:
    private_dir(dest.parent)
    dest.unlink(missing_ok=True)
    os.close(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    with (
        closing(store.connect(source, read_only=True)) as src,
        closing(sqlite3.connect(dest)) as dst,
    ):
        src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")


def sample_rows(index: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in index.execute(
            "SELECT e.episode_id, e.agent, e.kinds, t.parent_thread_id FROM episodes e"
            " JOIN transcripts t USING (path) ORDER BY e.episode_id"
        )
    ]


def stratum(row: Mapping[str, Any]) -> tuple[str, str, str]:
    thread = "subagent" if row["parent_thread_id"] else "main"
    return (str(row["kinds"]).split(",")[0], str(row["agent"]), thread)


def draw_sample(
    rows: Sequence[Mapping[str, Any]], *, seed: int, size: int, now_iso: str, source: str
) -> dict[str, Any]:
    rng = random.Random(seed)
    groups: dict[tuple[str, ...], list[str]] = {}
    for row in rows:
        groups.setdefault(stratum(row), []).append(row["episode_id"])
    ordered = {key: sorted(ids) for key, ids in sorted(groups.items())}
    quota = allocate({key: len(ids) for key, ids in ordered.items()}, size)
    dev: list[str] = []
    heldout: list[str] = []
    for key, ids in ordered.items():
        picked = rng.sample(ids, quota[key])
        cut = dev_count(len(picked))
        dev.extend(picked[:cut])
        heldout.extend(picked[cut:])
    strata = {episode_id: list(key) for key, ids in ordered.items() for episode_id in ids}
    return {
        "seed": seed,
        "drawnAt": now_iso,
        "index": source,
        "available": len(rows),
        "splits": {"dev": sorted(dev), "heldout": sorted(heldout)},
        "strata": {episode_id: strata[episode_id] for episode_id in sorted([*dev, *heldout])},
    }


def ensure_sample(
    work: Path, index_path: Path, *, seed: int, size: int, now_iso: str
) -> dict[str, Any]:
    source = str(index_path.resolve())
    existing = read_json(work / "sample.json")
    if isinstance(existing, dict):
        recorded = (existing.get("seed"), existing.get("index"))
        if recorded != (seed, source):
            raise ValueError(
                f"{work / 'sample.json'} was drawn with seed {recorded[0]} from {recorded[1]}; "
                f"pass the same --seed and --index, or delete sample.json and index.sqlite "
                f"in {work} to draw again"
            )
        if not work_index(work).exists():
            copy_index(index_path, work_index(work))
        return existing
    if not work_index(work).exists():
        copy_index(index_path, work_index(work))
    with closing(store.connect(work_index(work), read_only=True)) as index:
        sample = draw_sample(
            sample_rows(index), seed=seed, size=size, now_iso=now_iso, source=source
        )
    write_json(work / "sample.json", sample)
    return sample


def sample_ids(sample: Mapping[str, Any]) -> list[str]:
    return [episode_id for split in SPLITS for episode_id in sample["splits"][split]]


def load_episodes(work: Path, ids: Sequence[str]) -> dict[str, Episode]:
    marks = ", ".join("?" * len(ids))
    with closing(store.connect(work_index(work), read_only=True)) as index:
        rows = index.execute(f"SELECT * FROM episodes WHERE episode_id IN ({marks})", tuple(ids))
        return {row["episode_id"]: store.episode_from_row(row) for row in rows}


def valid_gold(data: Any, wanted: Sequence[str]) -> Gold:
    entries = data if isinstance(data, dict) else {}
    return {
        episode_id: {name: entry[name] for name, _ in FIELDS}
        for episode_id, entry in entries.items()
        if episode_id in wanted
        and isinstance(entry, dict)
        and all(entry.get(name) in values for name, values in FIELDS)
    }


def read_gold(work: Path, sample: Mapping[str, Any]) -> Gold:
    return valid_gold(read_json(work / "gold.json"), sample_ids(sample))


def import_gold(work: Path, source: Path, sample: Mapping[str, Any]) -> dict[str, int]:
    raw = read_json(source)
    kept = valid_gold(raw, sample_ids(sample))
    write_json(work / "gold.json", {**read_gold(work, sample), **kept})
    return {"read": len(raw) if isinstance(raw, dict) else 0, "kept": len(kept)}


def choose(ask: Ask, show: Show, title: str, values: Sequence[str]) -> str | None:
    show(f"{title}: " + "  ".join(f"{i}={v}" for i, v in enumerate(values, 1)) + "  s=skip")
    while True:
        answer = ask("> ").strip()
        if answer == "s":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(values):
            return values[int(answer) - 1]
        show("enter a number from the list, or s")


def annotate(work: Path, sample: Mapping[str, Any], *, ask: Ask, show: Show) -> dict[str, int]:
    gold = read_gold(work, sample)
    todo = [i for i in sample_ids(sample) if i not in gold]
    episodes = load_episodes(work, todo) if todo else {}
    done = 0
    try:
        for position, episode_id in enumerate(todo, 1):
            episode = episodes[episode_id]
            show(
                f"\n[{position}/{len(todo)}] {printable(episode.agent)}"
                f"  signals: {', '.join(episode.kinds)}"
                f"  calls: {episode.calls}\n{printable_lines(episode.excerpt)}\n"
            )
            category = choose(ask, show, "category", labels.EPISODE_CATEGORIES)
            friction = choose(ask, show, "friction", labels.FRICTION_TAGS)
            if category is None or friction is None:
                continue
            gold = {**gold, episode_id: {"category": category, "friction": friction}}
            write_json(work / "gold.json", gold)
            done += 1
    except EOFError:
        show("input closed; annotations so far are saved")
    return {"annotated": done, "gold": len(gold), "missing": len(sample_ids(sample)) - len(gold)}


def batches(sample: Mapping[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    size = labels.EPISODE_BATCH
    return [
        (split, tuple(ids[i : i + size]))
        for split in SPLITS
        for ids in [sample["splits"][split]]
        for i in range(0, len(ids), size)
    ]


def generate_key(candidate: Candidate, prompt_version: int, prompt: str, ids: Sequence[str]) -> str:
    return sha(json.dumps([candidate.engine, candidate.model, prompt_version, sha(prompt), ids]))


def generate_path(work: Path, key: str) -> Path:
    return work / "generate" / f"{key}.json"


@dataclasses.dataclass(frozen=True)
class PromptSpec:
    build: PromptFn = labels.episode_prompt
    version: int = labels.LABEL_PROMPT_VERSION


DEFAULT_SPEC = PromptSpec()


@dataclasses.dataclass(frozen=True)
class Runner:
    make_model: ModelFactory
    concurrency: int
    progress: Progress | None = None


@dataclasses.dataclass(frozen=True)
class Job:
    candidate: Candidate
    split: str
    ids: tuple[str, ...]
    prompt: str
    key: str


def plan_jobs(
    sample: Mapping[str, Any],
    candidates: Sequence[Candidate],
    episodes: Mapping[str, Episode],
    spec: PromptSpec,
) -> list[Job]:
    return [
        Job(
            candidate,
            split,
            ids,
            prompt,
            generate_key(candidate, spec.version, prompt, ids),
        )
        for candidate in candidates
        for split, ids in batches(sample)
        for prompt in [spec.build([episodes[i] for i in ids])]
    ]


def generate(
    work: Path,
    sample: Mapping[str, Any],
    candidates: Sequence[Candidate],
    runner: Runner,
    spec: PromptSpec = DEFAULT_SPEC,
) -> dict[str, dict[str, Any]]:
    episodes = load_episodes(work, sample_ids(sample))
    jobs = plan_jobs(sample, candidates, episodes, spec)
    todo = [job for job in jobs if not generate_path(work, job.key).exists()]
    models = {c.spec: runner.make_model(c, CALL_TIMEOUT_SECONDS["analyze"]) for c in candidates}
    errors: dict[str, list[str]] = {c.spec: [] for c in candidates}
    done = [0]

    def call(job: Job, stopped: Callable[[], bool]) -> dict[str, Any]:
        raw, stats = call_with_stats(
            with_cancel(models[job.candidate.spec], stopped),
            job.prompt,
            labels.EPISODE_LABEL_SCHEMA,
        )
        found = labels.parse_batch(raw, job.ids, ALLOWED)
        if len(found) < len(job.ids):
            raise ValueError(f"unparseable output: {len(found)} of {len(job.ids)} labels")
        return {
            "candidate": job.candidate.spec,
            "split": job.split,
            "ids": list(job.ids),
            "promptVersion": spec.version,
            "promptSha": sha(job.prompt),
            "labels": found,
            "stats": stats_dict(stats),
        }

    def landed(job: Job, outcome: dict[str, Any] | Exception) -> None:
        done[0] += 1
        if isinstance(outcome, Exception):
            errors[job.candidate.spec].append(error_text(outcome))
        else:
            write_json(generate_path(work, job.key), outcome)
        if runner.progress:
            runner.progress("generate", done[0], len(todo))

    run_pool(todo, call, concurrency=runner.concurrency, on_result=landed)
    planned = Counter(job.candidate.spec for job in jobs)
    called = Counter(job.candidate.spec for job in todo)
    return {
        c.spec: {
            "jobs": planned[c.spec],
            "cached": planned[c.spec] - called[c.spec],
            "calls": called[c.spec],
            "errors": len(errors[c.spec]),
            "errorMessages": dict(Counter(errors[c.spec])),
        }
        for c in candidates
    }


def collect(
    work: Path,
    sample: Mapping[str, Any],
    candidates: Sequence[Candidate],
    spec: PromptSpec = DEFAULT_SPEC,
) -> dict[str, dict[str, Any]]:
    episodes = load_episodes(work, sample_ids(sample))
    jobs = plan_jobs(sample, candidates, episodes, spec)
    records = [(job, read_json(generate_path(work, job.key))) for job in jobs]
    return {
        c.spec: {
            "labels": {
                episode_id: label
                for job, record in records
                if record is not None and job.candidate == c
                for episode_id, label in record["labels"].items()
            },
            "calls": {
                split: [
                    record["stats"]
                    for job, record in records
                    if record is not None and job.candidate == c and job.split == split
                ]
                for split in SPLITS
            },
        }
        for c in candidates
    }


def accuracy(gold: Gold, predicted: Mapping[str, Mapping[str, str]], field: str) -> float | None:
    hits = sum(1 for i, g in gold.items() if predicted.get(i, {}).get(field) == g[field])
    return hits / len(gold) if gold else None


def confusion(
    gold: Gold, predicted: Mapping[str, Mapping[str, str]], field: str
) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter[str]] = {}
    for episode_id, truth in sorted(gold.items()):
        counts.setdefault(truth[field], Counter())[
            predicted.get(episode_id, {}).get(field, NONE_LABEL)
        ] += 1
    return {truth: dict(sorted(row.items())) for truth, row in sorted(counts.items())}


def verdict(category: float | None, friction: float | None, *, complete: bool) -> bool | None:
    if not complete or category is None or friction is None:
        return None
    return category >= BAR_CATEGORY and friction >= BAR_FRICTION


def summed_cost(calls: Sequence[Mapping[str, Any]]) -> float | None:
    costs = [c["costUsd"] for c in calls if c["costUsd"] is not None]
    return sum(costs) if costs else None


def build_report(
    sample: Mapping[str, Any],
    gold: Gold,
    results: Mapping[str, Mapping[str, Any]],
    candidates: Sequence[Candidate],
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for c in candidates:
        predicted = results[c.spec]["labels"]
        for split in SPLITS:
            ids = sample["splits"][split]
            golded = {i: gold[i] for i in ids if i in gold}
            calls = results[c.spec]["calls"][split]
            category = accuracy(golded, predicted, "category")
            friction = accuracy(golded, predicted, "friction")
            rows.append(
                {
                    "candidate": c.spec,
                    "split": split,
                    "n": len(golded),
                    "sampled": len(ids),
                    "outputs": sum(1 for i in golded if i in predicted),
                    "category": category,
                    "friction": friction,
                    "confusion": {name: confusion(golded, predicted, name) for name, _ in FIELDS},
                    "medianSeconds": latency([x["seconds"] for x in calls])["median"],
                    "costUsd": summed_cost(calls),
                    "meetsBar": (
                        verdict(category, friction, complete=len(golded) == len(ids) > 0)
                        if split == "heldout"
                        else None
                    ),
                }
            )
    return {
        "meta": dict(meta),
        "bars": {"category": BAR_CATEGORY, "friction": BAR_FRICTION},
        "rows": rows,
    }


def percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def verdict_text(value: bool | None) -> str:
    return "—" if value is None else ("pass" if value else "fail")


def result_cells(row: Mapping[str, Any]) -> list[str]:
    median, cost = row["medianSeconds"], row["costUsd"]
    return [
        row["candidate"],
        row["split"],
        f"{row['n']}/{row['sampled']}",
        str(row["outputs"]),
        percent(row["category"]),
        percent(row["friction"]),
        "—" if median is None else f"{median:.1f}",
        "—" if cost is None else f"{cost:.4f}",
        verdict_text(row["meetsBar"]),
    ]


def table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
        *("| " + " | ".join(cells) + " |" for cells in rows),
    ]


def confusion_lines(row: Mapping[str, Any], field: str) -> list[str]:
    matrix = row["confusion"][field]
    predicted = sorted({p for counts in matrix.values() for p in counts})
    return table(
        [f"{field}: gold \\ predicted", *predicted],
        [[truth, *(str(counts.get(p, 0)) for p in predicted)] for truth, counts in matrix.items()],
    )


def render_markdown(report: Mapping[str, Any], title: str) -> str:
    meta, bars = report["meta"], report["bars"]
    lines = [
        f"# {title}",
        "",
        f"- date: {meta['date']}  commit: {meta['commit']}",
        f"- prompt version: {meta['promptVersion']}  prompt hash: {meta['promptHash']}",
        f"- index facts version: {meta['factsVersion']}  seed: {meta['seed']}",
        (
            f"- bar on heldout: category >= {percent(bars['category'])}, "
            f"friction >= {percent(bars['friction'])}"
        ),
        "- this record holds counts only: no episode text, cause, path or session id",
        "",
        *table(RESULT_HEADER, [result_cells(row) for row in report["rows"]]),
    ]
    for row in report["rows"]:
        if row["n"] == 0:
            continue
        lines += ["", f"## {row['candidate']} on {row['split']}"]
        for name, _ in FIELDS:
            lines += ["", *confusion_lines(row, name)]
    return "\n".join(lines) + "\n"


def git_commit() -> str:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parent.parent,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return done.stdout.strip() or "unknown"


def prompt_hash(prompt_fn: PromptFn) -> str:
    return sha(prompt_fn([]))[:12]


def index_file(path: Path) -> Path:
    return paths.analysis_index_path(store.FACTS_VERSION, path) if path.is_dir() else path


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="benchmark_analyze.py",
        description="Benchmark analysis episode labelling against hand-annotated gold labels.",
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=paths.analysis_dir(),
        help="the analysis index dir, as rl analyze --index takes, or the index file",
    )
    parser.add_argument("--work", type=Path, default=default_work())
    parser.add_argument("--phase", choices=(*PHASES, "all"), default="all")
    parser.add_argument("--candidates", type=parse_candidates, default="claude:haiku")
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path)
    return parser.parse_args(argv)


def print_progress(phase: str, done: int, total: int) -> None:
    if done == total or done % 10 == 0:
        print(f"{phase}: {done}/{total}", file=sys.stderr, flush=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    make_model: ModelFactory | None = None,
    now: datetime | None = None,
    ask: Ask = input,
    show: Show = print,
) -> int:
    args = parse_args(argv)
    work: Path = args.work
    index = index_file(args.index)
    moment = now or datetime.now(UTC)
    if not work_index(work).exists() and not index.is_file():
        print(f"no analysis index at {index}; run rl analyze first", file=sys.stderr)
        return 2
    try:
        private_dir(work)
        sample = ensure_sample(
            work,
            index,
            seed=args.seed,
            size=SAMPLE_SIZE,
            now_iso=moment.isoformat(timespec="seconds"),
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    phases = PHASES if args.phase == "all" else (args.phase,)
    summary: dict[str, Any] = {"sample": {s: len(sample["splits"][s]) for s in SPLITS}}
    try:
        if args.gold is not None:
            summary["goldImport"] = import_gold(work, args.gold, sample)
        elif "annotate" in phases:
            summary["annotate"] = annotate(work, sample, ask=ask, show=show)
        if "generate" in phases:
            summary["generate"] = generate(
                work,
                sample,
                args.candidates,
                Runner(
                    make_model or model_builder(load_config().ai_command_prefix),
                    args.concurrency,
                    print_progress,
                ),
            )
        if "report" in phases:
            report = build_report(
                sample,
                read_gold(work, sample),
                collect(work, sample, args.candidates),
                args.candidates,
                {
                    "date": moment.date().isoformat(),
                    "commit": git_commit(),
                    "promptVersion": labels.LABEL_PROMPT_VERSION,
                    "promptHash": prompt_hash(DEFAULT_SPEC.build),
                    "factsVersion": store.FACTS_VERSION,
                    "seed": sample["seed"],
                },
            )
            summary["report"] = report
            if args.out is not None:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(render_markdown(report, args.out.stem), encoding="utf-8")
    except KeyboardInterrupt:
        print("interrupted; finished calls are cached", file=sys.stderr)
        return 130
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
