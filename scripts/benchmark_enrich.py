from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import random
import shutil
import statistics
import sys
import tempfile
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ringleader import enrichment, overview, paths, registry
from ringleader.models import (
    CALL_TIMEOUT_SECONDS,
    ClaudeCliModel,
    CodexCliModel,
    LlmModel,
    with_cancel,
)
from ringleader.models import proc as model_proc
from ringleader.models.base import CallStats, Model, ModelError

ENGINES = ("claude", "codex", "llm")
SPLITS = ("dev", "heldout", "backlog")
LABELLED_SPLITS = ("dev", "heldout")
PHASES = ("sample", "generate", "judge", "report")
DEFAULT_SEED = 20261015
DEV_SHARE = 0.4
MIN_SPLIT_STRATUM = 2
BACKLOG_SIZE = 40
AGE_BUCKETS = ((7.0, "<=7d"), (30.0, "<=30d"))
MESSAGE_BUCKETS = ((12, "<12"), (50, "12-49"))
JUDGE_TAIL_CHARS = 24000
JUDGE_TIMEOUT = CALL_TIMEOUT_SECONDS["harvest"]
RATIONALE_CHARS = 200
ERROR_CHARS = 120
SPOT_CHECK_ROWS = 15
BAR = 0.9
P90 = 0.9
POLL_SECONDS = 0.5
FIELDS = ("status", "what", "name")
LABEL_WARNING = "reference written earlier, possibly stale or wrong — do not treat as the answer"
PROBE_THREAD = {
    "project": "<project>",
    "gitBranch": "<branch>",
    "lastPrompt": "<last prompt>",
    "messageCount": 0,
}
PROBE_TAIL = "<tail>"
RESULT_HEADER = (
    "candidate",
    "split",
    "n",
    "outputs",
    "errors",
    "judged",
    "status",
    "whatItIs",
    "name",
    "mean",
    "meets bar",
)
CONFUSION_HEADER = (
    "candidate",
    "split",
    "finished→finished",
    "finished→open",
    "open→finished",
    "open→open",
)

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status_ok": {"type": "boolean"},
        "what_ok": {"type": "boolean"},
        "name_ok": {"type": "boolean"},
        "label_disagrees": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["status_ok", "what_ok", "name_ok", "label_disagrees", "rationale"],
}

RUBRIC = """\
Rubric:
- status: `active` = work in progress with momentum; `unfinished` = stopped mid-task, more to do;
  `blocked` = waiting on the user or something external; `finished` = task completed, nothing
  pending. An adjacent call passes when the tail supports it (active<->unfinished,
  unfinished<->blocked). `finished` on a session that ended mid-task fails. A live status
  (active, unfinished, blocked) on a visibly completed task fails.
- whatItIs: accurate, one line, no invented specifics.
- name: short kebab-case, specific enough to recognise among the project's sessions."""

type Thread = dict[str, Any]
type TailReader = Callable[[Thread, int], str]
type PromptBuilder = Callable[[Thread, str], str]
type ModelFactory = Callable[[Candidate, float], Model]
type Progress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class Candidate:
    engine: str
    model: str | None

    @property
    def spec(self) -> str:
        return f"{self.engine}:{self.model or ''}"

    @property
    def slug(self) -> str:
        return f"{self.engine}-{self.model or 'default'}"


@dataclass(frozen=True)
class Job:
    candidate: Candidate
    qid: str
    split: str
    prompt: str
    key: str


def parse_candidate(spec: str) -> Candidate:
    engine, _, model = spec.strip().partition(":")
    if engine not in ENGINES:
        raise argparse.ArgumentTypeError(f"unknown engine {engine!r} in {spec!r}")
    return Candidate(engine, model or None)


def parse_candidates(text: str) -> tuple[Candidate, ...]:
    return tuple(parse_candidate(spec) for spec in text.split(",") if spec.strip())


def parse_splits(text: str) -> tuple[str, ...]:
    names = [name.strip() for name in text.split(",") if name.strip()]
    if "all" in names:
        return SPLITS
    unknown = [name for name in names if name not in SPLITS]
    if unknown or not names:
        raise argparse.ArgumentTypeError(f"unknown split(s) {unknown}; choose from {SPLITS}")
    return tuple(name for name in SPLITS if name in names)


def build_model(candidate: Candidate, timeout: float) -> Model:
    if candidate.engine == "claude":
        return ClaudeCliModel(model=candidate.model, timeout=timeout)
    if candidate.engine == "codex":
        return CodexCliModel(model=candidate.model, timeout=timeout)
    return LlmModel(model=candidate.model)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_hash(prompt: PromptBuilder) -> str:
    return sha(prompt(PROBE_THREAD, PROBE_TAIL))[:12]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def is_labelled(thread: Thread) -> bool:
    status = thread.get("status")
    what = thread.get("whatItIs")
    return (
        isinstance(status, str)
        and status not in ("", "unclassified")
        and isinstance(what, str)
        and bool(what.strip())
    )


def age_bucket(thread: Thread, now_iso: str) -> str:
    mtime = thread.get("mtime")
    days = overview.age_days(mtime, now_iso) if isinstance(mtime, str) and mtime else math.inf
    return next((label for limit, label in AGE_BUCKETS if days <= limit), "older")


def message_bucket(thread: Thread) -> str:
    count = thread.get("messageCount") or 0
    return next((label for limit, label in MESSAGE_BUCKETS if count < limit), ">=50")


def dev_count(size: int) -> int:
    return (
        0 if size < MIN_SPLIT_STRATUM else min(size - 1, max(1, math.floor(size * DEV_SHARE + 0.5)))
    )


