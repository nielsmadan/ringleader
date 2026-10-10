from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ringleader.portability.capabilities import TargetRequest
from ringleader.portability.claude import PROFILE, ClaudeAdapter
from ringleader.portability.model import Identity
from ringleader.portability.verify import claude, pi
from ringleader.portability.verify.common import PNG, isolated_roots, run_native

CLAUDE_SDK_PROBE = """
import json, os, sys, time
from datetime import datetime
from pathlib import Path
from claude_agent_sdk import get_session_messages, list_sessions
from claude_agent_sdk.types import ContentBlock
from typing import get_args
assert {kind.__name__ for kind in get_args(ContentBlock)} == {"TextBlock", "ThinkingBlock", "ToolUseBlock", "ToolResultBlock", "ServerToolUseBlock", "ServerToolResultBlock"}
session_id, cwd, compact = sys.argv[1:]
listed = list_sessions(directory=cwd, include_worktrees=False)
info = next(item for item in listed if item.session_id == session_id)
assert info.custom_title == 'Native portability smoke'
assert abs(time.time() * 1000 - info.created_at) < 60000
assert abs(time.time() * 1000 - info.last_modified) < 60000
rows = [json.loads(line) for path in Path(os.environ['CLAUDE_CONFIG_DIR']).glob('projects/*/' + session_id + '.jsonl') for line in path.read_text().splitlines()]
assert all(datetime.fromisoformat(row['timestamp'].replace('Z','+00:00')).timestamp() == 1577934245 for row in rows if row['type'] in ('user','assistant'))
messages = get_session_messages(session_id, directory=cwd)
assert len(messages) == 4
first = messages[0].message['content']
assert first[0]['text'] == ('checkpoint summary' if compact == 'yes' else 'Keep original history')
if compact == 'no':
 assert 'Historical user instruction' in first[1]['text']
 assert first[2]['source']['data'] == os.environ['SMOKE_IMAGE']
call = messages[1].message['content'][1]
assert call['type'] == 'tool_use' and call['name'] == 'read' and call['input'] == {'path':'example.txt'}
result = messages[2].message['content'][0]
assert result['type'] == 'tool_result' and result['tool_use_id'] == call['id']
assert result['is_error'] is False and result['content'][0]['text'] == 'read failed'
assert result['content'][1]['source']['data'] == os.environ['SMOKE_IMAGE']
assert messages[3].message['content'][0]['text'] == 'Final answer'
assert messages[1].message['usage'] == {'input_tokens':0,'output_tokens':0}
print(json.dumps({'native':'Claude SDK v0.2.156','active_visible_chain':len(messages),'picker':True}))
"""


def sdk_probe(sdk: Path, base: Path) -> list[object]:
    original = claude.source_conversation(base / "source")
    results = []
    for compact in (False, True):
        value = claude.checkpointed(original, "checkpoint summary") if compact else original
        roots = isolated_roots(base / f"sdk-{int(compact)}")
        identity = Identity("claude-code", str(uuid4()))
        stage = ClaudeAdapter().stage(
            value, TargetRequest(identity, roots, PROFILE, value.title or "")
        )
        for item in stage.files:
            destination = roots.store / item.relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(item.data)
        output = run_native(
            [
                sys.executable,
                "-c",
                CLAUDE_SDK_PROBE,
                identity.native_id,
                str(roots.cwd),
                "yes" if compact else "no",
            ],
            roots,
            "claude-code",
            {"PYTHONPATH": str(sdk), "SMOKE_IMAGE": PNG},
        )
        results.append(json.loads(output))
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--claude", default=shutil.which("claude"))
    parser.add_argument("--pi", default=shutil.which("pi"))
    parser.add_argument(
        "--claude-sdk",
        type=Path,
        help="dev-only: a claude-agent-sdk install to also run the SDK picker probe",
    )
    options = parser.parse_args()
    evidence = options.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    print(claude.run(options.claude, evidence / "claude").summary)
    print(pi.run(options.pi, evidence / "pi").summary)
    if options.claude_sdk:
        with tempfile.TemporaryDirectory(prefix="rl-claude-sdk-") as temporary:
            print(json.dumps(sdk_probe(options.claude_sdk.resolve(), Path(temporary))))


if __name__ == "__main__":
    main()
