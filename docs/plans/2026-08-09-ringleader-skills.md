# ringleader Skill Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `ringleader` Claude plugin — `init`/`triage`/`cleanup`/`overview` as an agent-neutral `PLAYBOOK.md` plus thin Claude `SKILL.md` wrappers — backed by the small `rl` additions those skills call.

**Architecture:** First build the deterministic `rl` support (recency filter, snooze-with-duration, overview per-session/snoozed views, `rl doctor`) with pytest TDD. Then scaffold the plugin (mirroring the multi-agent layout the superpowers plugin uses: a plugin root with `.claude-plugin/`, ready for `.codex-plugin/` later) with one shared `PLAYBOOK.md` and four thin `SKILL.md` wrappers whose only agent-specific seams are `[SELECT]`→`AskUserQuestion` and `[FAN-OUT]`→subagents.

**Tech Stack:** stdlib Python 3.13 + argparse (`rl`), pytest, ruff; Markdown skills packaged as a Claude Code plugin.

## Global Constraints

- **`rl` stays stdlib-only** — no new runtime dependencies in `pyproject.toml`.
- **The skill playbook is agent-neutral.** No Claude-only tool name (`AskUserQuestion`, subagent/`Task`) appears in `PLAYBOOK.md`; those live only in the Claude `SKILL.md` wrappers, mapped from the `[SELECT]`/`[FAN-OUT]` placeholders. Each placeholder has a prose/sequential fallback defined in the playbook.
- **Deterministic logic lives in `rl`**, not the skills (ordering, recency filtering, progress counts, snoozed listing).
- **Harvest integration target is prompted, never hardcoded** (default `/doc`); harvest writes to a clean checkout, else an isolated git worktree, confirming first.
- **The four skills replace** `session-triage`/`session-overview` and **fold in** `session-sync`/`session-name`/`session-harvest`; **`memory-triage` stays** untouched.
- Tests pass with `uv run pytest -q`; `uv run ruff check` and `uv run ruff format --check` clean.
- Commit messages follow the repo policy: `feat`/`fix`/`chore`, no scope, short subject.

## File Structure

- `ringleader/commands/triage.py`, `overview.py` — **modify.** Add `--recent <Nd>` filter; overview gains `--sessions --json` and `--snoozed`.
- `ringleader/commands/snooze.py` — **modify.** Accept `--until`/`--for`; store an until.
- `ringleader/registry.py` — **modify.** `snooze_thread` stores `snoozedUntil`; add `snooze_active(thread, now_iso)`.
- `ringleader/overview.py` — **modify.** `triage_inbox` + rollup filters use `snooze_active` (auto-surface when the until has passed); add a snoozed-listing renderer.
- `ringleader/jsonout.py` — **modify.** Add `sessions_json(threads, now_iso)` and `snoozed_json(threads, now_iso)`.
- `ringleader/commands/doctor.py` — **create.** `rl doctor` retention check.
- `ringleader/commands/__init__.py` — **modify.** Register `doctor`.
- `tests/test_recent.py`, `test_snooze_duration.py`, `test_overview_views.py`, `test_doctor.py` — **create.**
- `plugin/.claude-plugin/plugin.json` — **create.** The plugin manifest.
- `plugin/PLAYBOOK.md` — **create.** Shared agent-neutral conventions.
- `plugin/skills/{overview,init,triage,cleanup}/SKILL.md` — **create.** The four wrappers.
- `.claude/skills/{session-triage,session-overview,session-sync,session-name,session-harvest}/` — **delete** (replaced/folded). `memory-triage` stays.
- `Justfile`, `README.md` — **modify.** Add a plugin-install target + document the skills.

## Reference: current internals the tasks build on

