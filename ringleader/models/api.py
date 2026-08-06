from __future__ import annotations

import json
from dataclasses import dataclass

from .base import ModelError


@dataclass
class ApiModel:
    name: str = "api"
    provider: str = "anthropic"
    model: str | None = None

    def _llm(self):
        try:
            import llm  # noqa: PLC0415 -- optional extra, only imported when the api backend is used
        except ImportError as e:
            raise ModelError("the api backend needs `pip install ringleader[api]`") from e
        return llm

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
            raise ModelError(f"api backend failed: {e}") from e
