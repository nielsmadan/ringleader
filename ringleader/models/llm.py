from __future__ import annotations

import importlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any, cast

from ..progress import CancelCheck
from .base import ModelError

POLL_SECONDS = 0.1


@dataclass
class _Slot:
    value: dict[str, Any] | None = None
    error: Exception | None = None


def _watched(call: Callable[[], dict[str, Any]], cancelled: CancelCheck) -> dict[str, Any]:
    slot = _Slot()
    done = threading.Event()

    def run() -> None:
        try:
            slot.value = call()
        except Exception as error:  # noqa: BLE001 -- re-raised on the calling thread below
            slot.error = error
        finally:
            done.set()

    threading.Thread(target=run, name="rl-llm-call", daemon=True).start()
    while not done.wait(POLL_SECONDS):
        if cancelled():
            raise ModelError("the llm engine call was cancelled")
    if slot.error is not None:
        raise slot.error
    if slot.value is None:
        raise ModelError("the llm engine returned nothing")
    return slot.value


@dataclass
class LlmModel:
    name: str = "llm"
    provider: str = "anthropic"
    model: str | None = None
    cancelled: CancelCheck | None = None

    def _llm(self) -> ModuleType:
        try:
            return importlib.import_module("llm")
        except ImportError as e:
            raise ModelError("the llm engine needs `pip install llm`") from e

    def available(self) -> bool:
        try:
            llm = self._llm()
        except ModelError:
            return False
        try:
            llm.get_model(self.model or self.provider)  # resolves the model id
            return True
        except Exception:  # noqa: BLE001 -- any engine failure means unavailable
            return False

    def complete_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if self.cancelled is None:
            return self._complete(prompt, schema)
        return _watched(lambda: self._complete(prompt, schema), self.cancelled)

    def _complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        llm = self._llm()
        try:
            model = llm.get_model(self.model or self.provider)
            resp = model.prompt(prompt, schema=schema)
            return cast(dict[str, Any], json.loads(resp.text()))
        except Exception as e:
            raise ModelError(f"the llm engine failed: {e}") from e
