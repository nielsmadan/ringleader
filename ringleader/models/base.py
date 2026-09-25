from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class ModelError(Exception):
    pass


@runtime_checkable
class Model(Protocol):
    name: str

    def available(self) -> bool: ...
    def complete_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...
