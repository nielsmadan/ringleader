from pathlib import Path

import pytest

LIVE_REGISTRY = Path(__file__).resolve().parent.parent / "registry.json"


@pytest.fixture(autouse=True, scope="session")
def guard_live_registry():
    # the rl commands default to the repo's real registry, so one missing --registry
    # in a test silently overwrites the user's live cache
    before = LIVE_REGISTRY.read_bytes() if LIVE_REGISTRY.exists() else None
    yield
    after = LIVE_REGISTRY.read_bytes() if LIVE_REGISTRY.exists() else None
    if after == before:
        return
    if before is None:
        LIVE_REGISTRY.unlink()
    else:
        LIVE_REGISTRY.write_bytes(before)
    pytest.fail(
        "a test wrote to the live registry.json (restored) — "
        "pass --registry <tmp path> to every rl invocation"
    )
