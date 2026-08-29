from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .base import ModelError


def _envelope_error(stdout: str) -> str | None:
    """Claude reports API-side failures inside its stdout envelope, often while
    exiting 0 and leaving stderr empty — so the reason (auth, credit, rate limit)
    is invisible to anyone reading the exit code or stderr."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict) or not envelope.get("is_error"):
        return None
    reason = str(envelope.get("result") or envelope.get("subtype") or "unknown error")[:200]
    status = envelope.get("api_error_status")
    return f"{reason} (api status {status})" if status else reason


@dataclass
class ClaudeCliModel:
    name: str = "claude"
    model: str | None = None

    def available(self) -> bool:
        # shutil is a single shared module object across backends, so compare the
        # resolved binary's basename rather than just truthiness of shutil.which.
        path = shutil.which("claude")
        return path is not None and Path(path).name == "claude"

    def complete_json(self, prompt: str, schema: dict) -> dict:
        # --json-schema takes the schema inline (a JSON string), not a file path.
        # --tools "" disables tool use so -p returns a pure answer.
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--tools",
            "",
        ]
        if self.model:
            cmd += ["--model", self.model]
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, check=False)
        except OSError as e:  # CLI vanished mid-run
            raise ModelError(f"claude invocation failed: {e}") from e
        detail = _envelope_error(proc.stdout)
        if detail:
            raise ModelError(f"claude: {detail}")
        if proc.returncode != 0:
            raise ModelError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        try:
            envelope = json.loads(proc.stdout)
            # newer claude also returns the parsed object directly under structured_output
            if isinstance(envelope, dict) and isinstance(envelope.get("structured_output"), dict):
                return envelope["structured_output"]
            result = (
                envelope["result"]
                if isinstance(envelope, dict) and "result" in envelope
                else envelope
            )
            return result if isinstance(result, dict) else json.loads(result)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise ModelError(f"claude returned unparseable output: {e}") from e
