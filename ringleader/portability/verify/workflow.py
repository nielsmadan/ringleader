from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from ...agents import display_agent
from ..capabilities import AdapterRegistry, Harness
from ..model import PreservationError
from ..verification import (
    HARNESS_VERSION,
    TIERS,
    VerificationRecord,
    harness_inputs,
    record_verified,
)

EVIDENCE_KEPT = 3
PASSED = "✓"
FAILED = "✖"

Report = Callable[[str], None]


class NotInstalled(PreservationError):
    def __init__(self, executable: str) -> None:
        super().__init__(f"{executable} is not installed; cannot self-verify it")
        self.executable = executable


def self_verify(
    agent: str, harness: Harness, executable: str, *, workdir: Path
) -> VerificationRecord:
    if agent not in HARNESS_VERSION:
        raise PreservationError(f"No self-verification harness for {agent}")
    try:
        result = harness(executable, workdir.resolve())
    except PreservationError:
        raise
    except (
        OSError,
        ValueError,
        LookupError,
        TypeError,
        AttributeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        raise PreservationError(f"self-verification failed: {type(exc).__name__}: {exc}") from exc
    return VerificationRecord(
        agent,
        result.version,
        HARNESS_VERSION[agent],
        TIERS[agent],
        datetime.now(UTC).isoformat(timespec="seconds"),
        result.schema_identity,
        harness_inputs(),
    )


def stale_evidence(names: list[str], agent: str, keep: int) -> list[str]:
    mine = sorted(name for name in names if name.startswith(f"{agent}-"))
    return mine[: max(len(mine) - keep, 0)]


def preserve_evidence(verified: Path, agent: str, workdir: Path) -> Path | None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = verified.parent / "verify-failures"
    destination = root / f"{agent}-{stamp}"
    try:
        shutil.copytree(workdir, destination, symlinks=True)
    except OSError:
        return None
    with suppress(OSError):
        for name in stale_evidence([item.name for item in root.iterdir()], agent, EVIDENCE_KEPT):
            shutil.rmtree(root / name)
    return destination


def verify_and_record(
    adapters: AdapterRegistry, agent: str, verified: Path, report: Report
) -> VerificationRecord:
    verifier = adapters.verifier(agent)
    name = display_agent(agent)
    executable = shutil.which(verifier.executable)
    if executable is None:
        raise NotInstalled(verifier.executable)
    report(f"verifying {name} with {executable} (offline, about a minute)...")
    with TemporaryDirectory(prefix=f"rl-verify-{agent}-") as workdir:
        try:
            record = self_verify(agent, verifier.harness, executable, workdir=Path(workdir))
        except PreservationError as exc:
            evidence = preserve_evidence(verified, agent, Path(workdir))
            suffix = f" (evidence kept at {evidence})" if evidence else ""
            report(f"{FAILED} {name}: {exc}{suffix}")
            raise
    try:
        record_verified(verified, record)
    except PreservationError as exc:
        report(
            f"{FAILED} {name} {record.version}: verified ({record.tier}) but "
            f"failed to record in {verified}: {exc}"
        )
        raise
    report(f"{PASSED} {name} {record.version}: verified ({record.tier}); recorded in {verified}")
    return record
