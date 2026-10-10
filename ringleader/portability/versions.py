from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class VersionVerdict:
    accepted: bool
    basis: str | None
    needs_verification: bool
    message: str | None


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)+)", text.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def check_version(found: str, pinned: tuple[str, ...], local: tuple[str, ...]) -> VersionVerdict:
    parsed = parse_version(found)
    if parsed is None:
        return VersionVerdict(False, None, False, f"unrecognised version {found!r}")
    for candidates, label in ((pinned, None), (local, "self-verified on this machine")):
        basis = next((item for item in candidates if parse_version(item) == parsed), None)
        if basis is not None:
            message = f"{found} accepted: {label}, not repo-pinned" if label else None
            return VersionVerdict(True, basis, False, message)
    known = ", ".join((*pinned, *local)) or "none"
    return VersionVerdict(
        False,
        None,
        True,
        f"{found} is not a verified version (verified: {known}); it needs self-verification",
    )