def strata(
    threads: Iterable[Thread], key: Callable[[Thread], tuple[str, ...]]
) -> dict[tuple[str, ...], list[str]]:
    grouped: dict[tuple[str, ...], list[str]] = {}
    for thread in threads:
        grouped.setdefault(key(thread), []).append(registry.qualified_id(thread))
    return {k: sorted(members) for k, members in sorted(grouped.items())}


def shuffled(rng: random.Random, members: Sequence[str]) -> list[str]:
    order = list(members)
    rng.shuffle(order)
    return order


def split_labelled(
    threads: Sequence[Thread], now_iso: str, seed: int
) -> tuple[list[str], list[str]]:
    rng = random.Random(seed)
    groups = strata(
        threads,
        lambda t: (str(t.get("status")), registry.identity(t)[0], age_bucket(t, now_iso)),
    )
    orders = [shuffled(rng, members) for members in groups.values()]
    dev = [qid for order in orders for qid in order[: dev_count(len(order))]]
    heldout = [qid for order in orders for qid in order[dev_count(len(order)) :]]
    return sorted(dev), sorted(heldout)


def allocate(sizes: Mapping[tuple[str, ...], int], total: int) -> dict[tuple[str, ...], int]:
    keys = sorted((k for k, n in sizes.items() if n > 0), key=lambda k: (-sizes[k], k))
    quota = {k: (1 if index < total else 0) for index, k in enumerate(keys)}
    for _ in range(min(total, sum(sizes.values())) - sum(quota.values())):
        open_keys = [k for k in keys if quota[k] < sizes[k]]
        chosen = max(open_keys, key=lambda k: sizes[k] / (quota[k] + 1))
        quota[chosen] += 1
    return quota


def draw_backlog(
    pool: Sequence[Thread], seed: int, size: int, readable: Callable[[str], bool]
) -> tuple[list[str], list[str]]:
    rng = random.Random(seed + 1)
    groups = strata(pool, lambda t: (registry.identity(t)[0], message_bucket(t)))
    quota = allocate({k: len(v) for k, v in groups.items()}, size)
    chosen: list[str] = []
    dropped: list[str] = []
    for key, members in groups.items():
        taken = 0
        for qid in shuffled(rng, members):
            if taken >= quota[key]:
                break
            if readable(qid):
                chosen.append(qid)
                taken += 1
            else:
                dropped.append(qid)
    return sorted(chosen), sorted(dropped)


def tail_readable(read_tail: TailReader, chars: int) -> Callable[[Thread], bool]:
    def check(thread: Thread) -> bool:
        try:
            read_tail(thread, chars)
        except Exception:  # noqa: BLE001 -- any unreadable transcript is dropped and counted
            return False
        return True

    return check


def draw_sample(
    reg: Mapping[str, Any],
    *,
    now_iso: str,
    seed: int,
    readable: Callable[[Thread], bool],
    backlog_size: int = BACKLOG_SIZE,
    source: str = "",
) -> dict[str, Any]:
    threads = list(reg.get("threads", []))
    labelled_all = [t for t in threads if is_labelled(t)]
    labelled = [t for t in labelled_all if readable(t)]
    pending = registry.sessions_needing_classification(dict(reg))
    pool = [t for t in pending if not is_labelled(t)]
    by_qid = {registry.qualified_id(t): t for t in threads}
    dev, heldout = split_labelled(labelled, now_iso, seed)
    backlog, backlog_dropped = draw_backlog(
        pool, seed, backlog_size, lambda qid: readable(by_qid[qid])
    )
    chosen = [*dev, *heldout, *backlog]
    return {
        "seed": seed,
        "drawnAt": now_iso,
        "registry": source,
        "pendingCount": len(pending),
        "labelledCount": len(labelled_all),
        "dropped": {
            "labelled": len(labelled_all) - len(labelled),
            "backlog": len(backlog_dropped),
        },
        "splits": {"dev": dev, "heldout": heldout, "backlog": backlog},
        "sessions": {qid: by_qid[qid] for qid in chosen},
    }


def sample_stats(sample: Mapping[str, Any]) -> dict[str, Any]:
    sessions = sample["sessions"]

    def counts(split: str, field: Callable[[Thread], str]) -> dict[str, int]:
        return dict(sorted(Counter(field(sessions[q]) for q in sample["splits"][split]).items()))

    return {
        "seed": sample["seed"],
        "drawnAt": sample["drawnAt"],
        "pendingCount": sample["pendingCount"],
        "labelledCount": sample["labelledCount"],
        "dropped": sample["dropped"],
        "splits": {
            split: {
                "n": len(sample["splits"][split]),
                "bySource": counts(split, lambda t: registry.identity(t)[0]),
                "byStatus": counts(split, lambda t: str(t.get("status"))),
            }
            for split in SPLITS
        },
    }


def cached_tail(work: Path, read_tail: TailReader) -> TailReader:
    def tail(thread: Thread, chars: int) -> str:
        path = work / "tails" / f"{sha(registry.qualified_id(thread))[:24]}-{chars}.txt"
        if path.exists():
            return path.read_text(encoding="utf-8")
        text = read_tail(thread, chars)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return text

    return tail


def call_with_stats(
    model: Model, prompt: str, schema: dict[str, Any]
) -> tuple[dict[str, Any], CallStats]:
    with_stats = getattr(model, "complete_json_with_stats", None)
    if callable(with_stats):
        raw, stats = with_stats(prompt, schema)
        return raw, stats
    started = perf_counter()
    raw = model.complete_json(prompt, schema)
    return raw, CallStats(perf_counter() - started, None, None, None)


