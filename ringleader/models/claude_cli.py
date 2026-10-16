from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ..progress import CancelCheck, never_cancelled
from . import proc as model_proc
from .base import CallStats, ModelError


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


_INPUT_TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _stats(envelope: Any, seconds: float) -> CallStats:
    usage = envelope.get("usage") if isinstance(envelope, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    inputs = [usage[k] for k in _INPUT_TOKEN_KEYS if isinstance(usage.get(k), int)]
    output = usage.get("output_tokens")
    cost = envelope.get("total_cost_usd") if isinstance(envelope, dict) else None
    return CallStats(
        seconds=seconds,
        input_tokens=sum(inputs) if inputs else None,
        output_tokens=output if isinstance(output, int) else None,
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
    )


@dataclass
class ClaudeCliModel:
    name: str = "claude"
    model: str | None = None
    timeout: float | None = None
    cancelled: CancelCheck | None = None
    command_prefix: tuple[str, ...] = ()

    def available(self) -> bool:
        # shutil is a single shared module object across backends, so compare the
        # resolved binary's basename rather than just truthiness of shutil.which.
        path = shutil.which("claude")
        return (
            path is not None
            and Path(path).name == "claude"
            and model_proc.launchable(self.command_prefix)
        )

    def argv(self, schema: dict[str, Any]) -> list[str]:
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--tools",
            "",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--settings",
            '{"disableAllHooks":true}',
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-chrome",
        ]
        return [*cmd, "--model", self.model] if self.model else cmd

    def complete_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        return self.complete_json_with_stats(prompt, schema)[0]

    def complete_json_with_stats(
        self, prompt: str, schema: dict[str, Any]
    ) -> tuple[dict[str, Any], CallStats]:
        started = time.perf_counter()
        proc = model_proc.run_model_process(
            [*self.command_prefix, *self.argv(schema)],
            stdin_text=prompt,
            timeout=self.timeout,
            cancelled=self.cancelled or never_cancelled,
        )
        seconds = time.perf_counter() - started
        detail = _envelope_error(proc.stdout)
        if detail:
            raise ModelError(f"claude: {detail}")
        if proc.returncode != 0:
            raise ModelError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        try:
            envelope = json.loads(proc.stdout)
            # newer claude also returns the parsed object directly under structured_output
            if isinstance(envelope, dict) and isinstance(envelope.get("structured_output"), dict):
                return cast(dict[str, Any], envelope["structured_output"]), _stats(
                    envelope, seconds
                )
            result = (
                envelope["result"]
                if isinstance(envelope, dict) and "result" in envelope
                else envelope
            )
            parsed = cast(
                dict[str, Any], result if isinstance(result, dict) else json.loads(result)
            )
            return parsed, _stats(envelope, seconds)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise ModelError(f"claude returned unparseable output: {e}") from e
