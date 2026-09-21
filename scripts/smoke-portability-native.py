from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from ringleader.portability import native
from ringleader.portability.capabilities import NativeRoots, SourceRequest, TargetRequest
from ringleader.portability.claude import PROFILE as CLAUDE_PROFILE
from ringleader.portability.claude import ClaudeAdapter
from ringleader.portability.compare import compare
from ringleader.portability.model import (
    Attachment,
    AttachmentRef,
    Checkpoint,
    Conversation,
    HistoricalTimestamp,
    Identity,
    Instruction,
    Message,
    Provenance,
    Text,
    ToolCall,
    ToolResult,
)
from ringleader.portability.pi import PROFILE as PI_PROFILE
from ringleader.portability.pi import SUMMARY_PREFIX, SUMMARY_SUFFIX, PiAdapter

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
PI_PROBE = """
import { pathToFileURL } from 'node:url';
import { readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { createRequire } from 'node:module';
const [modulePath, sessionPath, cwd, sessionId, compact] = process.argv.slice(1);
const { SessionManager } = await import(pathToFileURL(modulePath));
const aiEntry = createRequire(modulePath).resolve.paths('@earendil-works/pi-ai').map(root => join(root,'@earendil-works/pi-ai/dist/index.js')).find(existsSync);
if (!aiEntry) throw new Error('Pi AI native module not found');
const { transformMessages } = await import(new URL('./api/transform-messages.js', pathToFileURL(aiEntry)));
const { stream } = await import(new URL('./api/anthropic-messages.js', pathToFileURL(aiEntry)));
const model = {id:'claude-sonnet-4-5',provider:'anthropic',api:'anthropic-messages',input:['text','image'],maxTokens:8192,contextWindow:200000,reasoning:false,cost:{input:0,output:0,cacheRead:0,cacheWrite:0}};
const payloadFor = async messages => {
 let payload;
 let transportTouched = false;
 const client = {beta:{messages:{create(){transportTouched = true; throw new Error('Unexpected transport');}}}};
 const output = await stream(model, {messages, tools:[]}, {client,cacheRetention:'none',onPayload(params){payload=params;throw new Error('offline-payload-captured');}}).result();
 if (!payload || transportTouched || !output.errorMessage.includes('offline-payload-captured')) throw new Error('Offline payload capture failed');
 return payload;
};
const { convertToLlm } = await import(pathToFileURL(modulePath.replace('session-manager.js', 'messages.js')));
const { parseArgs } = await import(pathToFileURL(modulePath.replace('core/session-manager.js', 'cli/args.js')));
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const manager = SessionManager.open(sessionPath);
const entries = manager.getEntries();
const historical = entries.filter(x => x.type === 'message').map(x => x.message);
assert(historical.length === 4, 'complete native historical message count');
assert(historical.every(x => x.timestamp === 1577934245000), 'historical message timestamps');
assert(entries.filter(x => x.type === 'message').every(x => Date.parse(x.timestamp) === 1577934245000), 'historical entry timestamps');
assert(historical[0].content[0].text === 'Keep original history', 'historical user text');
assert(historical[0].content[1].text.includes('Historical user instruction'), 'historical instructions');
assert(historical[0].content[2].data === process.env.SMOKE_IMAGE, 'owned historical attachment');
assert(manager.getCwd() === cwd, 'native cwd');
assert(manager.getSessionName() === 'Native portability smoke', 'native picker name');
const listed = await SessionManager.list(cwd, sessionPath.slice(0, sessionPath.lastIndexOf('/')));
assert(listed.some(x => x.id === sessionId && x.name === 'Native portability smoke'), 'native session listing');
const info = listed.find(x => x.id === sessionId);
assert(Date.now() - info.created.getTime() < 60000, 'fresh native creation');
assert(info.modified.getTime() === 1577934245000, 'Pi picker historical modified time limitation');
const context = convertToLlm(manager.buildSessionContext().messages);
assert(context.length === 4, 'active native model context count');
assert(context[0].content[0].text.includes(compact === 'yes' ? 'checkpoint summary' : 'Keep original history'), 'context begins correctly');
assert(context[1].role === 'assistant' && context[1].content[1].type === 'toolCall', 'native model tool call');
assert(context[1].content[1].arguments.path === 'example.txt', 'native model tool arguments');
assert(context[2].role === 'toolResult' && context[2].toolCallId === context[1].content[1].id, 'native model tool pairing');
assert(context[2].isError === true && context[2].content[0].text === 'read failed', 'native error semantics');
assert(context[2].content[1].data === process.env.SMOKE_IMAGE, 'native model owned tool image');
assert(context[3].content[0].text === 'Final answer', 'native context order');
assert(historical[1].usage.totalTokens === 0, 'zero imported usage');
assert(parseArgs(['--session', sessionId]).session === sessionId, 'native launch argv');
const providerMessages = transformMessages(context, model);
assert(providerMessages.length === 4, 'provider transform preserves supported message count');
const payload = await payloadFor(context);
assert(payload.messages.length === 4, 'provider payload preserves supported message count');
assert(payload.messages[0].content[0].text.includes(compact === 'yes' ? 'checkpoint summary' : 'Keep original history'), 'provider summary or user context');
if (compact === 'no') assert(payload.messages[0].content[2].source.data === process.env.SMOKE_IMAGE, 'provider user image');
assert(payload.messages[1].content[1].type === 'tool_use' && payload.messages[1].content[1].input.path === 'example.txt', 'provider tool call');
const providerResult = payload.messages[2].content[0];
assert(providerResult.type === 'tool_result' && providerResult.tool_use_id === payload.messages[1].content[1].id && providerResult.is_error, 'provider matched error result');
assert(providerResult.content[1].source.data === process.env.SMOKE_IMAGE, 'provider tool image');
assert(payload.messages[3].content[0].text === 'Final answer', 'provider answer order');
const plainUser = {role:'user',content:'question',timestamp:1577934245000};
const plainAssistant = {...historical[3]};
for (const stopReason of ['aborted','error']) {
 for (const errorMessage of [undefined,'']) {
  const source = [plainUser,{...plainAssistant,stopReason,...(errorMessage === undefined ? {} : {errorMessage})}];
  assert(transformMessages(source,model).length === 1, 'incomplete assistant excluded from provider context');
 }
}
for (const role of ['user','assistant']) {
 const interruption = role === 'user' ? plainUser : plainAssistant;
 const interrupted = transformMessages([historical[1],interruption,historical[2]],model);
 assert(interrupted.length === 4 && interrupted[1].content[0].text === 'No result provided', 'native synthetic result on tool interruption');
}
const secondCall = {...historical[1].content[1],id:'second-call'};
const multipleCalls = {...historical[1],content:[...historical[1].content,secondCall]};
const secondResult = {...historical[2],toolCallId:'second-call'};
assert(transformMessages([multipleCalls,historical[2],secondResult],model).length === 3, 'native contiguous multiple results supported');
const assistantImage = {...plainAssistant,content:[{type:'image',data:process.env.SMOKE_IMAGE,mimeType:'image/png'}]};
assert((await payloadFor([plainUser,assistantImage])).messages.length === 1, 'provider excludes invalid assistant-only image');
assert(readFileSync(sessionPath,'utf8').length > 0, 'native source remains readable');
const checkpoint = entries.find(x => x.type === 'compaction');
let retainedTailIgnored = null;
if (checkpoint) {
 const modified = entries.map(x => x === checkpoint ? {...x, firstKeptEntryId: undefined, retainedTail: [{role:'user',content:'UNSUPPORTED RETAINED TAIL',timestamp:0}]} : x);
 const { buildSessionContext } = await import(pathToFileURL(modulePath));
 const projected = convertToLlm(buildSessionContext(modified).messages);
 retainedTailIgnored = projected.length === 2;
 assert(retainedTailIgnored, 'pinned runtime retainedTail discrepancy');
}
console.log(JSON.stringify({native:'Pi SessionManager + convertToLlm + transformMessages + offline Anthropic payload',history:historical.length,context:context.length,picker:true,argv:true,retainedTailIgnored,historicalTimestamps:true,pickerModifiedHistorical:true,providerPayload:true}));
"""
CLAUDE_PROBE = """
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
assert result['is_error'] is True and result['content'][0]['text'] == 'read failed'
assert result['content'][1]['source']['data'] == os.environ['SMOKE_IMAGE']
assert messages[3].message['content'][0]['text'] == 'Final answer'
assert messages[1].message['usage'] == {'input_tokens':0,'output_tokens':0}
print(json.dumps({'native':'Claude SDK v0.2.156','active_visible_chain':len(messages),'picker':True,'cli_model_context':'not probed','historicalTimestamps':True,'freshCreationAndRecency':True}))
"""


