from __future__ import annotations

import contextlib
import os
from collections.abc import Iterable
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def zone_from(names: Iterable[str], fallback: tzinfo) -> tzinfo:
    for name in filter(None, names):
        try:
            return ZoneInfo(name.removeprefix(":"))
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return fallback


def local_timezone() -> tzinfo:
    names = [os.environ.get("TZ", "")]
    with contextlib.suppress(OSError):
        names.append(os.readlink("/etc/localtime").partition("zoneinfo/")[2])
    return zone_from(names, datetime.now().astimezone().tzinfo or UTC)
