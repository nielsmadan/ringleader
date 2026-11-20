import json
import os
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from ringleader.cli import main
from ringleader.usage import remote_cache, remote_http
from ringleader.usage.remote_types import HttpReply

ENDPOINTS = {
    "https://openrouter.ai/api/v1/key": ("key", "OPENROUTER_API_KEY"),
    "https://openrouter.ai/api/v1/activity": ("activity", "OPENROUTER_MANAGEMENT_KEY"),
    "https://openrouter.ai/api/v1/credits": ("credits", "OPENROUTER_MANAGEMENT_KEY"),
}


def run_fixture() -> int:
    scenario = Path(sys.argv[1])
    fixture = json.loads(scenario.read_text())
    now = datetime.fromisoformat(fixture["now"])

    def fixed_clock() -> datetime:
        return now

    def fetch(url: str, headers: Mapping[str, str], *, deadline: float) -> HttpReply:
        scope, variable = ENDPOINTS[url]
        assert headers == {
            "Authorization": f"Bearer {os.environ[variable]}",
            "Accept": "application/json",
        }
        assert deadline > time.monotonic()
        with scenario.with_suffix(".requests").open("a") as log:
            print(scope, file=log)
        status, reply_headers, body = fixture["replies"][scope]
        return HttpReply(status, reply_headers, json.dumps(body).encode())

    remote_cache.utc_now = fixed_clock
    remote_http.get = fetch
    return main(sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(run_fixture())
