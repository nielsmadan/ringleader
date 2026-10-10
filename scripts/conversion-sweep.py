from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ringleader import scanner
from ringleader.agents import canonical_agent
from ringleader.paths import verified_targets_path
from ringleader.portability.adapters import builtin_adapters
from ringleader.portability.capabilities import (
    AdapterRegistry,
    NeedsVerification,
    RunRequest,
    RunResult,
    SourceRequest,
)
from ringleader.portability.launch import ProbeRunner
from ringleader.portability.model import Identity, PreservationError
from ringleader.portability.service import ConversionRequest, ConversionService
from ringleader.portability.verification import load_verified
from ringleader.sources.base import RawRef

CUTOFF = datetime(2026, 10, 6, tzinfo=UTC)
LIMITS = {"claude-code": 200, "codex": 200}
NEEDS_VERIFICATION = "target needs self-verification"
INHERENTLY_EMPTY = frozenset(
    {"Codex rollout has no conversation turns", "Droid session has no conversation messages"}
)
PLACEHOLDERS = (
    (re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`"), "<q>"),
    (
        re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
        "<uuid>",
    ),
    (
        re.compile(r"(?<![\w/<])(?:~|\.{1,2})?/(?:[^\s:;,()\]]|\s(?=[^\s:;,()\]]*/))+"),
        "<path>",
    ),
    (re.compile(r"\b(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[\w-]{16,}\b"), "<id>"),
    (re.compile(r"\b[A-Za-z]+_[A-Za-z0-9]{6,}\b"), "<id>"),
    (re.compile(r"\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\b"), "<id>"),
    (re.compile(r"\b(?=[0-9a-z]*\d)[0-9a-z]{20,}\b"), "<id>"),
    (re.compile(r"\d+(?:\.\d+)*"), "<n>"),
    (re.compile(r"<id>(?:\s*[|,]\s*<id>)+"), "<id>"),
)


def normalize_reason(message: str) -> str:
    result = " ".join(message.split())
    for pattern, placeholder in PLACEHOLDERS:
        result = pattern.sub(placeholder, result)
    return result


class CachingRunner:
    def __init__(self) -> None:
        self.inner = ProbeRunner()
        self.results: dict[tuple[str, ...], RunResult] = {}

    def run(self, request: RunRequest) -> RunResult:
        if request.argv not in self.results:
            self.results[request.argv] = self.inner.run(request)
        return self.results[request.argv]


@dataclass(frozen=True)
class Pair:
    source: str
    target: str | None
    total: int
    ok: int
    categories: tuple[tuple[str, int], ...]
    excluded: tuple[tuple[str, int], ...] = ()

    @property
    def eligible(self) -> int:
        return self.total - sum(count for _, count in self.excluded)

    def as_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "total": self.total,
            "eligible": self.eligible,
            "ok": self.ok,
            "categories": [
                {"reason": reason, "count": count, "percent": percent(count, self.eligible)}
                for reason, count in self.categories
            ],
            "excluded": [{"reason": reason, "count": count} for reason, count in self.excluded],
        }


def percent(count: int, total: int) -> float:
    return round(100 * count / total, 1) if total else 0.0


def last_activity(ref: RawRef) -> datetime:
    value = datetime.fromisoformat(ref.mtime)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def sample(refs: Iterable[RawRef], cutoff: datetime, limit: int | None) -> list[RawRef]:
    eligible = sorted(
        (ref for ref in refs if last_activity(ref) < cutoff), key=last_activity, reverse=True
    )
    return eligible[:limit] if limit else eligible


def source_request(
    adapters: AdapterRegistry, agent: str, source: Any, ref: RawRef
) -> SourceRequest:
    session = source.parse(ref)
    if not session.cwd or not Path(session.cwd).is_absolute():
        raise PreservationError("session has no recorded absolute workspace")
    resolver = adapters.get(agent).roots
    if resolver is None:
        raise PreservationError(f"Native root resolution is unavailable for {agent}")
    roots, source_ref = resolver.resolve_source(Path(session.cwd), session.file or ref.file)
    return SourceRequest(
        Identity(agent, session.session_id or ref.identity or ""), roots, source_ref
    )


def reason(exc: BaseException) -> str:
    if isinstance(exc, NeedsVerification):
        return NEEDS_VERIFICATION
    prefix = "" if isinstance(exc, PreservationError) else type(exc).__name__ + ": "
    return normalize_reason(prefix + str(exc))


def _ranked(counts: Counter[str]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def tally(source: str, target: str | None, outcomes: list[str | None]) -> Pair:
    failures = Counter(item for item in outcomes if item is not None)
    empty = Counter({key: value for key, value in failures.items() if key in INHERENTLY_EMPTY})
    return Pair(
        source,
        target,
        len(outcomes),
        sum(item is None for item in outcomes),
        _ranked(failures - empty),
        _ranked(empty),
    )


def read_outcome(adapters: AdapterRegistry, request: SourceRequest | str) -> str | None:
    if isinstance(request, str):
        return request
    try:
        snapshot = adapters.reader(request.identity.agent).snapshot(request)
        if snapshot.conversation.source != request.identity:
            raise PreservationError("Source snapshot identity disagrees with selection")
    except Exception as exc:  # noqa: BLE001 -- every failure is a tallied category
        return reason(exc)
    return None


def convert_outcome(
    worker: ConversionService, request: SourceRequest | str, target: str
) -> str | None:
    if isinstance(request, str):
        return request
    try:
        worker.convert(
            ConversionRequest(request, target, worker.adapters.roots(target, request.roots.cwd)),
            dry_run=True,
        )
    except Exception as exc:  # noqa: BLE001 -- every failure is a tallied category
        return reason(exc)
    return None


def requests(
    adapters: AdapterRegistry, agent: str, source: Any, refs: list[RawRef]
) -> list[SourceRequest | str]:
    result: list[SourceRequest | str] = []
    for ref in refs:
        try:
            result.append(source_request(adapters, agent, source, ref))
        except Exception as exc:  # noqa: BLE001 -- every failure is a tallied category
            result.append(reason(exc))
    return result


def sweep(
    agent: str,
    source: Any,
    worker: ConversionService,
    targets: tuple[str, ...],
    *,
    limit: int | None,
    cutoff: datetime = CUTOFF,
) -> list[Pair]:
    refs = sample(source.discover(), cutoff, limit if limit else LIMITS.get(agent))
    built = requests(worker.adapters, agent, source, refs)
    if not targets:
        return [tally(agent, None, [read_outcome(worker.adapters, item) for item in built])]
    return [
        tally(agent, target, [convert_outcome(worker, item, target) for item in built])
        for target in targets
    ]


def render(pair: Pair) -> str:
    label = f"{pair.source} → {pair.target}" if pair.target else f"{pair.source} (reader)"
    verb = "convertible" if pair.target else "readable"
    lines = [f"{label}: {verb} {pair.ok}/{pair.eligible}"]
    lines.extend(
        f"  {count:4d}  {percent(count, pair.eligible):5.1f}%  {reason}"
        for reason, count in pair.categories
    )
    lines.extend(
        f"  {count:4d}  excluded, inherently empty: {reason}" for reason, count in pair.excluded
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry-run conversion sweep over real sessions")
    parser.add_argument("--source", required=True)
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("--target")
    targets.add_argument("--all-targets", action="store_true")
    parser.add_argument("--reader-only", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verified", type=Path, default=verified_targets_path())
    args = parser.parse_args()
    adapters = builtin_adapters(load_verified(args.verified))
    agent = canonical_agent(args.source)
    source = scanner.source_by_name(agent)
    if source is None:
        parser.error(f"unknown source {args.source}")
    chosen: tuple[str, ...] = ()
    if not args.reader_only:
        chosen = (
            adapters.agents()
            if args.all_targets or not args.target
            else (adapters.get(args.target).agent,)
        )
    with tempfile.TemporaryDirectory(prefix="rl-sweep-") as temporary:
        worker = ConversionService(
            adapters,
            CachingRunner(),
            registry_path=Path(temporary) / "registry.json",
            operations_path=Path(temporary) / "operations",
        )
        pairs = sweep(agent, source, worker, chosen, limit=args.limit)
    if args.json:
        print(json.dumps([pair.as_json() for pair in pairs], indent=2))
    else:
        print("\n".join(render(pair) for pair in pairs))


if __name__ == "__main__":
    main()
