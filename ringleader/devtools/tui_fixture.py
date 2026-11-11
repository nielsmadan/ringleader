from __future__ import annotations

import json
import os
import random
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import refresh, registry
from ..slug import droid_slug, slugify
from ..sources.claude_code import ClaudeCodeSource
from .isolation import agent_store_env

FILTERS = ("zz-hidden*",)
SOURCE_WEIGHTS = (("claude-code", 64), ("codex", 18), ("opencode", 12), ("pi", 3), ("droid", 3))
CLASSIFIED_STATUSES = ("unfinished", "finished", "active", "blocked", "abandoned")
WORDS = (
    "fix",
    "flaky",
    "login",
    "parser",
    "refactor",
    "cache",
    "deploy",
    "migrate",
    "schema",
    "docs",
    "release",
    "bench",
    "search",
    "picker",
    "export",
    "hooks",
)
DAY = 86_400
RECENT_SHARE = 0.13
NAMED_SHARE = 0.5


@dataclass(frozen=True)
class Fixture:
    root: Path
    registry_path: Path
    env: dict[str, str]


@dataclass(frozen=True)
class _Plan:
    index: int
    session_id: str
    source: str
    project: str
    mtime: datetime
    message_count: int
    title: str | None
    ai_title: str | None
    prompt: str
    size_bytes: int
    role: str = "plain"


def _env(root: Path) -> dict[str, str]:
    return {
        **agent_store_env(root),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
    }


def _phrase(rng: random.Random, count: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(count))


def _plan(
    rng: random.Random, index: int, *, source: str, project: Path, now: datetime, max_age: int
) -> _Plan:
    recent = rng.random() < RECENT_SHARE
    age = rng.randint(60, 7 * DAY) if recent else rng.randint(7 * DAY, max_age * DAY)
    named = rng.random() < NAMED_SHARE
    words = f"{_phrase(rng, 3)} {index}"
    message_count = rng.randint(2, 24)
    return _Plan(
        index=index,
        session_id=str(uuid.UUID(int=rng.getrandbits(128))),
        source=source,
        project=str(project),
        mtime=now - timedelta(seconds=age),
        message_count=message_count,
        title=words if named else None,
        ai_title=None if named else words,
        prompt=f"please {_phrase(rng, 6)}",
        size_bytes=message_count * rng.randint(400, 4000),
    )


