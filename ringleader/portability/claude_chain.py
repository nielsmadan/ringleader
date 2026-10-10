from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import PreservationError

Row = dict[str, Any]
Span = tuple[int, int, list[Row], dict[str, list[Row]]]
PRESERVED_KEYS = frozenset({"anchorUuid", "uuids", "allUuids"})
SEGMENT_KEYS = frozenset({"headUuid", "anchorUuid", "tailUuid"})


@dataclass(frozen=True)
class Entries:
    rows: dict[str, Row]
    aliases: dict[str, str | None]
    positions: dict[str, int]

    def position(self, row: Row) -> int:
        return self.positions[row["uuid"]]


def is_boundary(row: Row) -> bool:
    return row.get("type") == "system" and row.get("subtype") == "compact_boundary"


def _content(row: Row) -> list[Any]:
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _blocks(row: Row, kind: str, key: str) -> list[str]:
    return [
        block[key]
        for block in _content(row)
        if isinstance(block, dict) and block.get("type") == kind and isinstance(block.get(key), str)
    ]


def call_ids(row: Row) -> list[str]:
    return _blocks(row, "tool_use", "id") if row["type"] == "assistant" else []


def result_ids(row: Row) -> list[str]:
    return _blocks(row, "tool_result", "tool_use_id") if row["type"] == "user" else []


def message_id(row: Row) -> str | None:
    message = row.get("message")
    value = message.get("id") if isinstance(message, dict) else None
    return value if isinstance(value, str) and value else None


def _resolve(aliases: dict[str, str | None], key: str | None) -> str | None:
    seen: set[str] = set()
    while key is not None and key in aliases:
        if key in seen:
            raise PreservationError("Cyclic Claude parent chain")
        seen.add(key)
        key = aliases[key]
    return key


def index(graph: list[Row]) -> Entries:
    rows: dict[str, Row] = {}
    aliases: dict[str, str | None] = {}
    for row in graph:
        key = row.get("uuid")
        parent = row.get("parentUuid")
        if not isinstance(key, str) or not key or key in rows or key in aliases:
            raise PreservationError("Duplicate or empty native entry ID")
        if parent is not None and not isinstance(parent, str):
            raise PreservationError("Invalid native parent identity")
        if row.get("type") == "progress":
            aliases[key] = parent
        else:
            rows[key] = row
    resolved = {
        key: {**row, "parentUuid": _resolve(aliases, row.get("parentUuid"))}
        if row.get("parentUuid") in aliases
        else row
        for key, row in rows.items()
    }
    return Entries(resolved, aliases, {key: i for i, key in enumerate(resolved)})


def preserved(boundary: Row, rows: dict[str, Row]) -> tuple[str, tuple[str, ...]] | None:
    metadata = boundary.get("compactMetadata") or {}
    listed = metadata.get("preservedMessages")
    if listed is not None:
        return listed["anchorUuid"], tuple(listed["uuids"])
    segment = metadata.get("preservedSegment")
    if segment is None:
        return None
    walked: list[str] = []
    row = rows.get(segment["tailUuid"])
    while row is not None and row["uuid"] not in walked:
        walked.append(row["uuid"])
        if row["uuid"] == segment["headUuid"]:
            return segment["anchorUuid"], tuple(reversed(walked))
        row = rows.get(row.get("parentUuid") or "")
    return None


def validate_metadata(metadata: dict[str, Any]) -> None:
    listed = metadata.get("preservedMessages")
    segment = metadata.get("preservedSegment")
    if listed is not None and (
        not isinstance(listed, dict)
        or set(listed) - PRESERVED_KEYS
        or not isinstance(listed.get("anchorUuid"), str)
        or any(
            not isinstance(listed.get(key, []), list)
            or any(not isinstance(item, str) for item in listed.get(key, []))
            for key in ("uuids", "allUuids")
        )
        or "uuids" not in listed
    ):
        raise PreservationError("Unsupported Claude retained-segment compaction metadata")
    if segment is not None and (
        not isinstance(segment, dict)
        or set(segment) != SEGMENT_KEYS
        or any(not isinstance(segment[key], str) for key in SEGMENT_KEYS)
    ):
        raise PreservationError("Unsupported Claude retained-segment compaction metadata")


def _before(entries: Entries, key: Any, boundary: Row) -> bool:
    return isinstance(key, str) and entries.positions.get(key, -1) in range(
        entries.position(boundary)
    )