def fixture() -> Conversation:
    source = Identity("pi", "source")
    provenance = Provenance("authored", source, "original")
    history = native.linked(
        [
            Message(
                "request",
                "user",
                (
                    Text("Keep original history"),
                    Instruction("Use tabs", "user", provenance),
                    AttachmentRef("image"),
                ),
                provenance,
            ),
            Message(
                "call",
                "assistant",
                (Text("Checking"), ToolCall("read-id", "read", '{"path":"example.txt"}', "json")),
                provenance,
            ),
            Message(
                "result",
                "tool",
                (ToolResult("read-id", (Text("read failed"), AttachmentRef("image")), True),),
                provenance,
            ),
            Message("answer", "assistant", (Text("Final answer"),), provenance),
        ]
    )
    historical = HistoricalTimestamp("2020-01-02T03:04:05.000Z", provenance)
    history = tuple(replace(message, timestamp=historical) for message in history)
    return Conversation(
        source,
        "answer",
        history,
        history,
        attachments=(Attachment("image", "image/png", base64.b64decode(PNG), "pixel.png"),),
        title="Native portability smoke",
    )


def isolated_roots(base: Path) -> NativeRoots:
    roots = NativeRoots(
        base / "home", base / "store", base / "cwd", base / "config", base / "cache"
    )
    for path in (
        roots.home,
        roots.store,
        roots.cwd,
        roots.config,
        roots.cache / "tmp",
        roots.home / "data",
        roots.home / "state",
    ):
        path.mkdir(parents=True, exist_ok=True)
    return roots