def stats_dict(stats: CallStats) -> dict[str, Any]:
    return {
        "seconds": stats.seconds,
        "inputTokens": stats.input_tokens,
        "outputTokens": stats.output_tokens,
        "costUsd": stats.cost_usd,
    }


def run_pool[T, R](
    items: Sequence[T],
    work_fn: Callable[[T, Callable[[], bool]], R],
    *,
    concurrency: int,
    on_result: Callable[[T, R | Exception], None],
) -> None:
    jobs: queue.SimpleQueue[T] = queue.SimpleQueue()
    for item in items:
        jobs.put(item)
    results: queue.SimpleQueue[tuple[T, R | Exception]] = queue.SimpleQueue()
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                item = jobs.get_nowait()
            except queue.Empty:
                return
            try:
                outcome: R | Exception = work_fn(item, stop.is_set)
            except Exception as error:  # noqa: BLE001 -- reported per item, never cached
                outcome = error
            results.put((item, outcome))

    workers = [
        threading.Thread(target=worker, name="bench", daemon=True)
        for _ in range(max(1, min(concurrency, len(items))))
    ]
    for thread in workers:
        thread.start()
    try:
        while any(t.is_alive() for t in workers) or not results.empty():
            try:
                item, outcome = results.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
            on_result(item, outcome)
    except BaseException:
        stop.set()
        model_proc.kill_all_children()
        raise


def generate_key(candidate: Candidate, prompt: str, qid: str) -> str:
    return sha(json.dumps([candidate.engine, candidate.model, sha(prompt), qid]))


def generate_path(work: Path, key: str) -> Path:
    return work / "generate" / f"{key}.json"


def error_text(error: BaseException) -> str:
    return " ".join(f"{type(error).__name__}: {error}".split())[:ERROR_CHARS]


def update_errors(
    work: Path, phase: str, candidate: Candidate, attempted: Mapping[str, str | None]
) -> None:
    path = work / "errors" / f"{phase}-{candidate.slug}.json"
    current = read_json(path) or {}
    kept = {qid: msg for qid, msg in current.items() if qid not in attempted}
    write_json(path, {**kept, **{qid: msg for qid, msg in attempted.items() if msg}})


def read_errors(work: Path, phase: str, candidate: Candidate) -> dict[str, str]:
    return read_json(work / "errors" / f"{phase}-{candidate.slug}.json") or {}


def split_ids(sample: Mapping[str, Any], splits: Iterable[str]) -> list[tuple[str, str]]:
    return [(split, qid) for split in splits for qid in sample["splits"][split]]


def build_generate_jobs(
    sample: Mapping[str, Any],
    candidates: Sequence[Candidate],
    splits: Sequence[str],
    *,
    tail: TailReader,
    tail_chars: int,
    prompt: PromptBuilder,
) -> tuple[list[Job], dict[tuple[str, str], str]]:
    prompts: dict[str, str] = {}
    failures: dict[tuple[str, str], str] = {}
    for _, qid in split_ids(sample, splits):
        if qid in prompts:
            continue
        thread = sample["sessions"][qid]
        try:
            prompts[qid] = prompt(thread, tail(thread, tail_chars))
        except Exception as error:  # noqa: BLE001 -- an unreadable transcript is one error
            failures.update({(c.spec, qid): error_text(error) for c in candidates})
    jobs = [
        Job(c, qid, split, prompts[qid], generate_key(c, prompts[qid], qid))
        for c in candidates
        for split, qid in split_ids(sample, splits)
        if qid in prompts
    ]
    return jobs, failures


