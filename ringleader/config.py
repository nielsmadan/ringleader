from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Final, Literal, get_args

from .paths import config_path

type KeyPreset = Literal["vim", "circus"]
KEY_PRESETS: Final[tuple[KeyPreset, ...]] = get_args(KeyPreset.__value__)


@dataclass
class Config:
    ai: str | None = None
    model: str | None = None
    ai_command_prefix: list[str] = field(default_factory=list)
    enrich_model: str | None = None
    analyze_model: str | None = None
    llm_provider: str = "anthropic"
    roundup_stale_hours: int = 8
    archive_retention_days: int = 0  # 0 = keep archived sessions forever
    session_filters: list[str] = field(default_factory=list)
    inbox_keys: str = "vim"


def key_preset(value: object) -> KeyPreset:
    preset = next((p for p in KEY_PRESETS if p == value), None)
    if preset is None:
        raise ValueError(f"inbox.keys is {value!r}; it must be one of {', '.join(KEY_PRESETS)}")
    return preset


def load_config(path: Path | None = None) -> Config:
    p = Path(path) if path is not None else config_path()
    if not p.exists():
        return Config()
    data = json.loads(p.read_text(encoding="utf-8"))
    ai = data.get("ai", data.get("backend"))  # back-compat: the key was once "backend"
    if ai == "api":  # back-compat: the "api" engine was renamed to "llm"
        ai = "llm"
    return Config(
        ai=ai,
        model=data.get("model"),
        ai_command_prefix=list(data.get("ai_command_prefix", [])),
        enrich_model=data.get("enrich_model"),
        analyze_model=data.get("analyze_model"),
        llm_provider=data.get("llm_provider", data.get("api_provider", "anthropic")),
        roundup_stale_hours=data.get("roundup_stale_hours", 8),
        archive_retention_days=data.get("archive_retention_days", 0),
        session_filters=list(data.get("session_filters", [])),
        inbox_keys=data.get("inbox_keys", "vim"),
    )


def save_config(cfg: Config, path: Path | None = None) -> None:
    p = Path(path) if path is not None else config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), indent=2) + "\n", encoding="utf-8")
