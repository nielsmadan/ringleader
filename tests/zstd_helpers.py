import os
from pathlib import Path

import zstandard


def compress_like_codex(plain: Path, *, keep_plain: bool = False, frames: int = 1) -> Path:
    """Replace a rollout with its `.zst`, keeping the mtime, as Codex does to idle rollouts.
    `frames` splits the data across that many concatenated zstd frames."""
    data = plain.read_bytes()
    step = max(1, -(-len(data) // frames))
    compressor = zstandard.ZstdCompressor()
    packed = plain.with_name(plain.name + ".zst")
    pieces = [data[i : i + step] for i in range(0, len(data), step)] or [b""]
    packed.write_bytes(b"".join(compressor.compress(piece) for piece in pieces))
    st = plain.stat()
    os.utime(packed, ns=(st.st_atime_ns, st.st_mtime_ns))
    if not keep_plain:
        plain.unlink()
    return packed