def generate(  # noqa: PLR0913 -- injection points the suite drives
    work: Path,
    sample: Mapping[str, Any],
    candidates: Sequence[Candidate],
    splits: Sequence[str],
    *,
    make_model: ModelFactory,
    tail: TailReader,
    tail_chars: int,
    prompt: PromptBuilder = enrichment.enrich_prompt,
    prompt_label: str = "current",
    concurrency: int = 4,
    progress: Progress | None = None,
    clock: Callable[[], str] | None = None,
) -> dict[str, Any]:
    jobs, tail_failures = build_generate_jobs(
        sample, candidates, splits, tail=tail, tail_chars=tail_chars, prompt=prompt
    )
    todo = [job for job in jobs if not generate_path(work, job.key).exists()]
    models = {c.spec: make_model(c, CALL_TIMEOUT_SECONDS["enrich"]) for c in candidates}
    attempted: dict[str, dict[str, str | None]] = {c.spec: {} for c in candidates}
    for (spec, qid), message in tail_failures.items():
        attempted[spec][qid] = message
    latencies: dict[str, list[float]] = {c.spec: [] for c in candidates}
    done = [0]
    now = clock or (lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def call(job: Job, stopped: Callable[[], bool]) -> dict[str, Any]:
        model = with_cancel(models[job.candidate.spec], stopped)
        raw, stats = call_with_stats(model, job.prompt, enrichment.ENRICH_SCHEMA)
        found = enrichment.parse_classification(raw)
        return {
            "candidate": job.candidate.spec,
            "qid": job.qid,
            "promptSha": sha(job.prompt),
            "promptLabel": prompt_label,
            "tailChars": tail_chars,
            "output": {
                "status": found.status,
                "whatItIs": found.what_it_is,
                "suggestedName": found.suggested_name,
            },
            "stats": stats_dict(stats),
            "at": now(),
        }

    def landed(job: Job, outcome: dict[str, Any] | Exception) -> None:
        done[0] += 1
        if isinstance(outcome, Exception):
            attempted[job.candidate.spec][job.qid] = error_text(outcome)
        else:
            write_json(generate_path(work, job.key), outcome)
            attempted[job.candidate.spec][job.qid] = None
            latencies[job.candidate.spec].append(outcome["stats"]["seconds"])
        if progress:
            progress("generate", done[0], len(todo))

    run_pool(todo, call, concurrency=concurrency, on_result=landed)
    for c in candidates:
        update_errors(work, "generate", c, attempted[c.spec])
    called = Counter(job.candidate.spec for job in todo)
    planned = Counter(job.candidate.spec for job in jobs)
    unreadable = Counter(spec for spec, _ in tail_failures)
    return {
        c.spec: {
            "jobs": planned[c.spec] + unreadable[c.spec],
            "cached": planned[c.spec] - called[c.spec],
            "calls": called[c.spec],
            "errors": sum(1 for msg in attempted[c.spec].values() if msg),
            "errorMessages": dict(Counter(m for m in attempted[c.spec].values() if m)),
            "latency": latency(latencies[c.spec]),
        }
        for c in candidates
    }


def judge_prompt(
    thread: Thread, tail: str, output: Mapping[str, str], *, with_label: bool, tail_chars: int
) -> str:
    label = (
        "\nStored label ("
        f"{LABEL_WARNING}):\n"
        f"status: {thread.get('status')}\n"
        f"whatItIs: {thread.get('whatItIs')}\n"
        f"suggestedName: {thread.get('suggestedName')}\n"
        if with_label
        else ""
    )
    return (
        "You are judging one output of a classifier that labels DORMANT coding-agent sessions "
        "for a curation tool. Decide whether each field is reasonably correct for this "
        "session.\n\n"
        f"{RUBRIC}\n\n"
        "Session metadata:\n"
        f"project: {thread.get('project')}\n"
        f"git branch: {thread.get('gitBranch')}\n"
        f"title: {thread.get('title')}\n"
        f"ai title: {thread.get('aiTitle')}\n"
        f"message count: {thread.get('messageCount')}\n\n"
        f"Transcript tail (last {tail_chars} characters):\n{tail}\n\n"
        "Classifier output:\n"
        f"status: {output['status']}\n"
        f"whatItIs: {output['whatItIs']}\n"
        f"suggestedName: {output['suggestedName']}\n"
        f"{label}\n"
        "Return status_ok, what_ok and name_ok (true when that field is reasonably correct "
        "under the rubric); label_disagrees (true when the stored label's status contradicts "
        "the transcript; false when there is no stored label); rationale (one sentence in "
        "your own words; never quote the transcript)."
    )


def parse_verdict(raw: Mapping[str, Any]) -> dict[str, Any]:
    flags = ("status_ok", "what_ok", "name_ok", "label_disagrees")
    if not all(isinstance(raw.get(flag), bool) for flag in flags):
        raise ModelError("judge returned a verdict without all four booleans")
    rationale = raw.get("rationale")
    if not isinstance(rationale, str):
        raise ModelError("judge returned a verdict without a rationale")
    return {**{flag: raw[flag] for flag in flags}, "rationale": rationale.strip()}


def judge_key(judge: Candidate, prompt: str) -> str:
    return sha(json.dumps([judge.engine, judge.model, sha(prompt)]))


def judge_path(work: Path, key: str) -> Path:
    return work / "judge" / f"{key}.json"


@dataclass(frozen=True)
class Row:
    candidate: Candidate
    split: str
    qid: str
    output: dict[str, Any] | None
    stats: dict[str, Any] | None
    verdict: dict[str, Any] | None
    judge_prompt: str | None
    at: str | None = None


def collect_rows(  # noqa: PLR0913 -- injection points the suite drives
    work: Path,
    sample: Mapping[str, Any],
    candidates: Sequence[Candidate],
    splits: Sequence[str],
    *,
    judge: Candidate,
    tail: TailReader,
    tail_chars: int,
    judge_tail_chars: int,
    prompt: PromptBuilder = enrichment.enrich_prompt,
) -> list[Row]:
    rows: list[Row] = []
    for c in candidates:
        for split, qid in split_ids(sample, splits):
            thread = sample["sessions"][qid]
            try:
                gen_prompt = prompt(thread, tail(thread, tail_chars))
            except Exception:  # noqa: BLE001 -- an unreadable transcript has no output
                rows.append(Row(c, split, qid, None, None, None, None))
                continue
            cached = read_json(generate_path(work, generate_key(c, gen_prompt, qid)))
            if cached is None:
                rows.append(Row(c, split, qid, None, None, None, None))
                continue
            try:
                j_prompt: str | None = judge_prompt(
                    thread,
                    tail(thread, judge_tail_chars),
                    cached["output"],
                    with_label=split in LABELLED_SPLITS,
                    tail_chars=judge_tail_chars,
                )
            except Exception:  # noqa: BLE001 -- an unreadable transcript cannot be judged
                j_prompt = None
            verdict = read_json(judge_path(work, judge_key(judge, j_prompt))) if j_prompt else None
            rows.append(
                Row(
                    c,
                    split,
                    qid,
                    cached["output"],
                    cached["stats"],
                    verdict["verdict"] if verdict else None,
                    j_prompt,
                    cached.get("at"),
                )
            )
    return rows


def run_judge(
    work: Path,
    rows: Sequence[Row],
    *,
    judge: Candidate,
    make_model: ModelFactory,
    concurrency: int = 4,
    progress: Progress | None = None,
) -> dict[str, Any]:
    pending = [r for r in rows if r.output is not None and r.verdict is None and r.judge_prompt]
    todo = list({judge_key(judge, r.judge_prompt or ""): r for r in pending}.values())
    model = make_model(judge, JUDGE_TIMEOUT)
    attempted: dict[str, dict[str, str | None]] = {}
    done = [0]

    def call(row: Row, stopped: Callable[[], bool]) -> dict[str, Any]:
        prompt = row.judge_prompt or ""
        raw, stats = call_with_stats(with_cancel(model, stopped), prompt, JUDGE_SCHEMA)
        return {
            "judge": judge.spec,
            "candidate": row.candidate.spec,
            "qid": row.qid,
            "verdict": parse_verdict(raw),
            "stats": stats_dict(stats),
        }

    def landed(row: Row, outcome: dict[str, Any] | Exception) -> None:
        done[0] += 1
        bucket = attempted.setdefault(row.candidate.spec, {})
        if isinstance(outcome, Exception):
            bucket[row.qid] = error_text(outcome)
        else:
            write_json(judge_path(work, judge_key(judge, row.judge_prompt or "")), outcome)
            bucket[row.qid] = None
        if progress:
            progress("judge", done[0], len(todo))

    run_pool(todo, call, concurrency=concurrency, on_result=landed)
    for c in {r.candidate for r in todo}:
        update_errors(work, "judge", c, attempted[c.spec])
    return {
        spec: {
            "calls": len(results),
            "errors": sum(1 for msg in results.values() if msg),
            "errorMessages": dict(Counter(m for m in results.values() if m)),
        }
        for spec, results in attempted.items()
    }


def percentile(values: Sequence[float], share: float) -> float | None:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)] if ordered else None


