from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

type ModelTask = Literal["enrich", "harvest", "analyze"]


class ModelError(Exception):
    pass


@dataclass(frozen=True)
class CallStats:
    seconds: float
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


@runtime_checkable
class Model(Protocol):
    name: str
    model: str | None

    def available(self) -> bool: ...
    def complete_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...
