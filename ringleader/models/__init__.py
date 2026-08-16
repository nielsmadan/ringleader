from __future__ import annotations

from .base import Model, ModelError
from .claude_cli import ClaudeCliModel
from .codex_cli import CodexCliModel
from .llm import LlmModel

# agent-CLI auto-pick order
MODELS = [ClaudeCliModel, CodexCliModel]

__all__ = [
    "MODELS",
    "ClaudeCliModel",
    "CodexCliModel",
    "LlmModel",
    "Model",
    "ModelError",
    "active_model",
]


def active_model(cfg) -> Model | None:
    engine = getattr(cfg, "ai", None)
    model_name = getattr(cfg, "model", None) or None
    if engine == "llm":
        m = LlmModel(provider=getattr(cfg, "llm_provider", "anthropic"), model=model_name)
        return m if m.available() else None
    if engine in ("claude", "codex"):
        m = {"claude": ClaudeCliModel, "codex": CodexCliModel}[engine](model=model_name)
        return m if m.available() else None
    # unset -> auto-pick first available agent CLI
    for cls in MODELS:
        m = cls(model=model_name)
        if m.available():
            return m
    return None