- `overview.age_days(mtime_iso, now_iso) -> float` and `overview.effective_status(thread, now_iso) -> str` exist.
- `overview.triage_inbox(threads, now_iso) -> list[dict]` already excludes snoozed via `if t.get("snoozedAt")`.
- `jsonout._session_json(t, now_iso) -> dict` renders one session (has `session_id`, `status`, `age_days`, …). `jsonout.triage_json(...)` wraps `triage_inbox`.
- `registry.snooze_thread(reg, id, when)` currently sets `t["snoozedAt"]=when`; `wake_thread` sets it to `None`.
- `overview.py` cmd already parses `-s/--sessions`, `-a/--all`, `--archived`, `--source`, `--json` (rollup-only, ignores `-s`).
- `triage.py` cmd parses `--project`, `--no-scan`, `--memory`… wait, `--memory` was removed; it parses `--project`, `--no-scan`, `--json`, `--unclassified`.
- Commands register in `commands.TABLE` (nested: flat verbs + `session`/`mem`/`data` groups). `rl doctor` is a new flat verb.

---

### Task 1: `--recent <Nd>` recency filter on triage + overview

**Files:**
- Modify: `ringleader/commands/triage.py`, `ringleader/commands/overview.py`, `ringleader/jsonout.py`
- Test: `tests/test_recent.py`

**Interfaces:**
- Produces: `jsonout.sessions_json(threads, now_iso) -> list[dict]` (maps `_session_json` over the given threads). A shared `overview.filter_recent(threads, now_iso, days) -> list` helper: keep threads whose `age_days(mtime, now) <= days`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_recent.py
from ringleader import overview

NOW = "2026-06-10T00:00:00+00:00"

def _t(sid, mtime):
    return {"sessionId": sid, "mtime": mtime, "status": "unclassified", "project": "/p"}

def test_filter_recent_keeps_within_window():
    threads = [_t("new", "2026-06-09T00:00:00+00:00"), _t("old", "2026-06-01T00:00:00+00:00")]
    kept = overview.filter_recent(threads, NOW, 3)
    assert [t["sessionId"] for t in kept] == ["new"]

def test_filter_recent_none_returns_all():
    threads = [_t("a", "2026-01-01T00:00:00+00:00")]
    assert overview.filter_recent(threads, NOW, None) == threads
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_recent.py -q` — Expected: FAIL (`filter_recent` undefined).

- [ ] **Step 3: Implement `filter_recent` + `sessions_json`**

In `ringleader/overview.py`:
```python
def filter_recent(threads, now_iso: str, days: int | None):
    if days is None:
        return threads
    return [t for t in threads if t.get("mtime") and age_days(t["mtime"], now_iso) <= days]
```
In `ringleader/jsonout.py`:
```python
def sessions_json(threads, now_iso: str) -> list[dict]:
    return [_session_json(t, now_iso) for t in threads]
```

- [ ] **Step 4: Wire `--recent` into the commands**

In both `triage.py` and `overview.py` argparsers add:
```python
ap.add_argument("--recent", type=int, default=None, metavar="DAYS",
                help="only sessions active within the last DAYS")