def parent(entries: Entries, row: Row) -> str | None:
    if not is_boundary(row):
        value: str | None = row.get("parentUuid")
        return value
    raw_logical = row.get("logicalParentUuid")
    if raw_logical is not None and not isinstance(raw_logical, str):
        raise PreservationError("Invalid Claude logical parent identity")
    logical = _resolve(entries.aliases, raw_logical)
    if _before(entries, logical, row):
        return logical
    retained = preserved(row, entries.rows)
    earlier = [key for key in (retained[1] if retained else ()) if _before(entries, key, row)]
    return earlier[-1] if earlier else None


def walk(entries: Entries, leaf: str, *, logical: bool = True) -> list[Row]:
    path: list[Row] = []
    seen: set[str] = set()
    key: str | None = leaf
    while key is not None:
        if key in seen:
            raise PreservationError("Cyclic Claude parent chain")
        row = entries.rows.get(key)
        if row is None:
            raise PreservationError("Claude transcript references a missing parent entry")
        seen.add(key)
        path.append(row)
        key = parent(entries, row) if logical else row.get("parentUuid")
    return list(reversed(path))


def default_leaf(entries: Entries) -> str:
    parents = {parent(entries, row) for row in entries.rows.values()}
    candidates = []
    for terminal in entries.rows.values():
        if terminal["uuid"] in parents:
            continue
        row: Row | None = terminal
        seen: set[str] = set()
        while row is not None and row["type"] not in ("user", "assistant"):
            if row["uuid"] in seen:
                raise PreservationError("Cyclic Claude parent chain")
            seen.add(row["uuid"])
            row = entries.rows.get(parent(entries, row) or "")
        if row is not None:
            candidates.append((row, terminal))
    preferred = [pair for pair in candidates if not pair[0].get("isMeta")] or candidates
    if not preferred:
        raise PreservationError("Claude transcript has no conversational leaf")
    leaf: str = max(
        preferred, key=lambda pair: (entries.position(pair[0]), entries.position(pair[1]))
    )[1]["uuid"]
    return leaf


def leaf(entries: Entries, branch_id: str | None) -> str:
    if branch_id is None:
        return default_leaf(entries)
    key = _resolve(entries.aliases, branch_id) if branch_id in entries.aliases else branch_id
    if key not in entries.rows:
        raise PreservationError("Unknown selected Claude branch")
    return key


def _continues(row: Row, message: str) -> bool:
    kind = row["type"]
    if kind == "assistant":
        return message_id(row) == message
    if kind == "user":
        return row.get("isMeta") is True or bool(result_ids(row))
    return kind == "attachment" or (kind == "system" and not is_boundary(row))


def _context_only(row: Row) -> bool:
    if row["type"] == "attachment":
        return True
    if row["type"] == "system":
        return not is_boundary(row)
    return row["type"] == "user" and row.get("isMeta") is True and not result_ids(row)


def _tail(start: Row, children: dict[str, list[Row]], seen: set[str]) -> list[Row]:
    tail: list[Row] = []
    row: Row | None = start
    while row is not None:
        if row["uuid"] in seen or not _context_only(row):
            return []
        tail.append(row)
        later = [item for item in children.get(row["uuid"], []) if item["uuid"] not in seen]
        if len(later) > 1:
            return []
        row = later[0] if later else None
    return tail


@dataclass
class _Recovery:
    entries: Entries
    path: list[Row]
    seen: set[str]
    results: dict[str, list[Row]] = field(default_factory=dict)
    children: dict[str, list[Row]] = field(default_factory=dict)
    by_message: dict[str, list[Row]] = field(default_factory=dict)
    owners: dict[str, Row | None] = field(default_factory=dict)
    ranks: dict[str, float] = field(default_factory=dict)
    places: dict[str, int] = field(default_factory=dict)
    path_results: frozenset[str] = frozenset()

    def rank(self, row: Row) -> tuple[float, int]:
        position = self.entries.position(row)
        return self.ranks.get(row["uuid"], position), position

    def attach(self, key: str, row: Row) -> None:
        group = self.results.setdefault(key, [])
        if all(item["uuid"] != row["uuid"] for item in group):
            group.append(row)

    def tails(self, row: Row, out: list[Row]) -> None:
        rank = self.ranks.get(row["uuid"])
        for child in self.children.get(row["uuid"], []):
            for item in _tail(child, self.children, self.seen):
                self.seen.add(item["uuid"])
                out.append(item)
                if rank is not None:
                    self.ranks[item["uuid"]] = max(self.entries.position(item), rank)


def _same_agent(row: Row, other: Row) -> bool:
    return bool(row.get("agentId") == other.get("agentId"))


