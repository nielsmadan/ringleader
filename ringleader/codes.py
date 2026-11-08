from __future__ import annotations

from typing import Final, Literal, get_args

type OutcomeCode = Literal[
    "changed",
    "no_fingerprint",
    "gone",
    "transcript_missing",
    "running",
    "presence_unknown",
    "lifecycle_blocked",
    "until_invalid",
    "until_naive",
    "until_past",
    "no_harvest_model",
    "harvest_failed",
    "failed",
    "other",
]

OUTCOME_CODES: Final[tuple[OutcomeCode, ...]] = get_args(OutcomeCode.__value__)