```
In `triage.py`, before building the inbox output, filter the threads through `overview.filter_recent(threads, now_iso, args.recent)`. In `overview.py`'s `--sessions` path, apply the same filter to the live-thread list.

- [ ] **Step 5: Run the test + full suite**

Run: `uv run pytest tests/test_recent.py -q && uv run pytest -q && uv run ruff check` — Expected: PASS/clean.

- [ ] **Step 6: Commit**

```bash
git add ringleader/overview.py ringleader/jsonout.py ringleader/commands/triage.py ringleader/commands/overview.py tests/test_recent.py
git commit -m "feat: add --recent day filter to triage and overview"
```

---

### Task 2: Snooze with a duration + auto-wake

**Files:**
- Modify: `ringleader/registry.py`, `ringleader/commands/snooze.py`, `ringleader/overview.py`
- Test: `tests/test_snooze_duration.py`

**Interfaces:**
- Produces: `registry.snooze_thread(reg, id, when, until=None)` — stores `t["snoozedAt"]=when`, `t["snoozedUntil"]=until` (ISO or `None`). `overview.snooze_active(thread, now_iso) -> bool` — `True` if `snoozedAt` set AND (`snoozedUntil` is `None` OR `now_iso < snoozedUntil`). Consumers filter on `snooze_active`, not the raw `snoozedAt`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_snooze_duration.py
from ringleader import overview, registry

def _reg(**over):
    t = {"sessionId": "s1", "mtime": "2026-06-01T00:00:00+00:00", "snoozedAt": None, "snoozedUntil": None}
    t.update(over)
    return {"threads": [t]}

def test_snooze_active_indefinite():
    t = _reg(snoozedAt="2026-06-01T00:00:00+00:00")["threads"][0]
    assert overview.snooze_active(t, "2026-06-10T00:00:00+00:00") is True

def test_snooze_active_until_not_passed():
    t = _reg(snoozedAt="2026-06-01T00:00:00+00:00", snoozedUntil="2026-06-20T00:00:00+00:00")["threads"][0]
    assert overview.snooze_active(t, "2026-06-10T00:00:00+00:00") is True

def test_snooze_auto_wakes_after_until():
    t = _reg(snoozedAt="2026-06-01T00:00:00+00:00", snoozedUntil="2026-06-05T00:00:00+00:00")["threads"][0]
    assert overview.snooze_active(t, "2026-06-10T00:00:00+00:00") is False

def test_snooze_thread_stores_until():
    reg = _reg()
    registry.snooze_thread(reg, "s1", "2026-06-01T00:00:00+00:00", until="2026-06-08T00:00:00+00:00")
    assert reg["threads"][0]["snoozedUntil"] == "2026-06-08T00:00:00+00:00"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_snooze_duration.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement**

In `registry.py`, update `snooze_thread` to accept `until: str | None = None` and set `t["snoozedUntil"] = until` alongside `snoozedAt`. In `overview.py`:
```python
def snooze_active(thread: dict, now_iso: str) -> bool:
    if not thread.get("snoozedAt"):
        return False
    until = thread.get("snoozedUntil")
    return until is None or now_iso < until
```
Replace the raw `if t.get("snoozedAt")` checks in `triage_inbox` and the rollup snoozed counts with `snooze_active(t, now_iso)`.

- [ ] **Step 4: Add `--until` / `--for` to the snooze command**

In `snooze.py`:
```python
ap.add_argument("--until", help="ISO timestamp to auto-wake at")
ap.add_argument("--for", dest="for_days", type=int, help="auto-wake after N days")
...
until = args.until
if args.for_days is not None:
    until = (datetime.now(UTC) + timedelta(days=args.for_days)).isoformat()
registry.snooze_thread(reg, args.id, when, until=until)
```
(Add `timedelta` to the datetime import.) Update the success print to mention the wake time when set.

- [ ] **Step 5: Run tests + suite**

Run: `uv run pytest tests/test_snooze_duration.py -q && uv run pytest -q && uv run ruff check` — Expected: PASS/clean. (Existing snooze/overview tests must still pass — `snooze_active` is behavior-compatible for indefinite snoozes.)

- [ ] **Step 6: Commit**

```bash
git add ringleader/registry.py ringleader/commands/snooze.py ringleader/overview.py tests/test_snooze_duration.py
git commit -m "feat: support snoozing a session for a duration with auto-wake"
```

---

### Task 3: Overview per-session JSON + `--snoozed` listing

**Files:**
- Modify: `ringleader/commands/overview.py`, `ringleader/jsonout.py`
- Test: `tests/test_overview_views.py`

**Interfaces:**
- Consumes: `jsonout.sessions_json` (Task 1), `overview.snooze_active` (Task 2).
- Produces: `jsonout.snoozed_json(threads, now_iso) -> list[dict]` — one entry per currently-snooze_active thread: `_session_json(t)` plus `"snoozed_until": t.get("snoozedUntil")`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_overview_views.py
from ringleader import jsonout

NOW = "2026-06-10T00:00:00+00:00"

def _t(sid, **o):
    t = {"sessionId": sid, "mtime": "2026-06-09T00:00:00+00:00", "status": "active",
         "project": "/p", "snoozedAt": None, "snoozedUntil": None}
    t.update(o); return t

def test_sessions_json_shape():
    rows = jsonout.sessions_json([_t("s1")], NOW)
    assert rows[0]["session_id"] == "s1" and "age_days" in rows[0]

def test_snoozed_json_only_active_with_until():
    threads = [
        _t("awake"),
        _t("napping", snoozedAt="2026-06-01T00:00:00+00:00", snoozedUntil="2026-06-20T00:00:00+00:00"),
        _t("expired", snoozedAt="2026-06-01T00:00:00+00:00", snoozedUntil="2026-06-05T00:00:00+00:00"),
    ]
    rows = jsonout.snoozed_json(threads, NOW)
    assert [r["session_id"] for r in rows] == ["napping"]
    assert rows[0]["snoozed_until"] == "2026-06-20T00:00:00+00:00"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_overview_views.py -q` — Expected: FAIL.

