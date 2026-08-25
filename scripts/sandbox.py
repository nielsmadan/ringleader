#!/usr/bin/env python3
"""Build a throwaway ringleader world for testing the skills from a zero state.

`rl` takes every input it touches from the environment, so a sandbox needs no support
from the tool itself — just a directory and four exported variables.

  build     — rebuild `.sandbox/` from the committed fixture and reset the registry to
              empty. This is the command you normally want.
  --refreeze— regenerate the fixture from your real archive. Rarely needed.

The fixture is committed, small and reproducible, which needs one trick: `rl` only ever
reads a session's `cwd`, `gitBranch`, message *count* and title/summary lines — never the
prose. So the fixture keeps each real session's **shape** (how many turns, whether it was
named, whether it had a summary, which project it belonged to, how the turns interleave)
and throws the text away, replacing it with a placeholder and the paths with invented
ones. Structure comes from real sessions; not one byte of real content ships.

That is what makes it committable: real transcripts carry client names, home paths, email
addresses and pasted credentials in their thousands, and this repo is public. `--refreeze`
verifies its own output and refuses to write anything that still matches those patterns.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "sandbox"
SANDBOX = REPO / ".sandbox"
CLAUDE_ARCHIVE = Path.home() / ".local/share/ringleader/archive"
CODEX_LIVE = Path.home() / ".codex/sessions"
DAY = 86400
# a rollout's shape is established long before a huge one ends
SHAPE_SCAN_LINES = 4000
# real sessions run to 1400+ turns; past this the fixture only gets bigger, not more
# interesting — what matters is spanning prune's --max-turns gate (default 12)
MAX_TURNS = 40

# invented, so nothing about the real machine leaks through the project layout
FAKE_PROJECTS = [
    "/Users/dev/wrksp/orbital-api",
    "/Users/dev/wrksp/orbital-web",
    "/Users/dev/wrksp/notes",
    "/Users/dev/wrksp/incubation/tinderbox",
    "/Users/dev/wrksp/oss/quickpath",
    "/Users/dev/wrksp/oss/quickpath2",
    "/Users/dev/scratch",
]
FAKE_TOPICS = [
    ("wire up the retry budget", "retry-budget"),
    ("port the settings screen to the new router", "settings-router-port"),
    ("chase down the flaky upload test", "flaky-upload-test"),
    ("draft the migration plan for the v3 schema", "v3-schema-migration"),
    ("fix the timezone drift in the digest job", "digest-timezone-drift"),
    ("add pagination to the audit log", "audit-log-pagination"),
    ("investigate the memory spike after deploy", "deploy-memory-spike"),
    ("rename the billing webhooks", "billing-webhook-rename"),
]

# what must never appear in a committed fixture
LEAKS = {
    "absolute home path": re.compile(r"/Users/(?!dev/)[A-Za-z0-9._-]+"),
    "email address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"),
    "credential-shaped": re.compile(r"(sk-[A-Za-z0-9]{8}|ghp_[A-Za-z0-9]{8}|AKIA[A-Z0-9]{8})"),
}


def _shape_claude(path: Path) -> dict | None:
    """Read a real transcript and keep only its shape: the sequence of speakers and
    which metadata lines it carried. No text survives this function."""
    turns: list[str] = []
    has_title = has_summary = False
    try:
        with path.open(errors="ignore") as fh:
            for raw in fh:
                try:
                    o = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                t = o.get("type")
                if t in ("user", "assistant"):
                    turns.append(t)
                elif t == "custom-title":
                    has_title = True
                elif t == "summary":
                    has_summary = True
    except OSError:
        return None
    if not turns:
        return None
    return {"turns": turns[:MAX_TURNS], "has_title": has_title, "has_summary": has_summary}


def _shape_codex(path: Path) -> dict | None:
    turns = 0
    interactive = False
    try:
        with path.open(errors="ignore") as fh:
            for i, raw in enumerate(fh):
                try:
                    o = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                pay = o.get("payload", {})
                if o.get("type") == "session_meta":
                    src = pay.get("source")
                    interactive = src is None or src == "cli"
                    if not interactive:
                        return None
                elif o.get("type") == "response_item" and pay.get("role") == "user":
                    turns += 1
                if i > SHAPE_SCAN_LINES:
                    break
    except OSError:
        return None
    return {"turns": min(turns, MAX_TURNS)} if interactive and turns else None


def _write_claude(dest: Path, shape: dict, cwd: str, topic: str, name: str, sid: str) -> None:
    lines = []
    for n, speaker in enumerate(shape["turns"]):
        body = topic if n == 0 else f"{topic} — step {n}"
        lines.append(
            json.dumps(
                {
                    "type": speaker,
                    "cwd": cwd,
                    "message": {"role": speaker, "content": [{"type": "text", "text": body}]},
                }
            )
        )
    if shape["has_summary"]:
        lines.append(json.dumps({"type": "summary", "summary": f"work on {topic}"}))
    if shape["has_title"]:
        lines.append(json.dumps({"type": "custom-title", "customTitle": name}))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_codex(dest: Path, shape: dict, cwd: str, topic: str, sid: str) -> None:
    lines = [
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"session_id": sid, "id": sid, "cwd": cwd, "source": "cli"},
            }
        ),
        # the injected AGENTS.md preamble a real rollout opens with, so the parser's
        # skip-the-preamble path runs instead of being bypassed
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"text": f"# AGENTS.md instructions for {cwd}"}],
                },
            }
        ),
    ]
    for n in range(shape["turns"]):
        lines.append(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"text": f"{topic} ({n})"}],
                    },
                }
            )
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify(root: Path) -> list[str]:
    """Every committed byte, checked against the patterns that must never ship."""
    problems = []
    for f in sorted(root.rglob("*.jsonl")):
        text = f.read_text(errors="ignore")
        for label, pat in LEAKS.items():
            for m in pat.findall(text):
                problems.append(f"{f.relative_to(root)}: {label}: {m[:40]}")
    return problems


def refreeze(n_claude: int, n_codex: int, seed: int) -> int:
    rng = random.Random(seed)
    shapes: list[tuple[str, dict]] = []

    pool = sorted(p for p in CLAUDE_ARCHIVE.glob("*/*.jsonl") if not p.name.startswith("rollout-"))
    for src in rng.sample(pool, min(n_claude * 3, len(pool))):
        shape = _shape_claude(src)
        if shape:
            shapes.append(("claude-code", shape))
        if sum(1 for s, _ in shapes if s == "claude-code") >= n_claude:
            break

    if CODEX_LIVE.is_dir() and n_codex:
        cpool = sorted(CODEX_LIVE.glob("**/rollout-*.jsonl"))
        for src in rng.sample(cpool, min(n_codex * 30, len(cpool))):
            shape = _shape_codex(src)
            if shape:
                shapes.append(("codex", shape))
            if sum(1 for s, _ in shapes if s == "codex") >= n_codex:
                break

    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    for i, (source, shape) in enumerate(shapes):
        cwd = FAKE_PROJECTS[i % len(FAKE_PROJECTS)]
        topic, name = FAKE_TOPICS[i % len(FAKE_TOPICS)]
        sid = f"{i:08x}-0000-4000-8000-{i:012x}"
        if source == "claude-code":
            _write_claude(
                FIXTURE / "claude" / cwd.replace("/", "-") / f"{sid}.jsonl",
                shape,
                cwd,
                topic,
                name,
                sid,
            )
        else:
            _write_codex(
                FIXTURE / "codex" / "2026" / "01" / f"{i:02d}" / f"rollout-{sid}.jsonl",
                shape,
                cwd,
                topic,
                sid,
            )

    problems = verify(FIXTURE)
    if problems:
        shutil.rmtree(FIXTURE)
        print("REFUSING to write the fixture — sanitising missed something:")
        for p in problems[:10]:
            print("  " + p)
        return 1

    files = sorted(FIXTURE.rglob("*.jsonl"))
    size = sum(f.stat().st_size for f in files)
    turns = [
        len(s["turns"]) if "turns" in s and isinstance(s["turns"], list) else s["turns"]
        for _, s in shapes
    ]
    print(f"fixture: {FIXTURE.relative_to(REPO)}")
    print(f"  {len(files)} sessions, {size / 1024:.0f} KB total — safe to commit")
    print(f"  shapes taken from real sessions: {min(turns)}-{max(turns)} turns each")
    print("  verified: no home paths, emails or credential-shaped strings")
    return 0


def build(span: int, retention: int) -> int:
    if not FIXTURE.is_dir():
        print(f"no fixture at {FIXTURE.relative_to(REPO)} — run `just sandbox-refreeze`")
        return 1
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    data = SANDBOX / "data"
    data.mkdir(parents=True)
    for sub in ("claude", "codex"):
        src = FIXTURE / sub
        dest = SANDBOX / sub
        dest.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)

    files = sorted(SANDBOX.rglob("*.jsonl"))
    for i, f in enumerate(files):  # oldest first, so --recent windows and prune both bite
        when = time.time() - (i * (span / max(len(files) - 1, 1))) * DAY
        os.utime(f, (when, when))

    settings = SANDBOX / "claude-settings.json"
    settings.write_text(json.dumps({"cleanupPeriodDays": retention}, indent=2))

    print(f"sandbox: {SANDBOX}")
    print(f"  {len(files)} sessions from the committed fixture, mtimes over {span} days")
    print("  registry: empty (zero state)\n")
    print("Export these, then run `rl ...` or launch an agent — every call lands in the")
    print("sandbox, and nothing touches your real sessions, registry or settings:\n")
    print(f'export XDG_DATA_HOME="{data}"')
    print(f'export SESSION_TRACKER_PROJECTS_DIR="{SANDBOX / "claude"}"')
    print(f'export CODEX_SESSIONS_DIR="{SANDBOX / "codex"}"')
    print(f'export RINGLEADER_CLAUDE_SETTINGS="{settings}"')
    print("\nRe-run `just sandbox` to reset to zero state.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="sandbox", description=__doc__)
    ap.add_argument("--refreeze", action="store_true", help="regenerate the committed fixture")
    ap.add_argument("-n", "--claude", type=int, default=20)
    ap.add_argument("--codex", type=int, default=8)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--span", type=int, default=30, help="spread mtimes over N days")
    ap.add_argument("--retention", type=int, default=9999)
    args = ap.parse_args()
    if args.refreeze:
        return refreeze(args.claude, args.codex, args.seed)
    return build(args.span, args.retention)


if __name__ == "__main__":
    raise SystemExit(main())
