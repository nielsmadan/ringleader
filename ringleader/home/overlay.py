from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from .. import registry
from ..decisions import Op, Outcome, identities
from ..revalidate import Identity

type HiddenState = Literal["queued", "running", "applied"]


@dataclass(frozen=True)
class Hidden:
    ticket: int
    state: HiddenState
    applied_generation: int | None = None


type Overlay = Mapping[Identity, Hidden]


def submitted(overlay: Overlay, tickets: Sequence[tuple[int, Op]]) -> dict[Identity, Hidden]:
    added = {key: Hidden(ticket, "queued") for ticket, op in tickets for key in identities(op)}
    return {**overlay, **added}


def held(overlay: Overlay, identity: Identity, ticket: int) -> dict[Identity, Hidden]:
    return {**overlay, identity: Hidden(ticket, "queued")}


def started(overlay: Overlay, ticket: int) -> dict[Identity, Hidden]:
    return {
        key: replace(hidden, state="running") if hidden.ticket == ticket else hidden
        for key, hidden in overlay.items()
    }


def finished(
    overlay: Overlay, ticket: int, op: Op, outcome: Outcome, generation: int
) -> dict[Identity, Hidden]:
    if not outcome.ok and not outcome.wrote:
        return removed(overlay, (ticket,))
    covered = set(identities(op))
    refused = {seen.identity for seen, _ in outcome.refused}
    mine = {key for key, hidden in overlay.items() if hidden.ticket == ticket and key in covered}
    return {
        key: Hidden(ticket, "applied", generation) if key in mine else hidden
        for key, hidden in overlay.items()
        if key not in mine or key not in refused
    }


def removed(overlay: Overlay, tickets: Collection[int]) -> dict[Identity, Hidden]:
    return {key: hidden for key, hidden in overlay.items() if hidden.ticket not in tickets}


def adopted(overlay: Overlay, snapshot_generation: int) -> dict[Identity, Hidden]:
    return {
        key: hidden
        for key, hidden in overlay.items()
        if hidden.state != "applied"
        or hidden.applied_generation is None
        or hidden.applied_generation > snapshot_generation
    }


def visible(reg: dict[str, Any], overlay: Overlay) -> dict[str, Any]:
    return {**reg, "threads": [t for t in reg["threads"] if registry.identity(t) not in overlay]}