def _prepare(entries: Entries, path: list[Row]) -> _Recovery:
    state = _Recovery(
        entries,
        path,
        {row["uuid"] for row in path},
        places={row["uuid"]: i for i, row in enumerate(path)},
        path_results=frozenset(key for row in path for key in result_ids(row)),
    )
    misparented = []
    for row in entries.rows.values():
        if row["type"] == "assistant" and message_id(row):
            state.by_message.setdefault(message_id(row) or "", []).append(row)
        if row.get("parentUuid"):
            state.children.setdefault(row["parentUuid"], []).append(row)
        ids = result_ids(row)
        if not ids:
            continue
        if row.get("parentUuid"):
            state.attach(row["parentUuid"], row)
        owner = entries.rows.get(row.get("parentUuid") or "")
        if not set(ids) <= set(call_ids(owner) if owner else ()):
            misparented.append(row)
    for group in state.by_message.values() if misparented else ():
        for row in group:
            for call in call_ids(row):
                prior = state.owners.get(call, row)
                same = prior is not None and message_id(prior) == message_id(row)
                state.owners[call] = row if same else None
    for row in misparented:
        source = row.get("sourceToolAssistantUUID")
        target = entries.rows.get(source) if isinstance(source, str) else None
        if target is not None and source != row.get("parentUuid") and _same_agent(row, target):
            state.attach(target["uuid"], row)
        for call in result_ids(row):
            holder = state.owners.get(call)
            if (
                holder is not None
                and holder["uuid"] != row.get("parentUuid")
                and _same_agent(row, holder)
            ):
                state.attach(holder["uuid"], row)
    return state


def _recovered_results(state: _Recovery, siblings: list[Row]) -> list[Row]:
    keys = {row["uuid"] for row in siblings}
    direct: list[Row] = []
    indirect: list[Row] = []
    taken: set[str] = set()
    for sibling in siblings:
        for row in state.results.get(sibling["uuid"], []):
            if row["uuid"] in state.seen or row["uuid"] in taken:
                continue
            taken.add(row["uuid"])
            (direct if row.get("parentUuid") in keys else indirect).append(row)
    if not indirect:
        return direct
    known = {key for row in direct for key in result_ids(row)}
    top = max(state.entries.position(row) for row in siblings)
    for row in sorted(indirect, key=state.rank):
        ids = result_ids(row)
        if not any(
            key not in known
            and key not in state.path_results
            and (owner := state.owners.get(key)) is not None
            and owner["uuid"] in keys
            for key in ids
        ):
            continue
        known.update(ids)
        state.ranks[row["uuid"]] = max(state.entries.position(row), top + 0.5)
        direct.append(row)
    return direct


def _span(state: _Recovery, assistant: Row, message: str) -> Span | None:
    siblings = state.by_message.get(message, [assistant])
    found = [row for row in siblings if row["uuid"] not in state.seen]
    found += _recovered_results(state, siblings)
    state.seen.update(row["uuid"] for row in found)
    places = state.places
    start = places[assistant["uuid"]]
    end = max([start + 1, *(places[row["uuid"]] + 1 for row in siblings if row["uuid"] in places)])
    while end < len(state.path) and _continues(state.path[end], message):
        end += 1
    anchored: dict[str, list[Row]] = {}
    if any(call_ids(row) for row in siblings):
        roots = list(siblings)
        rooted = {row["uuid"] for row in roots}
        for sibling in siblings:
            for row in state.results.get(sibling["uuid"], []):
                if row["uuid"] in state.seen and row["uuid"] not in rooted:
                    rooted.add(row["uuid"])
                    roots.append(row)
        for row in roots:
            state.tails(row, found)
        for row in state.path[start + 1 : end]:
            if _context_only(row):
                extra: list[Row] = []
                state.tails(row, extra)
                if extra:
                    anchored[row["uuid"]] = extra
    if not found and not anchored:
        return None
    return start, end, sorted(found, key=state.rank), anchored


def recover(entries: Entries, path: list[Row]) -> list[Row]:
    if not any(row["type"] == "assistant" for row in path):
        return path
    state = _prepare(entries, path)
    spans: list[Span] = []
    messages: set[str] = set()
    for assistant in [row for row in path if row["type"] == "assistant"]:
        message = message_id(assistant)
        if message is None or message in messages:
            continue
        messages.add(message)
        span = _span(state, assistant, message)
        if span is not None:
            spans.append(span)
    merged: list[Span] = []
    for start, end, found, anchored in sorted(spans, key=lambda item: item[0]):
        if merged and start < merged[-1][1]:
            previous = merged[-1]
            joined = {**previous[3]}
            for key, rows in anchored.items():
                joined[key] = [*joined.get(key, []), *rows]
            merged[-1] = (
                previous[0],
                max(previous[1], end),
                sorted([*previous[2], *found], key=state.rank),
                joined,
            )
        else:
            merged.append((start, end, found, anchored))
    result: list[Row] = []
    cursor = 0
    for start, end, found, anchored in merged:
        result += path[cursor : start + 1]
        pending = list(found)
        for row in path[start + 1 : end]:
            while pending and state.rank(pending[0])[0] < entries.position(row):
                result.append(pending.pop(0))
            result += [row, *anchored.get(row["uuid"], [])]
        result += pending
        cursor = end
    return result + path[cursor:]