def latency(seconds: Sequence[float]) -> dict[str, float | None]:
    return {
        "median": statistics.median(seconds) if seconds else None,
        "p90": percentile(seconds, P90),
    }


def rate(rows: Sequence[Row], flag: str) -> float | None:
    judged = [r.verdict for r in rows if r.verdict is not None]
    return sum(1 for v in judged if v[flag]) / len(judged) if judged else None


def implied_finished(
    split: str, stored: Any, output: Mapping[str, Any], verdict: Mapping[str, Any]
) -> bool:
    if split in LABELLED_SPLITS and not verdict["label_disagrees"]:
        return bool(stored == "finished")
    predicted = output["status"] == "finished"
    return predicted if verdict["status_ok"] else not predicted


def confusion(rows: Sequence[Row], sessions: Mapping[str, Thread]) -> dict[str, int] | None:
    pairs = Counter(
        (
            implied_finished(r.split, sessions[r.qid].get("status"), r.output, r.verdict),
            r.output["status"] == "finished",
        )
        for r in rows
        if r.output is not None and r.verdict is not None
    )
    if not pairs:
        return None
    return {
        "finished_called_finished": pairs[(True, True)],
        "finished_called_open": pairs[(True, False)],
        "open_called_finished": pairs[(False, True)],
        "open_called_open": pairs[(False, False)],
    }


def summed(values: Iterable[Any]) -> float | None:
    known = [v for v in values if isinstance(v, int | float)]
    return sum(known) if known else None


def result_entry(
    c: Candidate,
    split: str,
    rows: Sequence[Row],
    sessions: Mapping[str, Thread],
    errors: Mapping[str, str],
    judge_errors: Mapping[str, str],
) -> dict[str, Any]:
    generated = [r for r in rows if r.output is not None]
    stats = [r.stats or {} for r in generated]
    rates = {field: rate(rows, f"{field}_ok") for field in FIELDS}
    known = [v for v in rates.values() if v is not None]
    mean = sum(known) / len(known) if len(known) == len(FIELDS) else None
    status = rates["status"]
    missing = [r.qid for r in rows if r.output is None]
    unjudged = [r.qid for r in generated if r.verdict is None]
    stamps = sorted(r.at for r in generated if r.at)
    return {
        "candidate": c.spec,
        "split": split,
        "n": len(rows),
        "outputs": len(generated),
        "missing": len(missing),
        "errors": sum(1 for qid in missing if qid in errors),
        "errorMessages": dict(Counter(errors[q] for q in missing if q in errors)),
        "judged": sum(1 for r in rows if r.verdict is not None),
        "judgeErrors": sum(1 for qid in unjudged if qid in judge_errors),
        "judgeErrorMessages": dict(Counter(judge_errors[q] for q in unjudged if q in judge_errors)),
        "generatedAt": {"first": stamps[0], "last": stamps[-1]} if stamps else None,
        "rates": rates,
        "headline": {"status": status, "mean": mean},
        "meetsBar": (
            None
            if split not in LABELLED_SPLITS
            or status is None
            or mean is None
            or any(r.verdict is None for r in rows)
            else status >= BAR and mean >= BAR
        ),
        "confusion": confusion(rows, sessions),
        "latency": latency([s["seconds"] for s in stats if isinstance(s.get("seconds"), float)]),
        "inputTokens": summed(s.get("inputTokens") for s in stats),
        "outputTokens": summed(s.get("outputTokens") for s in stats),
        "costUsd": summed(s.get("costUsd") for s in stats),
        "meanCostUsd": (
            cost / len(generated)
            if (cost := summed(s.get("costUsd") for s in stats)) is not None
            else None
        ),
    }


def projection(
    entries: Sequence[Mapping[str, Any]], pending: int, concurrency: int
) -> dict[str, Any]:
    by_candidate: dict[str, list[float]] = {}
    costs: dict[str, list[float]] = {}
    for entry in entries:
        if entry["latency"]["median"] is not None:
            by_candidate.setdefault(entry["candidate"], []).append(entry["latency"]["median"])
        if entry["meanCostUsd"] is not None:
            costs.setdefault(entry["candidate"], []).append(entry["meanCostUsd"])
    return {
        spec: {
            "pending": pending,
            "concurrency": concurrency,
            "seconds": pending * statistics.median(medians) / max(concurrency, 1),
            "costUsd": pending * statistics.mean(costs[spec]) if spec in costs else None,
        }
        for spec, medians in by_candidate.items()
    }


