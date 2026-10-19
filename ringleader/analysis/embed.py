from __future__ import annotations

import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

EMBED_MODEL = "minishlab/potion-base-8M"
OFFLINE_ENV = "HF_HUB_OFFLINE"
HF_CONSTANTS = "huggingface_hub.constants"
NOT_INSTALLED = "extra not installed — reinstall rl with it: uv tool install --reinstall '.[embed]'"
NOT_DOWNLOADED = "model not downloaded — run rl analyze issues --download-embed-model"


class Embedder(Protocol):
    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class EmbedLoad:
    embedder: Embedder | None
    reason: str | None


@dataclass(frozen=True)
class _StaticEmbedder:
    model: Any

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = self.model.encode(list(texts)).tolist()
        return vectors


def _set_hub_constant(value: bool) -> None:
    constants = sys.modules.get(HF_CONSTANTS)
    if constants is not None:
        setattr(constants, "HF_HUB_OFFLINE", value)  # noqa: B010 -- attribute is absent from the stub


@contextmanager
def _offline(enabled: bool) -> Iterator[None]:
    previous = os.environ.get(OFFLINE_ENV)
    if enabled:
        os.environ[OFFLINE_ENV] = "1"
    else:
        os.environ.pop(OFFLINE_ENV, None)
    _set_hub_constant(enabled)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(OFFLINE_ENV, None)
        else:
            os.environ[OFFLINE_ENV] = previous
        _set_hub_constant(previous is not None and previous.lower() in {"1", "true", "yes", "on"})


def load_embedder(*, allow_download: bool) -> EmbedLoad:
    with _offline(not allow_download):
        try:
            from model2vec import StaticModel  # noqa: PLC0415 -- optional extra, imported lazily
        except ImportError:
            return EmbedLoad(None, NOT_INSTALLED)
        except Exception as error:  # noqa: BLE001 -- a broken install degrades to exact signatures
            return EmbedLoad(None, f"embed model failed to load: {error}")
        _set_hub_constant(not allow_download)
        try:
            model = StaticModel.from_pretrained(EMBED_MODEL)
        except FileNotFoundError as error:
            reason = f"embed model failed to load: {error}" if allow_download else NOT_DOWNLOADED
            return EmbedLoad(None, reason)
        except Exception as error:  # noqa: BLE001 -- any load failure degrades to exact signatures
            return EmbedLoad(None, f"embed model failed to load: {error}")
    return EmbedLoad(_StaticEmbedder(model), None)
