from __future__ import annotations

import io
from collections.abc import Buffer, Iterable
from pathlib import Path
from typing import IO, BinaryIO, Literal

import zstandard

_ZSTD_SUFFIX = ".zst"
_TRANSCRIPT_SUFFIX = ".jsonl"


def is_zstd(path: str | Path) -> bool:
    return str(path).endswith(_ZSTD_SUFFIX)


def uncompressed_path(path: str | Path) -> Path:
    path = Path(path)
    return path.with_name(path.name.removesuffix(_ZSTD_SUFFIX))


def is_transcript(path: str | Path) -> bool:
    return uncompressed_path(path).suffix == _TRANSCRIPT_SUFFIX


def other_form(path: str | Path) -> Path | None:
    """The same transcript under its other name: `x.jsonl` ↔ `x.jsonl.zst`."""
    path = Path(path)
    if not is_transcript(path):
        return None
    return uncompressed_path(path) if is_zstd(path) else path.with_name(path.name + _ZSTD_SUFFIX)


def moved_form(path: str | Path) -> Path | None:
    """The other form, when the recorded transcript is gone and that is what's there."""
    path = Path(path)
    if path.exists():
        return None
    alternate = other_form(path)
    return alternate if alternate is not None and alternate.exists() else None


def prefer_plain(paths: Iterable[Path]) -> list[Path]:
    """Codex compresses an idle rollout in place and decompresses it again on resume;
    while both copies exist, the plain one is current."""
    found = set(paths)
    names = {str(p) for p in found}
    return sorted(
        p for p in found if not (is_zstd(p) and str(p).removesuffix(_ZSTD_SUFFIX) in names)
    )


class _Decoded(io.RawIOBase):
    """Reports a corrupt stream as the OSError every transcript reader already handles."""

    def __init__(self, reader: BinaryIO) -> None:
        self._reader = reader

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer).cast("B")
        try:
            data = self._reader.read(len(view))
        except zstandard.ZstdError as e:
            raise OSError(f"corrupt zstd stream: {e}") from e
        view[: len(data)] = data
        return len(data)

    def close(self) -> None:
        if not self.closed:
            self._reader.close()
        super().close()


def open_text(path: str | Path, *, errors: Literal["strict", "replace"] = "strict") -> IO[str]:
    """A `.zst` file decompressed on the fly, any other file opened as UTF-8 text."""
    path = Path(path)
    if not is_zstd(path):
        return path.open(encoding="utf-8", errors=errors)
    raw = path.open("rb")
    try:
        reader = zstandard.ZstdDecompressor().stream_reader(
            raw, read_across_frames=True, closefd=True
        )
    except BaseException:
        raw.close()
        raise
    return io.TextIOWrapper(io.BufferedReader(_Decoded(reader)), encoding="utf-8", errors=errors)


def decompress(data: bytes) -> bytes:
    with _Decoded(
        zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True)
    ) as reader:
        return reader.read() or b""