def findings(rows: Sequence[Row]) -> list[dict[str, Any]]:
    return [
        {
            "qid": r.qid,
            "candidate": r.candidate.spec,
            "split": r.split,
            "failed": [f for f in FIELDS if not r.verdict[f"{f}_ok"]],
            "labelDisagrees": r.verdict["label_disagrees"],
            "rationale": r.verdict["rationale"][:RATIONALE_CHARS],
        }
        for r in rows
        if r.verdict is not None
        and (r.verdict["label_disagrees"] or not all(r.verdict[f"{f}_ok"] for f in FIELDS))
    ]


def spot_check(
    rows: Sequence[Row], sessions: Mapping[str, Thread], seed: int
) -> list[dict[str, Any]]:
    judged = sorted((r for r in rows if r.verdict is not None), key=lambda r: (r.split, r.qid))
    picked = random.Random(seed).sample(judged, min(SPOT_CHECK_ROWS, len(judged)))
    return [
        {
            "qid": r.qid,
            "split": r.split,
            "output": r.output,
            "label": (
                {k: sessions[r.qid].get(k) for k in ("status", "whatItIs", "suggestedName")}
                if r.split in LABELLED_SPLITS
                else None
            ),
            "verdict": {f: r.verdict[f"{f}_ok"] for f in FIELDS} if r.verdict else None,
        }
        for r in sorted(picked, key=lambda r: (r.split, r.qid))
    ]


