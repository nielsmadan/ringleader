from __future__ import annotations

import os
import select
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from ringleader.devtools.isolation import agent_store_env
from ringleader.memory_sources.base import NativePaths
from ringleader.memory_sources.transactions import NativeBusy

from memory_helpers import codex_world


def check_lock(root: Path, rust_probe: Path, *, version: str) -> None:
    world, workspace, _ = codex_world(root, version=version)
    source = replace(world.source, paths=NativePaths(root))
    lock = workspace.with_suffix(".lock")
    ref = next(record.ref for record in source.scan().records if record.ref == world.ref)
    with source.mutation(ref):
        identity = lock.stat()
        blocked = subprocess.run([rust_probe, lock], capture_output=True, check=False, timeout=5)
        if blocked.returncode != 35:
            raise RuntimeError(
                f"Rust did not report contention: {blocked.returncode}, {blocked.stderr!r}"
            )

    with subprocess.Popen(
        [rust_probe, lock, "hold"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    ) as rust:
        try:
            if rust.stdout is None or not select.select([rust.stdout], [], [], 5)[0]:
                raise RuntimeError("Rust lock probe timed out waiting for readiness")
            if rust.stdout.readline() != b"locked\n":
                raise RuntimeError("Rust lock probe did not acquire the lock")
            try:
                with source.mutation(ref):
                    raise RuntimeError("Adapter acquired a Rust-held native lock")
            except NativeBusy:
                pass
        finally:
            try:
                rust.communicate(b"\n", timeout=5)
            except subprocess.TimeoutExpired:
                rust.kill()
                rust.communicate(timeout=5)
                raise
        if rust.returncode != 0:
            raise RuntimeError(f"Rust lock probe exited with status {rust.returncode}")

    with source.mutation(ref):
        if not os.path.samestat(identity, lock.stat()):
            raise RuntimeError("Native workspace lock inode changed")
    acquires = subprocess.run([rust_probe, lock], capture_output=True, check=True, timeout=5)
    if acquires.stdout != b"locked\n":
        raise RuntimeError("Rust lock probe did not reacquire the lock")
    print(f"{version}: Rust and the native adapter exclude each other and reuse the lock inode")


def main() -> None:
    with TemporaryDirectory(prefix="ringleader-native-lock-") as temp:
        root = Path(temp).resolve()
        os.environ.update(agent_store_env(root))
        os.environ["XDG_DATA_HOME"] = str(root / "data")
        os.environ["XDG_STATE_HOME"] = str(root / "state")
        os.environ["TMPDIR"] = str(root)
        helper = Path(__file__).with_name("probe_codex_memory_lock.rs")
        binary = root / "rust-lock"
        subprocess.run(
            ["rustc", "--edition=2024", "-D", "warnings", helper, "-o", binary],
            check=True,
            timeout=60,
        )
        for version in ("v1", "v2"):
            fixture = root / version
            fixture.mkdir()
            check_lock(fixture, binary, version=version)
        compiler = subprocess.check_output(["rustc", "--version"], text=True, timeout=5)
        print(f"PASS on {sys.platform} with {compiler.strip()}")


if __name__ == "__main__":
    main()