- [ ] **Step 3: Implement `snoozed_json` + wire the flags**

In `jsonout.py`:
```python
def snoozed_json(threads, now_iso: str) -> list[dict]:
    from . import overview
    out = []
    for t in threads:
        if overview.snooze_active(t, now_iso):
            row = _session_json(t, now_iso)
            row["snoozed_until"] = t.get("snoozedUntil")
            out.append(row)
    return out
```
In `overview.py`: add `--snoozed` (action="store_true"). When `--snoozed`: print `snoozed_json` (if `--json`) or a text list of `name · age · until`. When `--sessions --json`: print `jsonout.sessions_json(live_threads, now_iso)` instead of the rollup json (make the `--json` branch honor `--sessions`).

- [ ] **Step 4: Run tests + suite**

Run: `uv run pytest tests/test_overview_views.py -q && uv run pytest -q && uv run ruff check` — Expected: PASS/clean.

- [ ] **Step 5: Commit**

```bash
git add ringleader/commands/overview.py ringleader/jsonout.py tests/test_overview_views.py
git commit -m "feat: add overview per-session json and a snoozed listing"
```

---

### Task 4: `rl doctor` — session-retention check

**Files:**
- Create: `ringleader/commands/doctor.py`
- Modify: `ringleader/commands/__init__.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Produces: `doctor.check_claude_retention(settings_path) -> dict` returning `{"harness": "claude-code", "setting": "cleanupPeriodDays", "value": <int|None>, "ok": <bool>, "advice": <str>}`. `value` is the parsed setting (or `None` if unset/missing file); `ok` is `value is not None and value >= 30`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doctor.py
import json
from ringleader.commands import doctor

def test_reports_low_retention(tmp_path):
    p = tmp_path / "settings.json"; p.write_text(json.dumps({"cleanupPeriodDays": 7}))
    r = doctor.check_claude_retention(p)
    assert r["value"] == 7 and r["ok"] is False and "cleanupPeriodDays" in r["advice"]

def test_reports_ok_retention(tmp_path):
    p = tmp_path / "settings.json"; p.write_text(json.dumps({"cleanupPeriodDays": 90}))
    assert doctor.check_claude_retention(p)["ok"] is True

def test_missing_file_is_not_ok(tmp_path):
    r = doctor.check_claude_retention(tmp_path / "nope.json")
    assert r["value"] is None and r["ok"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_doctor.py -q` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

Create `ringleader/commands/doctor.py`:
```python
from __future__ import annotations
import argparse, json
from pathlib import Path

def check_claude_retention(settings_path) -> dict:
    p = Path(settings_path)
    value = None
    try:
        value = json.loads(p.read_text()).get("cleanupPeriodDays")
    except (OSError, json.JSONDecodeError):
        value = None
    ok = isinstance(value, int) and value >= 30
    advice = (
        "cleanupPeriodDays looks fine."
        if ok else
        "Set cleanupPeriodDays >= 30 in ~/.claude/settings.json so ringleader can "
        "curate sessions before Claude Code deletes them."
    )
    return {"harness": "claude-code", "setting": "cleanupPeriodDays",
            "value": value, "ok": ok, "advice": advice}

def cmd_doctor(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl doctor")
    ap.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    args = ap.parse_args(argv)
    r = check_claude_retention(args.settings)
    mark = "✓" if r["ok"] else "⚠"
    print(f"{mark} {r['harness']} {r['setting']}={r['value']}")
    if not r["ok"]:
        print(f"  {r['advice']}")
    return 0
```
Register in `commands/__init__.py`: import `doctor`, add `"doctor": doctor.cmd_doctor` to the flat entries in `TABLE`.

