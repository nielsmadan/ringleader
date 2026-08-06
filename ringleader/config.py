from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import config_path


@dataclass
class Config:
    backend: str | None = None
    model: str | None = None
    api_provider: str = "anthropic"


def load_config(path: Path | None = None) -> Config:
    p = Path(path) if path is not None else config_path()
    if not p.exists():
        return Config()
    data = json.loads(p.read_text(encoding="utf-8"))
    return Config(
        backend=data.get("backend"),
        model=data.get("model"),
        api_provider=data.get("api_provider", "anthropic"),
    )


def save_config(cfg: Config, path: Path | None = None) -> None:
    p = Path(path) if path is not None else config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), indent=2) + "\n", encoding="utf-8")
