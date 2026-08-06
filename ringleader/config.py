from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Config:
    backend: str | None = None
    model: str | None = None
    api_provider: str = "anthropic"