- [ ] **Step 4: Run tests + suite + dispatch check**

Run: `uv run pytest tests/test_doctor.py -q && uv run pytest -q && uv run ruff check` then `uv run rl doctor --help` — Expected: PASS/clean; help shows `rl doctor`.

- [ ] **Step 5: Commit**

```bash
git add ringleader/commands/doctor.py ringleader/commands/__init__.py tests/test_doctor.py
git commit -m "feat: add rl doctor session-retention check"
```

---

### Task 5: Plugin scaffold + shared `PLAYBOOK.md`

**Files:**
- Create: `plugin/.claude-plugin/plugin.json`, `plugin/PLAYBOOK.md`
- Delete: `.claude/skills/session-triage`, `session-overview`, `session-sync`, `session-name`, `session-harvest`
- Modify: `Justfile`

**Interfaces:**
- Produces: `plugin/PLAYBOOK.md` — the single source of truth the four `SKILL.md` wrappers reference. Defines: (a) how to call `rl` (installed vs `uv run rl`; read structured data from `rl … --json`, never edit `registry.json`); (b) strategic ordering (auto-prune first, then project-by-project oldest-first in bounded chunks); (c) the running progress tally format `Project i/N · done/total triaged · H harvested`; (d) the harvest sub-protocol (prompt the integration target defaulting to `/doc`; write to a clean checkout else a fresh `git worktree`, confirm first; two-phase extract→synthesize-per-repo; then `rl session mark harvested` + `rl session archive`); (e) the two enhancement placeholders with fallbacks — `[SELECT]` ("present a batch, get a per-item choice"; fallback: numbered list, user replies with codes) and `[FAN-OUT]` ("run independent read-only tasks concurrently"; fallback: sequential).

- [ ] **Step 1: Write the manifest**

`plugin/.claude-plugin/plugin.json`:
```json
{
  "name": "ringleader",
  "description": "Curate dormant agent sessions — triage, overview, and cleanup skills over the rl CLI.",
  "version": "0.1.0",
  "keywords": ["sessions", "triage", "ringleader", "cleanup"]
}
```

- [ ] **Step 2: Write `PLAYBOOK.md`**

Author the shared conventions per the Interfaces block above and §"Shared conventions" of the spec (`docs/specs/2026-08-04-ringleader-skills-design.md`). Agent-neutral only — no `AskUserQuestion`/subagent mentions; use `[SELECT]`/`[FAN-OUT]`. Include a "Calling rl" section listing the exact commands the skills use: `rl prune`, `rl triage [--recent N] [--project X] --json`, `rl overview [-s] [--recent N] [--snoozed] --json`, `rl session set/delete/name/snooze/archive`, `rl session mark reviewed|harvested|cleaned`, `rl doctor`.

- [ ] **Step 3: Remove the replaced/folded skills**

```bash
git rm -r .claude/skills/session-triage .claude/skills/session-overview .claude/skills/session-sync .claude/skills/session-name .claude/skills/session-harvest
```
(Leave `.claude/skills/memory-triage` in place.)

- [ ] **Step 4: Add a plugin-install Justfile target**

Add to `Justfile`:
```make
install-plugin:
    @echo "Load the ringleader plugin dir into Claude Code: /plugin install $(pwd)/plugin"
```

- [ ] **Step 5: Validate**

Run:
```bash
python -c "import json,sys; json.load(open('plugin/.claude-plugin/plugin.json'))"
grep -oE "rl [a-z][a-z -]*" plugin/PLAYBOOK.md | sort -u
grep -c "AskUserQuestion\|subagent\|Task tool" plugin/PLAYBOOK.md   # must print 0
```
Expected: manifest parses; every `rl …` reference matches a real command from the reference list; the Claude-ism grep prints `0`.

- [ ] **Step 6: Commit**

