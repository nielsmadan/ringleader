from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Final, Literal, get_args

from .paths import config_path

type KeyPreset = Literal["vim", "circus"]
KEY_PRESETS: Final[tuple[KeyPreset, ...]] = get_args(KeyPreset.__value__)
MAX_USAGE_CACHE_SECONDS = 3600


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
    memory_recovery_retention_days: int = 30
    memory_codex_sqlite_home: str | None = None
    memory_claude_trust_file: str | None = None
    session_filters: list[str] = field(default_factory=list)
    inbox_keys: str = "vim"
    usage_claude_auth: str = "env"
    usage_codex_auth: str = "disabled"
    usage_cache_seconds: int = 60


USAGE_DEFAULTS: Final[dict[str, str | int]] = {
    "usage.claude_auth": Config.usage_claude_auth,
    "usage.codex_auth": Config.usage_codex_auth,
    "usage.cache_seconds": Config.usage_cache_seconds,
}


def usage_preference(key: str, value: object) -> str | int:
    if key == "usage.cache_seconds":
        if type(value) is int and 0 <= value <= MAX_USAGE_CACHE_SECONDS:
            return value
        raise ValueError("usage.cache_seconds must be an integer from 0 to 3600")
    choices = {
        "usage.claude_auth": ("env", "agent"),
        "usage.codex_auth": ("disabled", "agent"),
    }
    if key not in choices:
        raise ValueError("unknown usage preference")
    if isinstance(value, str) and value in choices[key]:
        return value
    raise ValueError(f"{key} must be one of {', '.join(choices[key])}")


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
        memory_recovery_retention_days=data.get("memory_recovery_retention_days", 30),
        memory_codex_sqlite_home=data.get("memory_codex_sqlite_home"),
        memory_claude_trust_file=data.get("memory_claude_trust_file"),
        session_filters=list(data.get("session_filters", [])),
        inbox_keys=data.get("inbox_keys", "vim"),
        usage_claude_auth=data.get("usage_claude_auth", Config.usage_claude_auth),
        usage_codex_auth=data.get("usage_codex_auth", Config.usage_codex_auth),
        usage_cache_seconds=data.get("usage_cache_seconds", Config.usage_cache_seconds),
    )


def save_config(cfg: Config, path: Path | None = None) -> None:
    p = Path(path) if path is not None else config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), indent=2) + "\n", encoding="utf-8")
