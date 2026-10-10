from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

from ..capabilities import TargetRequest
from ..compare import compare
from ..model import Conversation, Identity
from ..pi import PROFILE, SUMMARY_PREFIX, SUMMARY_SUFFIX, PiAdapter
from ..versions import parse_version
from .claude import checkpointed, source_conversation
from .common import (
    PNG,
    HarnessResult,
    VerificationFailed,
    isolated_roots,
    require,
    require_equal,
    require_outside_checkout,
    run_native,
)

CONTEXT_LENGTH = 4
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
const model = {id:'claude-sonnet-4-5',provider:'anthropic',api:'anthropic-messages',baseUrl:'http://127.0.0.1:9',input:['text','image'],maxTokens:8192,contextWindow:200000,reasoning:false,cost:{input:0,output:0,cacheRead:0,cacheWrite:0}};
const payloadFor = async messages => {
 let payload;
 let transportTouched = false;
 const client = {beta:{messages:{create(){transportTouched = true; throw new Error('Unexpected transport');}}},messages:{create(){transportTouched = true; throw new Error('Unexpected transport');}}};
 const output = await stream(model, {messages, tools:[]}, {client,cacheRetention:'none',onPayload(params){payload=params;throw new Error('offline-payload-captured');}}).result();
 if (!payload || transportTouched || !String(output.errorMessage).includes('offline-payload-captured')) throw new Error('Offline payload capture failed: ' + output.errorMessage);
 return payload;
};
const { convertToLlm } = await import(pathToFileURL(modulePath.replace('session-manager.js', 'messages.js')));
const { parseArgs } = await import(pathToFileURL(modulePath.replace('core/session-manager.js', 'cli/args.js')));
const assert = (condition, message) => { if (!condition) throw new Error('assertion: ' + message); };
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
assert(context[2].isError === false && context[2].content[0].text === 'read failed', 'native error semantics');
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
assert(providerResult.type === 'tool_result' && providerResult.tool_use_id === payload.messages[1].content[1].id && providerResult.is_error === false, 'provider matched error result');
assert(providerResult.content[1].source.data === process.env.SMOKE_IMAGE, 'provider tool image');
assert(payload.messages[3].content[0].text === 'Final answer', 'provider answer order');
const failed = await payloadFor(context.map(x => x.role === 'toolResult' ? {...x,isError:true} : x));
assert(failed.messages[2].content[0].is_error === true && failed.messages[2].content[0].content[1].source.data === process.env.SMOKE_IMAGE, 'Pi failed tool image preserved');
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
console.log(JSON.stringify({history:historical.length,context:context.length,retainedTailIgnored}));
"""


def session_module(executable: str) -> Path:
    package = Path(executable).resolve().parent.parent / "@earendil-works/pi-coding-agent"
    module = (package / "dist/core/session-manager.js").resolve()
    require(module.is_file(), f"Pi session-manager module beside {executable}")
    return module


def native_case(
    original: Conversation, compact: bool, executable: str, node: str, base: Path
) -> None:
    adapter = PiAdapter(executable)
    if compact:
        original = checkpointed(original, SUMMARY_PREFIX + "checkpoint summary" + SUMMARY_SUFFIX)
    identity = Identity("pi", str(uuid4()))
    roots = isolated_roots(base)
    stage = adapter.stage(original, TargetRequest(identity, roots, PROFILE, original.title or ""))
    compare(original, adapter.read_stage(stage), adapter.normalizations(PROFILE))
    for item in stage.files:
        destination = roots.store / item.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.data)
    path = roots.store / stage.source_ref
    output = run_native(
        [
            node,
            "--input-type=module",
            "-e",
            PI_PROBE,
            str(session_module(executable)),
            str(path),
            str(roots.cwd),
            identity.native_id,
            "yes" if compact else "no",
        ],
        roots,
        "pi",
        {"SMOKE_IMAGE": PNG},
    )
    require(path.read_bytes() == stage.files[0].data, "staged Pi bytes untouched")
    launch = adapter.launch_request(identity, roots)
    require_equal(launch.argv, (executable, "--session", identity.native_id), "launch argv")
    require_equal(json.loads(output.splitlines()[-1])["context"], CONTEXT_LENGTH, "Pi probe")


def run(executable: str, workdir: Path) -> HarnessResult:
    workdir = require_outside_checkout(workdir)
    node = shutil.which("node")
    if node is None:
        raise VerificationFailed("self-verification failed: node is not on PATH")
    roots = isolated_roots(workdir / "version")
    version = run_native([executable, "--version"], roots, "pi")
    require(parse_version(version) is not None, f"pi --version output {version!r}")
    require("--session" in run_native([executable, "--help"], roots, "pi"), "--session")
    original = source_conversation(workdir / "source")
    for compact in (False, True):
        native_case(original, compact, executable, node, workdir / f"case-{int(compact)}")
    return HarnessResult(
        version,
        f"PASS Pi {version}: SessionManager list/read, convertToLlm, transformMessages and the "
        "offline Anthropic payload for plain and checkpoint sessions; tool pairing, error flags, "
        "image bytes, historical timestamps, launch argv. No external inference.",
    )