def run(
    argv: list[str], roots: NativeRoots, agent: str, extra: dict[str, str] | None = None
) -> str:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "en_US.UTF-8",
        **dict(native.env(roots, agent)),
        **(extra or {}),
    }
    result = subprocess.run(
        argv,
        cwd=roots.cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"{argv[0]} exited {result.returncode}: {result.stdout}\n{result.stderr}"
        )
    return result.stdout.strip()


def smoke_case(base, adapter, profile, original, compact, options):
    agent = profile.agent
    roots = isolated_roots(base)
    if compact:
        text = (
            SUMMARY_PREFIX + "checkpoint summary" + SUMMARY_SUFFIX
            if agent == "pi"
            else "checkpoint summary"
        )
        summary = Message(
            "summary",
            "user",
            (Text(text),),
            original.history[0].provenance,
            timestamp=original.history[0].timestamp,
        )
        checkpoint = Checkpoint(
            "checkpoint",
            original.history[2].id,
            (summary,),
            tuple(item.id for item in original.history[1:3]),
        )
        original = replace(
            original,
            checkpoints=(checkpoint,),
            context=native.linked([summary, *original.history[1:]]),
        )
    identity = Identity(agent, str(uuid4()))
    stage = adapter.stage(original, TargetRequest(identity, roots, profile, original.title))
    compare(original, adapter.read_stage(stage), adapter.normalizations(profile))
    for item in stage.files:
        destination = roots.store / item.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.data)
    path = roots.store / stage.source_ref
    if agent == "pi":
        output = run(
            [
                options.node,
                "--input-type=module",
                "-e",
                PI_PROBE,
                str(options.pi_module),
                str(path),
                str(roots.cwd),
                identity.native_id,
                "yes" if compact else "no",
            ],
            roots,
            agent,
            {"SMOKE_IMAGE": PNG},
        )
    else:
        output = run(
            [
                sys.executable,
                "-c",
                CLAUDE_PROBE,
                identity.native_id,
                str(roots.cwd),
                "yes" if compact else "no",
            ],
            roots,
            agent,
            {"PYTHONPATH": str(options.claude_sdk), "SMOKE_IMAGE": PNG},
        )
    assert path.read_bytes() == stage.files[0].data
    launch = adapter.launch_request(identity, roots)
    assert launch.cwd == roots.cwd
    assert launch.argv == (
        adapter.executable,
        "--session" if agent == "pi" else "--resume",
        identity.native_id,
    )
    return {"agent": agent, "compact": compact, "result": json.loads(output)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pi-module", required=True, type=Path)
    parser.add_argument("--claude-sdk", required=True, type=Path)
    parser.add_argument("--node", default=shutil.which("node"))
    parser.add_argument("--claude", default=shutil.which("claude"))
    parser.add_argument("--pi", default=shutil.which("pi"))
    options = parser.parse_args()
    options.pi_module = options.pi_module.resolve()
    options.claude_sdk = options.claude_sdk.resolve()
    with tempfile.TemporaryDirectory(prefix="rl-native-smoke-") as temporary:
        base = Path(temporary)
        source_roots = isolated_roots(base / "source")
        identity = Identity("pi", str(uuid4()))
        source_stage = PiAdapter().stage(
            fixture(), TargetRequest(identity, source_roots, PI_PROFILE, "Native portability smoke")
        )
        source_path = source_roots.store / source_stage.source_ref
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(source_stage.files[0].data)
        snapshot = PiAdapter().snapshot(SourceRequest(identity, source_roots, str(source_path)))
        shutil.rmtree(source_roots.store)
        results = []
        for agent, adapter, profile, executable in [
            ("claude-code", ClaudeAdapter(), CLAUDE_PROFILE, options.claude),
            ("pi", PiAdapter(), PI_PROFILE, options.pi),
        ]:
            version_roots = isolated_roots(base / f"{agent}-version")
            version = run([executable, "--version"], version_roots, agent)
            assert version == ("2.1.278 (Claude Code)" if agent == "claude-code" else "0.85.1")
            help_text = run([executable, "--help"], version_roots, agent)
            assert ("--session" if agent == "pi" else "--resume") in help_text
            results.append({"agent": agent, "version": version, "help": True})
            for compact in (False, True):
                results.append(
                    smoke_case(
                        base / f"{agent}-{compact}",
                        adapter,
                        profile,
                        snapshot.conversation,
                        compact,
                        options,
                    )
                )
        print(
            json.dumps({"source_removed_before_native_checks": True, "results": results}, indent=2)
        )


if __name__ == "__main__":
    main()
