"""The sandbox fixture is committed to a public repo, so its contents are a standing
disclosure risk. These tests are the guard: they fail if a refreeze ever ships real
paths, addresses or credential-shaped strings.
"""

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "sandbox"

_spec = importlib.util.spec_from_file_location("sandbox", REPO / "scripts" / "sandbox.py")
sandbox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sandbox)


def _files():
    return sorted(FIXTURE.rglob("*.jsonl"))


def test_fixture_exists_and_covers_both_sources():
    claude = [f for f in _files() if "claude" in f.parts]
    codex = [f for f in _files() if "codex" in f.parts]
    assert claude, "no claude sessions in the fixture"
    assert codex, "no codex sessions in the fixture"


def test_fixture_leaks_nothing():
    problems = sandbox.verify(FIXTURE)
    assert not problems, "fixture contains data that must not be public:\n" + "\n".join(problems)


def test_fixture_stays_small():
    """It is committed, so it has to stay a rounding error in the repo."""
    total = sum(f.stat().st_size for f in _files())
    assert total < 512 * 1024, f"fixture grew to {total // 1024} KB"


def test_fixture_spans_the_prune_gate():
    """Sessions either side of prune's --max-turns default, or the sandbox can't
    exercise the tiny-session gate at all."""
    counts = []
    for f in _files():
        n = sum(
            1
            for line in f.read_text().splitlines()
            if json.loads(line).get("type") in ("user", "assistant", "response_item")
        )
        counts.append(n)
    assert min(counts) < 12 < max(counts), f"turn counts do not span the gate: {sorted(counts)}"