```bash
git add plugin/.claude-plugin/plugin.json plugin/PLAYBOOK.md Justfile
git rm -r .claude/skills/session-triage .claude/skills/session-overview .claude/skills/session-sync .claude/skills/session-name .claude/skills/session-harvest
git commit -m "feat: scaffold the ringleader plugin and shared playbook"
```

---

### Task 6: `overview` + `init` skill wrappers

**Files:**
- Create: `plugin/skills/overview/SKILL.md`, `plugin/skills/init/SKILL.md`

**Interfaces:**
- Consumes: `PLAYBOOK.md` (Task 5), `rl overview --recent/--snoozed/--sessions --json`, `rl doctor`.

- [ ] **Step 1: Write `overview/SKILL.md`**

Frontmatter `name: overview`, a `description` with triggers ("morning overview", "what's in flight"). Body follows the spec's overview flow: `rl overview` (auto-refresh) → if any untriaged session older than 24h, `[SELECT]` offer to drop into `triage` → collect open sessions from the last 3 days via `rl overview -s --recent 3 --json` → list snoozed via `rl overview --snoozed` → `[SELECT]` pick next / snooze (`rl session snooze <id> --for <days>`) / send to triage. State the Claude mapping once: "`[SELECT]` → use `AskUserQuestion`." Reference `PLAYBOOK.md` for conventions.

- [ ] **Step 2: Write `init/SKILL.md`**

Frontmatter `name: init`, triggers ("set up ringleader", "get started"). Body: verify `rl` installed (else `uv run rl`) → run `rl doctor`, relay advice and offer to help bump `cleanupPeriodDays` → kick off a bounded first triage by following the `triage` skill with `scope: recent` bounded to `--recent 7` → point at `cleanup` for the backlog.

- [ ] **Step 3: Validate**

Run:
```bash
for f in plugin/skills/overview/SKILL.md plugin/skills/init/SKILL.md; do
  head -1 "$f" | grep -q '^---' && echo "$f frontmatter ok"
  grep -oE "rl [a-z][a-z -]*" "$f" | sort -u
done
```
Expected: both have YAML frontmatter; every `rl …` matches a real command (incl. the Task 1-4 additions).

- [ ] **Step 4: Commit**

```bash
git add plugin/skills/overview/SKILL.md plugin/skills/init/SKILL.md
git commit -m "feat: add ringleader overview and init skills"
```

---

### Task 7: `triage` + `cleanup` skill wrappers (shared, scoped)

**Files:**
- Create: `plugin/skills/triage/SKILL.md`, `plugin/skills/cleanup/SKILL.md`

**Interfaces:**
- Consumes: `PLAYBOOK.md` harvest sub-protocol + `[SELECT]`/`[FAN-OUT]`; `rl prune`, `rl triage --recent/--project --json`, `rl session set/delete/name/archive`, `rl session mark reviewed|harvested|cleaned`.

- [ ] **Step 1: Write `triage/SKILL.md` (scope: recent)**

Frontmatter `name: triage`, triggers ("triage sessions", "clean up my sessions"). Body follows the spec's triage flow and defers all shared mechanics to `PLAYBOOK.md`: auto-prune → build inbox `rl triage --recent <N> --json` grouped by project → per project with the progress tally, `[SELECT]` delete/finished/open → apply (delete now; finished→harvest queue; open→`rl session set …`) → harvest the finished set via the playbook's harvest sub-protocol (`[FAN-OUT]` extract) → name+review open (`rl session name`, `rl session mark reviewed`) → `rl session mark cleaned <project>` + report tally. State the Claude mappings once: `[SELECT]`→`AskUserQuestion`, `[FAN-OUT]`→parallel subagents.

- [ ] **Step 2: Write `cleanup/SKILL.md` (scope: all)**

Frontmatter `name: cleanup`, triggers ("clean up session history", "the big backlog"). Body: same as `triage` but unbounded (omit `--recent`), emphasizing bounded chunks + resumability ("re-run to continue where you left off — the registry records what's triaged"). Do **not** duplicate the harvest/selection mechanics — reference `PLAYBOOK.md` and the `triage` flow, differing only in scope.

- [ ] **Step 3: Validate**