def _role_counts(threads: int) -> list[tuple[str, int]]:
    return [
        ("harvested", 1),
        ("finishing", 1),
        ("snoozed-expired", 1),
        ("snoozed-soon", max(1, threads // 60)),
        ("kept", max(1, threads // 40)),
        ("hidden", max(1, round(threads * 0.01))),
        ("classified", max(1, round(threads * 0.05))),
    ]


def _roles(rng: random.Random, threads: int) -> dict[int, str]:
    order = list(range(threads))
    rng.shuffle(order)
    roles: dict[int, str] = {}
    for role, count in _role_counts(threads):
        start = len(roles)
        roles.update(dict.fromkeys(order[start : start + count], role))
    return roles


def _hide(plan: _Plan) -> _Plan:
    name = f"zz-hidden {plan.title or plan.ai_title}"
    return replace(plan, title=name, ai_title=None)


def _transcript_path(root: Path, plan: _Plan) -> Path:
    slug = slugify(plan.project)
    stem = plan.session_id
    return {
        "claude-code": ClaudeCodeSource(projects_dir=root / "_claude").storage_for(plan.project)
        / f"{stem}.jsonl",
        "codex": root / "_codex" / f"{plan.mtime:%Y/%m/%d}" / f"rollout-{stem}.jsonl",
        "opencode": root / "xdg-data" / "opencode" / "storage" / "session" / f"{stem}.json",
        "pi": root / "_pi" / slug / f"{stem}.jsonl",
        "droid": root
        / "_factory"
        / ".factory"
        / "sessions"
        / droid_slug(plan.project)
        / f"{stem}.jsonl",
    }[plan.source]


def _iso(when: datetime) -> str:
    return when.isoformat()


def _thread(root: Path, plan: _Plan) -> dict[str, Any]:
    mtime_ns = int(plan.mtime.timestamp()) * 1_000_000_000
    return {
        "sessionId": plan.session_id,
        "source": plan.source,
        "sourceRef": plan.session_id,
        "file": str(_transcript_path(root, plan)),
        "project": plan.project,
        "title": plan.title,
        "aiTitle": plan.ai_title,
        "summary": None,
        "lastPrompt": plan.prompt,
        "mtime": _iso(plan.mtime),
        "startedAt": _iso(plan.mtime - timedelta(days=7)),
        "sizeBytes": plan.size_bytes,
        "messageCount": plan.message_count,
        "gitBranch": None,
        "cacheVersion": 1,
        "fileFingerprint": f"1:{plan.index}:{plan.size_bytes}:{mtime_ns}:{mtime_ns}",
        "gitProjectKey": None,
        "gitProjectLabel": None,
        "gitRoot": None,
        "gitRemote": None,
        "gitCloneUrl": None,
        "status": "unclassified",
        "statusConfidence": 0.0,
        "whatItIs": None,
        "suggestedName": None,
        "appliedName": None,
        "namedAtMtime": None,
        "reviewedAtMtime": None,
        "classifiedAtMtime": None,
        "decision": None,
        "harvestPath": None,
        "todoId": None,
        "tags": [],
        "locationPinned": False,
        "snoozedAt": None,
        "keptAt": None,
        "selectedForDay": None,
        "filteredBy": None,
    }


def _classified(thread: dict[str, Any], index: int) -> dict[str, Any]:
    status = CLASSIFIED_STATUSES[index % len(CLASSIFIED_STATUSES)]
    return {
        **thread,
        "status": status,
        "statusConfidence": 0.8,
        "whatItIs": f"work on {thread.get('title') or thread.get('aiTitle')}",
        "suggestedName": f"session-{index:04d}",
        "classifiedAtMtime": thread["mtime"],
    }


def _stamp(thread: dict[str, Any], role: str, index: int, now: datetime) -> dict[str, Any]:
    if role == "classified":
        return _classified(thread, index)
    if role == "kept":
        return {**thread, "decision": "keep", "keptAt": _iso(now - timedelta(days=3 + index % 20))}
    if role == "snoozed-soon":
        until = now + timedelta(hours=6 + (index % 6) * 11)
        return {**thread, "snoozedAt": _iso(now - timedelta(days=4)), "snoozedUntil": _iso(until)}
    if role == "snoozed-expired":
        until = now - timedelta(days=1)
        return {**thread, "snoozedAt": _iso(now - timedelta(days=9)), "snoozedUntil": _iso(until)}
    if role == "harvested":
        return {**_classified(thread, 1), "status": "finished", "decision": "harvested"}
    if role == "finishing":
        return {**thread, "finishOnExit": True, "finishedAt": _iso(now - timedelta(hours=2))}
    return thread


def _between(plan: _Plan, now: datetime) -> str:
    return _iso(plan.mtime + (now - plan.mtime) / 2)


def _archived(root: Path, plan: _Plan, now: datetime) -> dict[str, Any]:
    thread = _classified(_thread(root, plan), plan.index)
    return {
        "sessionId": plan.session_id,
        "source": plan.source,
        "archivedAt": _between(plan, now),
        "archivePath": str(
            root / "xdg-data" / "ringleader" / "archive" / f"{plan.session_id}.jsonl"
        ),
        "harvestPath": None,
        "summary": thread["whatItIs"],
        "project": plan.project,
        "originalPath": thread["file"],
        "thread": {**thread, "status": "finished"},
    }


def _deleted(root: Path, plan: _Plan, now: datetime) -> dict[str, Any]:
    return {
        "sessionId": plan.session_id,
        "deletedAt": _between(plan, now),
        "reason": "prune" if plan.index % 3 else "gc",
        "source": plan.source,
        "thread": _thread(root, plan),
        "harvestPath": None,
    }


def _sources(rng: random.Random, count: int, *, transcripts: bool) -> list[str]:
    if transcripts:
        return ["claude-code"] * count
    names = [name for name, _ in SOURCE_WEIGHTS]
    weights = [weight for _, weight in SOURCE_WEIGHTS]
    return rng.choices(names, weights=weights, k=count)


def _plans(
    rng: random.Random, projects: list[Path], now: datetime, *, threads: int, transcripts: bool
) -> list[_Plan]:
    sources = _sources(rng, threads, transcripts=transcripts)
    plans = [
        _plan(
            rng,
            index,
            source=sources[index],
            project=projects[index % len(projects)],
            now=now,
            max_age=120,
        )
        for index in range(threads)
    ]
    roles = _roles(rng, threads)
    staged = [replace(p, role=roles.get(p.index, "plain")) for p in plans]
    return [_hide(p) if p.role == "hidden" else p for p in staged]


def _tombstone_plans(
    rng: random.Random, projects: list[Path], now: datetime, *, count: int, offset: int
) -> list[_Plan]:
    sources = _sources(rng, count, transcripts=False)
    return [
        _plan(
            rng,
            offset + k,
            source=sources[k],
            project=projects[k % len(projects)],
            now=now,
            max_age=240,
        )
        for k in range(count)
    ]


def _transcript_lines(plan: _Plan) -> list[dict[str, Any]]:
    title = (
        {"type": "custom-title", "customTitle": plan.title, "sessionId": plan.session_id}
        if plan.title
        else {"type": "ai-title", "aiTitle": plan.ai_title, "sessionId": plan.session_id}
    )
    turns = [
        {
            "type": "user" if k % 2 == 0 else "assistant",
            "timestamp": _iso(plan.mtime - timedelta(days=7) if k == 0 else plan.mtime),
            "cwd": plan.project,
            "sessionId": plan.session_id,
            "message": {
                "role": "user" if k % 2 == 0 else "assistant",
                "content": plan.prompt if k % 2 == 0 else f"done step {k}",
            },
        }
        for k in range(plan.message_count)
    ]
    return [title, *turns]


def _write_transcript(root: Path, plan: _Plan) -> None:
    path = _transcript_path(root, plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(line) + "\n" for line in _transcript_lines(plan)), encoding="utf-8"
    )
    stamp = plan.mtime.timestamp()
    os.utime(path, (stamp, stamp))


def _scanned_threads(root: Path, registry_path: Path, now: datetime) -> list[dict[str, Any]]:
    reg, _ = refresh.auto_refresh(
        registry_path,
        _iso(now),
        claude_projects_dir=root / "_claude",
        git_resolver=lambda _: None,
        name_filters=list(FILTERS),
        persist=False,
    )
    return list(reg["threads"])


def build_fixture(
    root: Path, *, threads: int, now: datetime, seed: int = 7, transcripts: bool = False
) -> Fixture:
    rng = random.Random(seed)
    now = now.replace(microsecond=0)
    env = _env(root)
    data_dir = root / "xdg-data" / "ringleader"
    data_dir.mkdir(parents=True, exist_ok=True)
    (root / "xdg-state").mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(
        json.dumps({"session_filters": list(FILTERS)}), encoding="utf-8"
    )
    registry_path = data_dir / "registry.json"
    projects = [root / "work" / f"proj-{k:02d}" for k in range(max(1, threads // 14))]
    for project in projects:
        project.mkdir(parents=True, exist_ok=True)

    plans = _plans(rng, projects, now, threads=threads, transcripts=transcripts)
    archived = _tombstone_plans(rng, projects, now, count=round(threads * 0.15), offset=threads)
    deleted = _tombstone_plans(
        rng, projects, now, count=round(threads * 0.5), offset=threads + len(archived)
    )
    by_id = {p.session_id: p for p in plans}
    if transcripts:
        for plan in plans:
            _write_transcript(root, plan)
    base = (
        _scanned_threads(root, registry_path, now)
        if transcripts
        else [_thread(root, p) for p in plans]
    )
    stamped = [
        _stamp(t, by_id[t["sessionId"]].role, by_id[t["sessionId"]].index, now) for t in base
    ]
    reg = {
        **registry.empty_registry(),
        "lastScan": _iso(now),
        "threads": stamped,
        "archived": [_archived(root, p, now) for p in archived],
        "deleted": [_deleted(root, p, now) for p in deleted],
    }
    registry.apply_name_filters(reg, FILTERS)
    registry_path.write_text(json.dumps(reg, indent=1), encoding="utf-8")
    return Fixture(root=root, registry_path=registry_path, env=env)
