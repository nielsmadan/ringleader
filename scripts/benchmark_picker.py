from __future__ import annotations

import asyncio
import json
import platform
import statistics
import tempfile
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

from ringleader.picker.app import PickerApp, PickerScreen
from ringleader.resume import Scope

MATCH_COUNT = 1322
SESSION_COUNT = 1834


async def benchmark(path: Path) -> None:
    app = PickerApp(
        path,
        "/benchmark",
        scope=Scope.ALL,
        refresher=lambda *_: (json.loads(path.read_text()), None),
        presence=frozenset,
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PickerScreen)
        screen._query = "session"
        await screen._render_rows()
        samples = []
        for _ in range(3):
            start = perf_counter()
            await screen._render_rows()
            samples.append(perf_counter() - start)
        print(
            json.dumps(
                {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "textual": version("textual"),
                    "threads": len(screen._reg["threads"]),
                    "matches": len(screen._rows),
                    "seconds": samples,
                    "median_seconds": statistics.median(samples),
                },
                indent=2,
            )
        )


def main() -> None:
    threads = [
        {
            "sessionId": f"row-{index}",
            "source": "claude-code",
            "project": "/benchmark",
            "title": f"{'session' if index < MATCH_COUNT else 'work'} {index}",
            "lastPrompt": "Plan next task",
            "mtime": "2026-10-12T00:00:00+00:00",
        }
        for index in range(SESSION_COUNT)
    ]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "registry.json"
        path.write_text(json.dumps({"threads": threads}))
        asyncio.run(benchmark(path))


if __name__ == "__main__":
    main()