Run:
```bash
for f in plugin/skills/triage/SKILL.md plugin/skills/cleanup/SKILL.md; do
  head -1 "$f" | grep -q '^---' && echo "$f frontmatter ok"
  grep -oE "rl [a-z][a-z -]*" "$f" | sort -u
done
grep -l "scope" plugin/skills/triage/SKILL.md plugin/skills/cleanup/SKILL.md
```
Expected: frontmatter present; `rl …` references real; both name their scope.

- [ ] **Step 4: Commit**

```bash
git add plugin/skills/triage/SKILL.md plugin/skills/cleanup/SKILL.md
git commit -m "feat: add ringleader triage and cleanup skills"
```

---

### Task 8: Docs + smoke checklist

**Files:**
- Modify: `README.md`, `AGENTS.md`
- Create: `plugin/SMOKE.md`

- [ ] **Step 1: Document the plugin**

In `README.md`, add a "Skills" section: the four skills, what each does, and `just install-plugin`. In `AGENTS.md`, note the plugin layout (playbook + wrappers) and that the old `session-*` skills were folded into it.

- [ ] **Step 2: Write the smoke checklist**

`plugin/SMOKE.md` — a manual run list (skills are markdown, no unit harness): (1) `overview` renders and offers triage when a >24h untriaged session exists; (2) `triage` completes a small `--recent 3` scope end-to-end on a throwaway `--registry`; (3) harvest writes to a clean checkout, and to a worktree when the checkout is dirty; (4) `init` reports retention and runs a bounded pass; (5) `cleanup` resumes on re-run. Each item: the command(s) and the expected observable.

- [ ] **Step 3: Verify docs reference only the new surface**

Run: `grep -nE "session-triage|session-overview|session-sync|session-name|session-harvest" README.md AGENTS.md` — Expected: no output (old skill names gone from docs).

- [ ] **Step 4: Commit**

```bash
git add README.md AGENTS.md plugin/SMOKE.md
git commit -m "docs: document the ringleader plugin skills and smoke checklist"
```

---

## Self-Review

**Spec coverage:**
- Four skills (init/triage/cleanup/overview) → Tasks 5-8. ✓
- Playbook + per-agent wrapper, `[SELECT]`/`[FAN-OUT]` with fallbacks → Task 5 (playbook), Tasks 6-7 (mappings). ✓
- Fate of existing skills (replace/fold/keep memory-triage) → Task 5 Step 3. ✓
- Harvest sub-protocol (prompted target, clean-checkout-else-worktree, two-phase) → Task 5 playbook, used in Task 7. ✓
- Strategic ordering + progress tally → Task 5 playbook. ✓
- `rl` additions: `--recent` (Task 1), snooze duration (Task 2), overview per-session json + `--snoozed` (Task 3), `rl doctor` (Task 4), `--json` ids (already present — noted in Reference). ✓
- Overview flow (24h untriaged offer, 3-day open, snoozed, pick/snooze) → Task 6. ✓
- init settings check + bounded first triage → Task 6. ✓
- Testing (rl pytest + skills smoke) → Tasks 1-4 tests, Task 8 SMOKE.md. ✓
- Out of scope (memory/Codex-wrapper/TUI/Spec-2) → untouched. ✓

**Placeholder scan:** No TBD/TODO. Skill-authoring steps reference the spec's flow sections + concrete `rl` commands rather than pre-writing full prose (the deliverable is prose; the structure + commands + validation are concrete). rl code steps carry real code.

**Type consistency:** `snooze_active(thread, now_iso)`, `filter_recent(threads, now_iso, days)`, `sessions_json`/`snoozed_json(threads, now_iso)`, `check_claude_retention(settings_path)->dict{value,ok,advice}` are used consistently across tasks. `snooze_thread(reg,id,when,until=None)` matches Task 2's caller in `snooze.py`.

## Out of scope

- The `memory-triage` skill / memory track (future `ringleader:mem`).
- Non-Claude skill wrappers (`.codex-plugin/` etc.) — follow that agent's source.
- The TUI (Spec 3) and tool-native-LLM harvest (Spec 2).
- Plugin marketplace publishing — local install via `just install-plugin` only.
