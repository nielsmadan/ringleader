from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..progress import CancelCheck, never_cancelled
from . import proc as model_proc
from .base import CallStats, ModelError


def _inject_additional_properties_false(schema: Any) -> Any:
    # OpenAI's structured-output backend rejects object schemas that omit
    # additionalProperties; codex forwards the schema verbatim, so set it here on
    # every nested object node ($defs/definitions, properties, items included) --
    # not just the root -- since C2's richer schemas will nest objects.
    if not isinstance(schema, dict):
        return schema
    schema = dict(schema)
    if schema.get("type") == "object" and "additionalProperties" not in schema:
        schema["additionalProperties"] = False
    if "properties" in schema:
        schema["properties"] = {
            k: _inject_additional_properties_false(v) for k, v in schema["properties"].items()
        }
    if "items" in schema:
        items = schema["items"]
        if isinstance(items, list):
            schema["items"] = [_inject_additional_properties_false(i) for i in items]
        else:
            schema["items"] = _inject_additional_properties_false(items)
    for key in ("$defs", "definitions"):
        if key in schema:
            schema[key] = {
                k: _inject_additional_properties_false(v) for k, v in schema[key].items()
            }
    return schema


@dataclass
class CodexCliModel:
    name: str = "codex"
    model: str | None = None
    timeout: float | None = None
    cancelled: CancelCheck | None = None
    command_prefix: tuple[str, ...] = ()

    def available(self) -> bool:
        # shutil is a single shared module object across backends, so compare the
        # resolved binary's basename rather than just truthiness of shutil.which.
        path = shutil.which("codex")
        return (
            path is not None
            and Path(path).name == "codex"
            and model_proc.launchable(self.command_prefix)
        )

    def argv(self, schema: dict[str, Any]) -> list[str]:
        # --skip-git-repo-check: cwd isn't guaranteed to be a git repo.
        # --sandbox read-only: this is a completion call, not a file-editing one.
        cmd = [
            "codex",
            "exec",
            "--ephemeral",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
        ]
        return [*cmd, "--model", self.model] if self.model else cmd

    def complete_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        return self.complete_json_with_stats(prompt, schema)[0]

    def complete_json_with_stats(
        self, prompt: str, schema: dict[str, Any]
    ) -> tuple[dict[str, Any], CallStats]:
        wire_schema = _inject_additional_properties_false(schema)
        started = time.perf_counter()
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "schema.json"
            sf.write_text(json.dumps(wire_schema), encoding="utf-8")
            proc = model_proc.run_model_process(
                [*self.command_prefix, *self.argv(schema), "--output-schema", str(sf), prompt],
                stdin_text=None,
                timeout=self.timeout,
                cancelled=self.cancelled or never_cancelled,
            )
        seconds = time.perf_counter() - started
        if proc.returncode != 0:
            raise ModelError(f"codex exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ModelError(f"codex returned unparseable output: {e}") from e
        if not isinstance(result, dict):
            raise ModelError("codex returned non-object JSON")
        return result, CallStats(
            seconds=seconds, input_tokens=None, output_tokens=None, cost_usd=None
        )
