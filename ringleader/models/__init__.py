from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .base import Model, ModelError
from .claude_cli import ClaudeCliModel
from .codex_cli import CodexCliModel
from .llm import LlmModel

if TYPE_CHECKING:
    from ..config import Config

ModelFactory = Callable[..., Model]

# agent-CLI auto-pick order
MODELS: list[ModelFactory] = [ClaudeCliModel, CodexCliModel]

__all__ = [
    "MODELS",
    "ClaudeCliModel",
    "CodexCliModel",
    "LlmModel",
    "Model",
    "ModelError",
    "active_model",
]


def active_model(cfg: Config) -> Model | None:
    engine = getattr(cfg, "ai", None)
    model_name = getattr(cfg, "model", None) or None
    if engine == "llm":
        llm_model = LlmModel(provider=getattr(cfg, "llm_provider", "anthropic"), model=model_name)
        return llm_model if llm_model.available() else None
    if engine in ("claude", "codex"):
        factories: dict[str, ModelFactory] = {
            "claude": ClaudeCliModel,
            "codex": CodexCliModel,
        }
        cli_model = factories[engine](model=model_name)
        return cli_model if cli_model.available() else None
    # unset -> auto-pick first available agent CLI
    for cls in MODELS:
        candidate = cls(model=model_name)
        if candidate.available():
            return candidate
    return None
