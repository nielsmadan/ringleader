from __future__ import annotations

import importlib
import json
from dataclasses import dataclass

from .base import ModelError


@dataclass
class LlmModel:
    name: str = "llm"
    provider: str = "anthropic"
    model: str | None = None

    def _llm(self):
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
        except Exception:
            return False

    def complete_json(self, prompt: str, schema: dict) -> dict:
        llm = self._llm()
        try:
            model = llm.get_model(self.model or self.provider)
            resp = model.prompt(prompt, schema=schema)
            return json.loads(resp.text())
        except Exception as e:
            raise ModelError(f"the llm engine failed: {e}") from e
