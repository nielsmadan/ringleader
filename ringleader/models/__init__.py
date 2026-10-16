from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..progress import CancelCheck
from .base import CallStats, Model, ModelError, ModelTask
from .claude_cli import ClaudeCliModel
from .codex_cli import CodexCliModel
from .llm import LlmModel

if TYPE_CHECKING:
    from ..config import Config

ModelFactory = Callable[..., Model]

# agent-CLI auto-pick order
MODELS: list[ModelFactory] = [ClaudeCliModel, CodexCliModel]

ENRICH_DEFAULT_MODELS: dict[str, str | None] = {"claude": "haiku", "codex": None, "llm": None}
CALL_TIMEOUT_SECONDS: dict[ModelTask, float] = {"enrich": 120.0, "harvest": 300.0}

__all__ = [
    "CALL_TIMEOUT_SECONDS",
    "ENRICH_DEFAULT_MODELS",
    "MODELS",
    "CallStats",
    "ClaudeCliModel",
    "CodexCliModel",
    "LlmModel",
    "Model",
    "ModelError",
    "ModelTask",
    "active_model",
    "describe",
    "model_name_for",
    "with_cancel",
]


def model_name_for(cfg: Config, engine: str, task: ModelTask) -> str | None:
    if task == "harvest":
        return cfg.model or None
    return cfg.enrich_model or ENRICH_DEFAULT_MODELS.get(engine) or cfg.model or None


def active_model(cfg: Config, task: ModelTask = "harvest") -> Model | None:
    engine = getattr(cfg, "ai", None)
    if engine == "llm":
        llm_model = LlmModel(
            provider=getattr(cfg, "llm_provider", "anthropic"),
            model=model_name_for(cfg, engine, task),
        )
        return llm_model if llm_model.available() else None
    timeout = CALL_TIMEOUT_SECONDS[task]
    if engine in ("claude", "codex"):
        factories: dict[str, ModelFactory] = {
            "claude": ClaudeCliModel,
            "codex": CodexCliModel,
        }
        cli_model = factories[engine](model=model_name_for(cfg, engine, task), timeout=timeout)
        return cli_model if cli_model.available() else None
    # unset -> auto-pick first available agent CLI
    for cls in MODELS:
        candidate = cls(model=model_name_for(cfg, cls().name, task), timeout=timeout)
        if candidate.available():
            return candidate
    return None


def describe(model: Model) -> str:
    return f"{model.name} ({getattr(model, 'model', None) or 'default model'})"


def with_cancel(model: Model, cancelled: CancelCheck) -> Model:
    if isinstance(model, ClaudeCliModel | CodexCliModel):
        return dataclasses.replace(model, cancelled=cancelled)
    return model
