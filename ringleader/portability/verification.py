from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from functools import cache
from pathlib import Path
from typing import Any, Literal

from ..stateio import atomic_write_bytes, registry_lock
from .model import PreservationError

Tier = Literal["model-request-capture", "cli-acceptance"]
HARNESS_VERSION: dict[str, int] = {
    "claude-code": 3,
    "codex": 4,
    "pi": 1,
    "opencode": 2,
    "droid": 2,
}
TIERS: dict[str, Tier] = {
    "claude-code": "model-request-capture",
    "codex": "model-request-capture",
    "pi": "model-request-capture",
    "opencode": "cli-acceptance",
    "droid": "model-request-capture",
}
STATE_VERSION = 1


@dataclass(frozen=True)
class VerificationRecord:
    agent: str
    version: str
    harness_version: int
    tier: Tier
    verified_at: str
    schema_identity: str | None = None
    inputs: str | None = None


@cache
def harness_inputs() -> str:
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def _decode(item: Any) -> VerificationRecord:
    names = {field.name for field in fields(VerificationRecord)}
    required = names - {"schema_identity", "inputs"}
    if (
        not isinstance(item, dict)
        or not required <= set(item)
        or not all(isinstance(item[key], str) for key in ("agent", "version", "verified_at"))
        or type(item["harness_version"]) is not int
        or item["tier"] not in TIERS.values()
        or not isinstance(item.get("schema_identity"), str | None)
        or not isinstance(item.get("inputs"), str | None)
    ):
        raise PreservationError("Invalid self-verification record")
    return VerificationRecord(**{key: item[key] for key in names if key in item})


def _stored(path: Path) -> tuple[VerificationRecord, ...]:
    try:
        data = json.loads(path.read_bytes())
    except FileNotFoundError:
        return ()
    except (OSError, ValueError) as exc:
        raise PreservationError(f"Unreadable self-verification state {path}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != STATE_VERSION
        or not isinstance(data.get("records"), list)
    ):
        raise PreservationError(f"Unsupported self-verification state {path}")
    try:
        return tuple(_decode(item) for item in data["records"])
    except PreservationError as exc:
        raise PreservationError(f"{exc} in {path}") from exc


def load_verified(path: Path) -> tuple[VerificationRecord, ...]:
    return tuple(
        record
        for record in _stored(path)
        if HARNESS_VERSION.get(record.agent) == record.harness_version
        and record.inputs == harness_inputs()
    )


def record_verified(path: Path, record: VerificationRecord) -> None:
    with registry_lock(path):
        kept = [
            item
            for item in _stored(path)
            if (item.agent, item.version) != (record.agent, record.version)
        ]
        records = sorted((*kept, record), key=lambda item: (item.agent, item.version))
        payload = {"version": STATE_VERSION, "records": [asdict(item) for item in records]}
        atomic_write_bytes(path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())


def local_versions(records: tuple[VerificationRecord, ...], agent: str) -> tuple[str, ...]:
    return tuple(record.version for record in records if record.agent == agent)


def local_schemas(records: tuple[VerificationRecord, ...], agent: str) -> dict[str, str]:
    return {
        record.version: record.schema_identity
        for record in records
        if record.agent == agent and record.schema_identity
    }
