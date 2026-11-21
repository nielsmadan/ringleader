from __future__ import annotations

import argparse
import io
import json
import os
import platform
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path


def assistant(index: str, timestamp: str) -> str:
    return (
        json.dumps(
            {
                "type": "assistant",
                "timestamp": timestamp,
                "cwd": "/fixture",
                "requestId": index,
                "message": {
                    "id": index,
                    "model": "claude-opus-4-6",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 500,
                        "cache_creation_input_tokens": 50,
                    },
                },
            }
        )
        + "\n"
    )


def fixture(root: Path) -> tuple[list[Path], str]:
    root.mkdir(parents=True)
    timestamp = datetime.now(UTC).isoformat()
    filler = json.dumps({"type": "user", "message": {"content": "x" * 2048}}) + "\n"
    files = []
    for index in range(50):
        path = root / f"session-{index:03}.jsonl"
        with path.open("w") as handle:
            for row in range(2000):
                handle.write(assistant(f"{index}-{row}", timestamp) if row % 200 == 0 else filler)
        files.append(path)
    return files, timestamp


def run(code: Path, state: Path, profile: Path) -> tuple[float, dict]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(state / "home"),
        "XDG_DATA_HOME": str(state),
        "CLAUDE_CONFIG_DIR": str(profile),
        "PYTHONPATH": str(code),
        "TERM": "dumb",
        "NO_COLOR": "1",
    }
    started = time.perf_counter()
    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "ringleader",
            "usage",
            "local",
            "--source",
            "claude",
            "--json",
        ],
        env=environment,
        cwd=code,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return time.perf_counter() - started, json.loads(completed.stdout)


def measure(repo: Path, baseline: str, root: Path) -> dict:
    base_code = root / "baseline"
    base_code.mkdir()
    archive = subprocess.check_output(["git", "archive", baseline, "ringleader"], cwd=repo)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(base_code, filter="data")
    profile = root / "claude"
    files, timestamp = fixture(profile / "projects" / "fixture")
    original_bytes = sum(path.stat().st_size for path in files)
    _, expected = run(base_code, root / "base-state", profile)
    before = []
    for _ in range(3):
        duration, output = run(base_code, root / "base-state", profile)
        assert output == expected
        before.append(duration)
    cold, output = run(repo, root / "new-state", profile)
    assert output == expected
    after = []
    for _ in range(3):
        duration, output = run(repo, root / "new-state", profile)
        assert output == expected
        after.append(duration)
    with files[0].open("a") as handle:
        handle.write(assistant("appended", timestamp))
    incremental, output = run(repo, root / "new-state", profile)
    assert output["total_tokens"] == expected["total_tokens"] + 670
    return {
        "baseline": baseline,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "files": len(files),
        "bytes": original_bytes,
        "concurrency": 1,
        "warmup": 1,
        "repetitions": 3,
        "baseline_seconds": before,
        "cold_checkpoint_seconds": cold,
        "warm_checkpoint_seconds": after,
        "one_changed_file_seconds": incremental,
        "json_equal": True,
        "speedup": statistics.median(before) / statistics.median(after),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default="HEAD")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    baseline = subprocess.check_output(
        ["git", "rev-parse", args.baseline], cwd=repo, text=True
    ).strip()
    with tempfile.TemporaryDirectory(prefix="rl-usage-benchmark-") as temporary:
        result = measure(repo, baseline, Path(temporary))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