def synthetic_error(row: Row) -> bool:
    message = row.get("message")
    return (
        row["type"] == "assistant"
        and row.get("isApiErrorMessage") is True
        and isinstance(message, dict)
        and message.get("model") == "<synthetic>"
    )


def _companion(row: Row) -> bool:
    return row.get("turnCompanion") is True or isinstance(row.get("sourceToolUseID"), str)


def _trailing(path: list[Row], unresolved: set[str]) -> set[str]:
    found: set[str] = set()
    answered = False
    for row in reversed(path):
        if row["type"] in ("system", "attachment"):
            continue
        if row["type"] == "user":
            if (
                result_ids(row)
                or row.get("interruptedByShutdown") is True
                or (not answered and _companion(row))
            ):
                continue
            break
        answered = True
        found.update(key for key in call_ids(row) if key in unresolved)
    return found


def _last_response(path: list[Row], trailing: set[str]) -> list[tuple[Row, str]]:
    seen: set[str | None] = set()
    current: str | None = None
    calls: list[tuple[Row, str]] = []
    turn: dict[str, bool] | None = {"result": False, "classified": False}
    for row in path:
        if row["type"] == "assistant" and not row.get("isVirtual") and not synthetic_error(row):
            if turn is not None and not turn["classified"]:
                if not turn["result"]:
                    seen.clear()
                turn["classified"] = True
            key = message_id(row)
            if key in seen:
                if key == current:
                    calls += [(row, call) for call in call_ids(row) if call in trailing]
                continue
            seen.add(key)
            current = key
            calls = [(row, call) for call in call_ids(row) if call in trailing]
            turn = None
        elif (row["type"] == "user" and not row.get("isVirtual")) or (
            row["type"] == "system" and row.get("subtype") == "local_command"
        ):
            turn = turn or {"result": False, "classified": False}
            turn["result"] = turn["result"] or bool(result_ids(row))
    return calls


def resolved(path: list[Row]) -> tuple[list[Row], list[tuple[Row, str]]]:
    calls = {key for row in path for key in call_ids(row)}
    unresolved = calls - {key for row in path for key in result_ids(row)}
    if not unresolved:
        return path, []
    trailing = _trailing(path, unresolved)
    dropped = unresolved - trailing
    kept = [
        row for row in path if not (ids := call_ids(row)) or not all(key in dropped for key in ids)
    ]
    return kept, _last_response(kept, trailing)


def relinked(entries: Entries, boundary: Row) -> Entries | None:
    retained = preserved(boundary, entries.rows)
    if retained is None or not retained[1] or any(key not in entries.rows for key in retained[1]):
        return None
    anchor, keys = retained
    rows = dict(entries.rows)
    previous = anchor
    for key in keys:
        rows[key] = {**rows[key], "parentUuid": previous}
        previous = key
    for key, row in list(rows.items()):
        if row.get("parentUuid") == anchor and key != keys[0]:
            rows[key] = {**row, "parentUuid": keys[-1]}
    limit = entries.position(boundary)
    kept = set(keys)
    dropped = {key for key in rows if entries.positions[key] < limit and key not in kept}
    rows = {
        key: {**row, "parentUuid": keys[-1]}
        if row["type"] in ("user", "assistant") and row.get("parentUuid") in dropped
        else row
        for key, row in rows.items()
        if key not in dropped
    }
    return Entries(rows, entries.aliases, {key: entries.positions[key] for key in rows})


def context(entries: Entries, leaf: str, boundary: Row, chain: list[Row]) -> list[Row]:
    native = relinked(entries, boundary)
    if native is None:
        return chain[next(i for i, row in enumerate(chain) if row is boundary) :]
    if leaf not in native.rows:
        raise PreservationError("Claude compaction removed the selected branch from context")
    path = walk(native, leaf, logical=False)
    if path[0]["uuid"] != boundary["uuid"]:
        raise PreservationError("Claude compacted context does not start at its boundary")
    return recover(native, path)