def build_report(  # noqa: PLR0913 -- injection points the suite drives
    work: Path,
    sample: Mapping[str, Any],
    rows: Sequence[Row],
    *,
    candidates: Sequence[Candidate],
    splits: Sequence[str],
    judge: Candidate,
    concurrency: int,
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    sessions = sample["sessions"]
    errors = {c.spec: read_errors(work, "generate", c) for c in candidates}
    judge_errors = {c.spec: read_errors(work, "judge", c) for c in candidates}
    entries = [
        result_entry(
            c,
            split,
            [r for r in rows if r.candidate == c and r.split == split],
            sessions,
            errors[c.spec],
            judge_errors[c.spec],
        )
        for c in candidates
        for split in splits
    ]
    return {
        **meta,
        "judge": judge.spec,
        "bar": {"status": BAR, "meanOfFields": BAR, "split": "heldout"},
        "sample": sample_stats(sample),
        "results": entries,
        "projection": projection(entries, sample["pendingCount"], concurrency),
        "findings": findings(rows),
        "spotCheck": {
            c.spec: spot_check([r for r in rows if r.candidate == c], sessions, sample["seed"])
            for c in candidates
        },
    }


def cell(value: Any) -> str:
    text = "—" if value is None else str(value)
    return " ".join(text.replace("|", "\\|").split())


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def secs(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def table(header: Sequence[str], body: Iterable[Sequence[Any]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
        *("| " + " | ".join(cell(v) for v in row) + " |" for row in body),
    ]


def verdict_text(verdict: Mapping[str, bool] | None) -> str:
    if verdict is None:
        return "not judged"
    return " ".join(f"{f} {'ok' if verdict[f] else 'FAIL'}" for f in FIELDS)


def output_text(output: Mapping[str, Any] | None) -> str:
    if not output:
        return "—"
    return f"{output.get('status')} · {output.get('whatItIs')} · {output.get('suggestedName')}"


def sample_lines(stats: Mapping[str, Any]) -> list[str]:
    splits = stats["splits"]
    return [
        (
            f"Sample: seed {stats['seed']}, drawn {stats['drawnAt']}; "
            f"{stats['labelledCount']} labelled sessions in the registry copy, "
            f"{stats['pendingCount']} pending classification. Dropped for an unreadable "
            f"transcript: {stats['dropped']['labelled']} labelled, "
            f"{stats['dropped']['backlog']} backlog."
        ),
        "",
        *table(
            ("split", "n", "by source", "by stored status"),
            (
                (
                    split,
                    splits[split]["n"],
                    ", ".join(f"{k} {v}" for k, v in splits[split]["bySource"].items()),
                    ", ".join(f"{k} {v}" for k, v in splits[split]["byStatus"].items()),
                )
                for split in SPLITS
            ),
        ),
    ]


def short_identity(qid: str) -> str:
    source, _, session = qid.partition(":")
    return f"{source}:{session[:8]}"


def error_summary(messages: Mapping[str, int], *, local: bool) -> str:
    kinds = Counter[str]()
    for message, count in messages.items():
        kinds[message if local else message.partition(":")[0]] += count
    return "; ".join(f"{m} (x{n})" for m, n in kinds.items())


def generated_span(stamps: Mapping[str, str] | None) -> str | None:
    if not stamps:
        return None
    first, last = stamps["first"][:10], stamps["last"][:10]
    return first if first == last else f"{first} to {last}"


def finding_line(f: Mapping[str, Any], *, local: bool) -> str:
    line = (
        f"- `{short_identity(f['qid']) if not local else f['qid']}` ({f['candidate']}, "
        f"{f['split']}): failed {', '.join(f['failed']) or 'none'}"
        f"{'; stored label disagrees' if f['labelDisagrees'] else ''}"
    )
    return f"{line} — {cell(f['rationale'])}" if local else line


def error_rows(results: Sequence[Mapping[str, Any]], *, local: bool) -> list[tuple[Any, ...]]:
    return [
        (e["candidate"], e["split"], phase, e[count], error_summary(e[messages], local=local))
        for e in results
        for phase, count, messages in (
            ("generate", "errors", "errorMessages"),
            ("judge", "judgeErrors", "judgeErrorMessages"),
        )
        if e[count]
    ]


def render_markdown(
    report: Mapping[str, Any],
    *,
    title: str,
    local: bool = False,
    local_report: Path | None = None,
) -> str:
    results = report["results"]
    judged = sum(entry["judged"] for entry in results)
    lines = [
        f"# Enrich benchmark — {title}",
        "",
        (
            f"Date: {report['date']}. Commit: `{report['commit']}`. "
            f"Prompt: `{report['promptLabel']}` (hash `{report['promptHash']}`), "
            f"tail {report['tailChars']} chars; judge `{report['judge']}` "
            f"(tail {report['judgeTailChars']} chars), {judged} verdicts."
        ),
        "",
        *sample_lines(report["sample"]),
        "",
        "## Results",
        "",
        (
            f"Bar (held-out labelled split): status ≥ {pct(BAR)} and mean of the three "
            f"fields ≥ {pct(BAR)}."
        ),
        "",
        *table(
            RESULT_HEADER,
            (
                (
                    e["candidate"],
                    e["split"],
                    e["n"],
                    e["outputs"],
                    e["errors"],
                    e["judged"],
                    pct(e["rates"]["status"]),
                    pct(e["rates"]["what"]),
                    pct(e["rates"]["name"]),
                    pct(e["headline"]["mean"]),
                    {True: "yes", False: "NO", None: "—"}[e["meetsBar"]],
                )
                for e in results
            ),
        ),
        "",
        "## Latency and cost",
        "",
        *table(
            (
                "candidate",
                "split",
                "median s",
                "p90 s",
                "input tokens",
                "output tokens",
                "cost $",
                "outputs generated",
            ),
            (
                (
                    e["candidate"],
                    e["split"],
                    secs(e["latency"]["median"]),
                    secs(e["latency"]["p90"]),
                    e["inputTokens"],
                    e["outputTokens"],
                    None if e["costUsd"] is None else f"{e['costUsd']:.4f}",
                    generated_span(e["generatedAt"]),
                )
                for e in results
            ),
        ),
        "",
        "Projected backlog (pending * median latency / concurrency):",
        "",
        *table(
            ("candidate", "pending", "concurrency", "hours", "cost $"),
            (
                (
                    spec,
                    p["pending"],
                    p["concurrency"],
                    f"{p['seconds'] / 3600:.1f}",
                    None if p["costUsd"] is None else f"{p['costUsd']:.2f}",
                )
                for spec, p in report["projection"].items()
            ),
        ),
        "",
        "## Finished vs not (judge-corrected truth)",
        "",
        *(
            table(
                CONFUSION_HEADER,
                (
                    (e["candidate"], e["split"], *e["confusion"].values())
                    for e in results
                    if e["confusion"]
                ),
            )
            if judged
            else ["Pending: no judge verdicts yet."]
        ),
        "",
        "## Errors",
        "",
        *(
            table(
                ("candidate", "split", "phase", "errors", "messages" if local else "kinds"),
                error_rows(results, local=local),
            )
            if error_rows(results, local=local)
            else ["None."]
        ),
        "",
        "## Label disagreements and failed fields",
        "",
        *(
            [finding_line(f, local=local) for f in report["findings"]]
            or ["None." if judged else "Pending: no judge verdicts yet."]
        ),
    ]
    if not local:
        where = (
            f"`reports/{local_report.name}` in the `--work` dir (default `$TMPDIR/rl-enrich-bench`)"
            if local_report
            else "the work dir's `reports/` folder"
        )
        lines += [
            "",
            "## Spot check",
            "",
            f"The spot-check table is derived from private transcripts and is not committed. It is in {where}.",
        ]
        return "\n".join(lines) + "\n"
    for spec, rows in report["spotCheck"].items():
        lines += [
            "",
            f"## Spot check — {spec}",
            "",
            *(
                table(
                    ("identity", "candidate output", "stored label", "judge", "human agrees?"),
                    (
                        (
                            r["qid"],
                            output_text(r["output"]),
                            output_text(r["label"]),
                            verdict_text(r["verdict"]),
                            "",
                        )
                        for r in rows
                    ),
                )
                if rows
                else ["Pending: no judge verdicts yet."]
            ),
        ]
    return "\n".join(lines) + "\n"


def ensure_sample(
    work: Path,
    registry_path: Path,
    *,
    seed: int,
    now_iso: str,
    readable: Callable[[Thread], bool],
) -> dict[str, Any]:
    existing = read_json(work / "sample.json")
    if existing is not None:
        return dict(existing)
    copy = work / "registry.json"
    shutil.copyfile(registry_path, copy)
    sample = draw_sample(
        registry.load_registry(copy),
        now_iso=now_iso,
        seed=seed,
        readable=readable,
        source=str(registry_path),
    )
    write_json(work / "sample.json", sample)
    return sample


def smoke(  # noqa: PLR0913 -- injection points the suite drives
    sample: Mapping[str, Any],
    candidates: Sequence[Candidate],
    splits: Sequence[str],
    *,
    judge: Candidate,
    make_model: ModelFactory,
    tail: TailReader,
    tail_chars: int,
    judge_tail_chars: int,
) -> dict[str, Any]:
    split, qid = split_ids(sample, splits)[0]
    thread = sample["sessions"][qid]
    prompt = enrichment.enrich_prompt(thread, tail(thread, tail_chars))
    calls: list[dict[str, Any]] = []
    first: dict[str, Any] | None = None
    for c in candidates:
        try:
            raw, stats = call_with_stats(
                make_model(c, CALL_TIMEOUT_SECONDS["enrich"]), prompt, enrichment.ENRICH_SCHEMA
            )
            found = enrichment.parse_classification(raw)
            first = first or {
                "status": found.status,
                "whatItIs": found.what_it_is,
                "suggestedName": found.suggested_name,
            }
            calls.append({"role": "candidate", "spec": c.spec, "ok": True, **stats_dict(stats)})
        except Exception as error:  # noqa: BLE001 -- a smoke run reports every failure
            calls.append(
                {"role": "candidate", "spec": c.spec, "ok": False, "error": error_text(error)}
            )
    if first is not None:
        j_prompt = judge_prompt(
            thread,
            tail(thread, judge_tail_chars),
            first,
            with_label=split in LABELLED_SPLITS,
            tail_chars=judge_tail_chars,
        )
        try:
            raw, stats = call_with_stats(make_model(judge, JUDGE_TIMEOUT), j_prompt, JUDGE_SCHEMA)
            parse_verdict(raw)
            calls.append({"role": "judge", "spec": judge.spec, "ok": True, **stats_dict(stats)})
        except Exception as error:  # noqa: BLE001 -- a smoke run reports every failure
            calls.append(
                {"role": "judge", "spec": judge.spec, "ok": False, "error": error_text(error)}
            )
    return {"session": qid, "split": split, "calls": calls}


def git_commit() -> str:
    head = Path(__file__).resolve().parent.parent / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            return (head.parent / ref[5:]).read_text(encoding="utf-8").strip()[:12]
        return ref[:12]
    except OSError:
        return "unknown"


def default_work() -> Path:
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / "rl-enrich-bench"


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="benchmark_enrich.py",
        description="Benchmark enrich models against labelled sessions (read-only on live data).",
    )
    parser.add_argument("--registry", type=Path, default=paths.default_registry())
    parser.add_argument("--work", type=Path, default=default_work())
    parser.add_argument("--phase", choices=(*PHASES, "all"), default="all")
    parser.add_argument("--split", type=parse_splits, default=SPLITS)
    parser.add_argument("--candidates", type=parse_candidates, default="claude:haiku")
    parser.add_argument("--judge", type=parse_candidate, default="claude:sonnet")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--tail-chars", type=int, default=enrichment.TAIL_CHARS)
    parser.add_argument("--judge-tail-chars", type=int, default=JUDGE_TAIL_CHARS)
    parser.add_argument("--prompt-label", default="current")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--out", type=Path)
    return parser.parse_args(argv)


def print_progress(phase: str, done: int, total: int) -> None:
    if done == total or done % 10 == 0:
        print(f"{phase}: {done}/{total}", file=sys.stderr, flush=True)


def main(
    argv: Sequence[str] | None = None,
    *,
    make_model: ModelFactory = build_model,
    read_tail: TailReader = enrichment.session_tail,
    now: datetime | None = None,
) -> int:
    args = parse_args(argv)
    work: Path = args.work
    work.mkdir(parents=True, exist_ok=True)
    moment = now or datetime.now(UTC)
    tail = cached_tail(work, read_tail)
    sample = ensure_sample(
        work,
        args.registry,
        seed=args.seed,
        now_iso=moment.isoformat(timespec="seconds"),
        readable=tail_readable(tail, args.tail_chars),
    )
    if args.smoke:
        print(
            json.dumps(
                smoke(
                    sample,
                    args.candidates,
                    args.split,
                    judge=args.judge,
                    make_model=make_model,
                    tail=tail,
                    tail_chars=args.tail_chars,
                    judge_tail_chars=args.judge_tail_chars,
                ),
                indent=2,
            )
        )
        return 0
    phases = PHASES if args.phase == "all" else (args.phase,)
    summary: dict[str, Any] = {"sample": sample_stats(sample)}
    try:
        if "generate" in phases:
            summary["generate"] = generate(
                work,
                sample,
                args.candidates,
                args.split,
                make_model=make_model,
                tail=tail,
                tail_chars=args.tail_chars,
                prompt_label=args.prompt_label,
                concurrency=args.concurrency,
                progress=print_progress,
            )

        def rows() -> list[Row]:
            return collect_rows(
                work,
                sample,
                args.candidates,
                args.split,
                judge=args.judge,
                tail=tail,
                tail_chars=args.tail_chars,
                judge_tail_chars=args.judge_tail_chars,
            )

        if "judge" in phases:
            summary["judge"] = run_judge(
                work,
                rows(),
                judge=args.judge,
                make_model=make_model,
                concurrency=args.concurrency,
                progress=print_progress,
            )
        if "report" in phases:
            report = build_report(
                work,
                sample,
                rows(),
                candidates=args.candidates,
                splits=args.split,
                judge=args.judge,
                concurrency=args.concurrency,
                meta={
                    "date": moment.date().isoformat(),
                    "commit": git_commit(),
                    "promptLabel": args.prompt_label,
                    "promptHash": prompt_hash(enrichment.enrich_prompt),
                    "tailChars": args.tail_chars,
                    "judgeTailChars": args.judge_tail_chars,
                },
            )
            summary["report"] = report
            local_report = work / "reports" / f"{args.prompt_label}-{'-'.join(args.split)}.md"
            local_report.parent.mkdir(parents=True, exist_ok=True)
            local_report.write_text(
                render_markdown(report, title=local_report.stem, local=True), encoding="utf-8"
            )
            summary["localReport"] = str(local_report)
            if args.out is not None:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(
                    render_markdown(report, title=args.out.stem, local_report=local_report),
                    encoding="utf-8",
                )
    except KeyboardInterrupt:
        print("interrupted; finished calls are cached", file=sys.stderr)
        return 130
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
