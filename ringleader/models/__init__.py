from __future__ import annotations

from .api import ApiModel
from .base import Model, ModelError
from .claude_cli import ClaudeCliModel
from .codex_cli import CodexCliModel

# agent-CLI auto-pick order
MODELS = [ClaudeCliModel, CodexCliModel]

__all__ = [
    "MODELS",
    "ApiModel",
    "ClaudeCliModel",
    "CodexCliModel",
    "Model",
    "ModelError",
    "active_model",
]


def active_model(cfg) -> Model | None:
    backend = getattr(cfg, "backend", None)
    model_name = getattr(cfg, "model", None) or None
    if backend == "api":
        m = ApiModel(provider=getattr(cfg, "api_provider", "anthropic"), model=model_name)
        return m if m.available() else None
    if backend in ("claude", "codex"):
        m = {"claude": ClaudeCliModel, "codex": CodexCliModel}[backend](model=model_name)
        return m if m.available() else None
    # unset -> auto-pick first available agent backend
    for cls in MODELS:
        m = cls(model=model_name)
        if m.available():
            return m
    return None
