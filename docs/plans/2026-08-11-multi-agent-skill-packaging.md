# Multi-Agent Skill Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Relocate ringleader's four skills to agent-neutral `.agents/skills/ringleader-*` (self-contained, bundled playbook), add a consent-first `rl skills install/update/status/uninstall` command that symlinks them into the detected agents' skill dirs, and drop the Claude-only plugin.

**Architecture:** Skills become spec-conformant, agent-neutral `SKILL.md` folders bundled into the wheel; a stdlib `rl skills` command resolves the bundled dir and symlinks `ringleader-*` skills into `~/.agents/skills/` (Codex/OpenCode/Pi) and `~/.claude/skills/` (Claude), touching only `ringleader-*` links.

**Tech Stack:** Python 3.13 stdlib + argparse, hatchling (`force-include` for the bundled skills), pytest, ruff.

## Global Constraints

- **stdlib only** — no new runtime dependency in `pyproject.toml`.
- **Skill dirs are namespaced `ringleader-`** (`ringleader-init`, `ringleader-triage`, `ringleader-cleanup`, `ringleader-overview`); each `SKILL.md` `name:` equals its dir.
- **Frontmatter uses only Agent-Skills spec fields** (`name`, `description`).
- **Skills are agent-neutral** — no hard "map `[SELECT]` to `AskUserQuestion`" directive; Claude may appear only as an example inside neutral phrasing; each body defines the `[SELECT]`/`[FAN-OUT]` fallbacks. `PLAYBOOK.md` stays agent-neutral.
- **PLAYBOOK bundled per skill** as `references/playbook.md`, byte-identical to one canonical source (drift-tested).
- **`rl skills` only ever touches `ringleader-*` symlinks** whose target is inside the bundled skills dir — never other skills sharing a dir.
- **Install by symlink**; consent-first (`install`/`update`/`uninstall` prompt unless `--yes`).
- Tests pass with `uv run pytest -q`; `uv run ruff check` and `uv run ruff format --check` clean.
- Commit policy: `feat`/`fix`/`chore`, no scope, short subject.

## File Structure

- `.agents/skills/ringleader-{init,triage,cleanup,overview}/SKILL.md` — **create** (relocated from `plugin/skills/*`).
- `.agents/skills/ringleader-*/references/playbook.md` — **create** (bundled copy of canonical).
- `.agents/skills/PLAYBOOK.md` — **create** (canonical source; not itself a skill, not installed).
- `.agents/skills/SMOKE.md` — **create** (moved from `plugin/SMOKE.md`).
- `plugin/` — **delete** entirely (`.claude-plugin/plugin.json`, `skills/*`, `PLAYBOOK.md`, `SMOKE.md`).
- `ringleader/paths.py` — **modify**: add `bundled_skills_dir()`.
- `ringleader/commands/skills.py` — **create**: the `rl skills` command.
- `ringleader/commands/__init__.py` — **modify**: register the `skills` group.
- `pyproject.toml` — **modify**: `force-include` the skills into the wheel.
- `Justfile` — **modify**: remove `install-plugin`; add `sync-playbook`.
- `README.md`, `AGENTS.md` — **modify**: document `rl skills` + the new layout.
- `tests/test_skills_cmd.py`, `tests/test_playbook_sync.py`, `tests/test_skill_hygiene.py` — **create**.

## Reference: current internals

- Commands register in `ringleader/commands/__init__.py` `TABLE`: flat verbs map to a callable (`"doctor": doctor.cmd_doctor`); groups map to a sub-dict (`"session": {...}`). `main()` walks nested dicts, so a group needs `"skills": {"install": …, …}`.
- `ringleader/paths.py` holds path helpers (`state_dir()`, etc.).
- Wheel currently ships `packages = ["ringleader"]` only — repo-root `.agents/` is NOT in the wheel without `force-include`.
- The four current skills live at `plugin/skills/{init,triage,cleanup,overview}/SKILL.md`; the shared conventions at `plugin/PLAYBOOK.md` (already agent-neutral, zero Claude-isms). The wrapper bodies carry the Claude-isms to remove.

---

### Task 1: Relocate, namespace & bundle the skills

**Files:**
- Create: `.agents/skills/PLAYBOOK.md`, `.agents/skills/ringleader-{init,triage,cleanup,overview}/SKILL.md`, `.agents/skills/ringleader-*/references/playbook.md`, `.agents/skills/SMOKE.md`
- Delete: `plugin/` (whole dir)
- Modify: `ringleader/paths.py`, `pyproject.toml`, `Justfile`
- Test: `tests/test_playbook_sync.py`

**Interfaces:**
- Produces: `paths.bundled_skills_dir() -> Path` — the dir containing the `ringleader-*` skill folders, resolving to the wheel-bundled copy when installed, else the repo's `.agents/skills/`.

- [ ] **Step 1: Move the skill files & canonical playbook**

```bash
cd <repo>
mkdir -p .agents/skills
git mv plugin/PLAYBOOK.md .agents/skills/PLAYBOOK.md
git mv plugin/SMOKE.md .agents/skills/SMOKE.md
for s in init triage cleanup overview; do
  mkdir -p ".agents/skills/ringleader-$s/references"
  git mv "plugin/skills/$s/SKILL.md" ".agents/skills/ringleader-$s/SKILL.md"
done
git rm -r plugin
```

- [ ] **Step 2: Namespace the frontmatter**

In each `.agents/skills/ringleader-<s>/SKILL.md`, change the frontmatter `name:` to match the dir: `name: ringleader-init` / `ringleader-triage` / `ringleader-cleanup` / `ringleader-overview`. Leave `description:` as-is (body neutralization is Task 2).

- [ ] **Step 3: Add the playbook sync target + materialize the copies**

Add to `Justfile`:
```make
sync-playbook:
    @for s in init triage cleanup overview; do \
      cp .agents/skills/PLAYBOOK.md ".agents/skills/ringleader-$$s/references/playbook.md"; \
    done
```
Run `just sync-playbook` to create the four `references/playbook.md` copies.

- [ ] **Step 4: Write the drift test**

```python
# tests/test_playbook_sync.py
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / ".agents" / "skills"

def test_playbook_copies_match_canonical():
    canonical = (ROOT / "PLAYBOOK.md").read_text()
    for s in ("init", "triage", "cleanup", "overview"):
        copy = ROOT / f"ringleader-{s}" / "references" / "playbook.md"
        assert copy.read_text() == canonical, f"{copy} drifted from canonical PLAYBOOK.md — run `just sync-playbook`"
```

- [ ] **Step 5: Run the drift test**

Run: `uv run pytest tests/test_playbook_sync.py -q` — Expected: PASS.

- [ ] **Step 6: Bundle skills into the wheel + add the path resolver**

In `pyproject.toml`, after `[tool.hatch.build.targets.wheel]`:
```toml
[tool.hatch.build.targets.wheel.force-include]
".agents/skills" = "ringleader/_skills"
```
In `ringleader/paths.py`:
```python
def bundled_skills_dir() -> Path:
    """Where the ringleader-* skill folders live: the wheel-bundled copy when
    installed, else the repo's .agents/skills for source runs."""
    packaged = Path(__file__).resolve().parent / "_skills"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parent.parent / ".agents" / "skills"
```

- [ ] **Step 7: Verify build bundles the skills + full suite**

Run:
```bash
uv build 2>&1 | tail -3
uv run python -c "import zipfile,glob; z=zipfile.ZipFile(sorted(glob.glob('dist/*.whl'))[-1]); print([n for n in z.namelist() if '_skills/ringleader-triage/SKILL.md' in n])"
uv run pytest -q && uv run ruff check && uv run ruff format --check
```
Expected: the wheel lists `ringleader/_skills/ringleader-triage/SKILL.md`; suite + ruff clean.

- [ ] **Step 8: Commit**

```bash
git add .agents ringleader/paths.py pyproject.toml Justfile tests/test_playbook_sync.py
git rm -r plugin
git commit -m "chore: relocate skills to .agents/skills and bundle the playbook"
```

---

### Task 2: Neutralize the skill bodies

**Files:**
- Modify: `.agents/skills/ringleader-{init,triage,cleanup,overview}/SKILL.md`
- Test: `tests/test_skill_hygiene.py`

**Interfaces:**
- Consumes: the relocated skills from Task 1.

- [ ] **Step 1: Write the hygiene test (failing)**

```python
# tests/test_skill_hygiene.py
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / ".agents" / "skills"
SKILLS = ["ringleader-init", "ringleader-triage", "ringleader-cleanup", "ringleader-overview"]

def _body(name): return (ROOT / name / "SKILL.md").read_text()

def test_frontmatter_only_spec_fields():
    for name in SKILLS:
        fm = _body(name).split("---")[1]
        keys = set(re.findall(r"^([a-z-]+):", fm, re.M))
        assert keys <= {"name", "description"}, f"{name}: non-spec frontmatter {keys}"
        assert f"name: {name}" in fm

def test_no_hard_claude_mapping():
    # Claude may appear as an example, but not as the sole directive nor a "Claude wrapper" framing.
    for name in SKILLS:
        b = _body(name)
        assert "Claude wrapper" not in b, f"{name}: still framed as a Claude wrapper"
        assert "Claude Code sessions" not in b, f"{name}: says 'Claude Code sessions' (use 'agent sessions')"

def test_select_fanout_neutral_with_fallback():
    for name in SKILLS:
        b = _body(name)
        if "[SELECT]" in b:
            assert "numbered list" in b, f"{name}: [SELECT] lacks the numbered-list fallback"
        if "[FAN-OUT]" in b:
            assert re.search(r"sequential|one at a time|in sequence", b), f"{name}: [FAN-OUT] lacks the sequential fallback"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_skill_hygiene.py -q` — Expected: FAIL (current bodies say "Claude wrapper"/"Claude Code sessions").

- [ ] **Step 3: Neutralize each body**

In each `SKILL.md`:
- Replace the "This is the Claude wrapper: map `[SELECT]` to the `AskUserQuestion` tool … map `[FAN-OUT]` to dispatching parallel subagents …" paragraph with agent-neutral phrasing, e.g.:
  > This skill uses two mechanisms your agent provides. `[SELECT]`: use your native structured-choice tool (Claude Code: `AskUserQuestion`) to get a per-item decision; if you have none, present a **numbered list** and read back the choices. `[FAN-OUT]`: run the independent extractions **in parallel** if your agent supports subagents, otherwise **sequentially, one at a time**. The mechanics, ordering, and harvest sub-protocol are in `references/playbook.md`.
- Change every `plugin/PLAYBOOK.md` / `PLAYBOOK.md` reference to **`references/playbook.md`**.
- Replace "Claude Code sessions" → "agent sessions" (and similar Claude-specific framing) throughout.
- Keep all `rl …` commands exactly as they are (they're real).

- [ ] **Step 4: Run the hygiene test + full suite**

Run: `uv run pytest tests/test_skill_hygiene.py -q && uv run pytest -q && uv run ruff check` — Expected: PASS/clean.

- [ ] **Step 5: Commit**

```bash
git add .agents/skills tests/test_skill_hygiene.py
git commit -m "feat: make the ringleader skills agent-neutral"
```

---

### Task 3: `rl skills` command — install & status

**Files:**
- Create: `ringleader/commands/skills.py`
- Modify: `ringleader/commands/__init__.py`
- Test: `tests/test_skills_cmd.py`

**Interfaces:**
- Consumes: `paths.bundled_skills_dir()` (Task 1).
- Produces: `commands.skills` with `cmd_install(argv)`, `cmd_status(argv)`, and helpers `_bundled_skills()`, `_install_dirs(home)`, `_owned(link)`. Registered as `TABLE["skills"] = {"install": …, "status": …}` (update/uninstall added in Task 4).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_skills_cmd.py
from pathlib import Path
from ringleader.commands import skills

def _fake_home(tmp_path, *agents):
    for a in agents:
        (tmp_path / a).mkdir(parents=True, exist_ok=True)
    return tmp_path

def test_install_links_into_agents_dirs(tmp_path, monkeypatch, capsys):
    home = _fake_home(tmp_path, ".claude", ".codex")
    monkeypatch.setenv("HOME", str(home))
    assert skills.cmd_install(["--yes"]) == 0
    # claude gets ~/.claude/skills, codex the shared ~/.agents/skills
    for base in (home / ".claude" / "skills", home / ".agents" / "skills"):
        link = base / "ringleader-triage"
        assert link.is_symlink() and (link / "SKILL.md").exists()

def test_install_is_idempotent(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    before = sorted((home / ".claude" / "skills").iterdir())
    skills.cmd_install(["--yes"])
    assert sorted((home / ".claude" / "skills").iterdir()) == before

def test_install_no_agents_is_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))  # no agent dirs
    assert skills.cmd_install(["--yes"]) == 0
    assert "no supported agents" in capsys.readouterr().out.lower()

def test_status_reports_linked(tmp_path, monkeypatch, capsys):
    home = _fake_home(tmp_path, ".claude")
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    skills.cmd_status([])
    out = capsys.readouterr().out
    assert "ringleader-triage" in out and "linked" in out.lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_skills_cmd.py -q` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement `skills.py`**

```python
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..paths import bundled_skills_dir

PREFIX = "ringleader-"


def _bundled_skills() -> list[Path]:
    root = bundled_skills_dir()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(PREFIX))


def _install_dirs(home: Path) -> list[tuple[str, Path]]:
    """(agent label, skills dir) for each detected agent."""
    out: list[tuple[str, Path]] = []
    if (home / ".claude").exists():
        out.append(("claude", home / ".claude" / "skills"))
    shared = home / ".agents" / "skills"
    for label, probe in (("codex", ".codex"), ("opencode", ".config/opencode"), ("pi", ".pi")):
        if (home / probe).exists():
            out.append((label, shared))
    return out


def _unique_dirs(targets: list[tuple[str, Path]]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for _, d in targets:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _owned(link: Path) -> bool:
    """A symlink this tool manages: name ringleader-*, target inside the bundled dir
    (or a broken ringleader-* symlink, which is ours to repair/remove)."""
    if not link.name.startswith(PREFIX) or not link.is_symlink():
        return False
    try:
        return str(link.resolve()).startswith(str(bundled_skills_dir().resolve()))
    except OSError:
        return True


def _confirm(targets: list[tuple[str, Path]], skills: list[Path], yes: bool) -> bool:
    print("Will link these skills:")
    for label, d in targets:
        print(f"  {label}: {d}")
    for s in skills:
        print(f"  - {s.name}")
    if yes:
        return True
    return input("Proceed? [y/N] ").strip().lower() in ("y", "yes")


def cmd_install(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl skills install")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args(argv)
    home = Path.home()
    skills = _bundled_skills()
    if not skills:
        print("no bundled ringleader skills found", file=sys.stderr)
        return 1
    targets = _install_dirs(home)
    if not targets:
        print("no supported agents detected (~/.claude, ~/.codex, ~/.config/opencode, ~/.pi)")
        return 0
    if not _confirm(targets, skills, args.yes):
        print("aborted")
        return 0
    created = 0
    for d in _unique_dirs(targets):
        d.mkdir(parents=True, exist_ok=True)
        for s in skills:
            link = d / s.name
            if link.is_symlink():
                if link.resolve() == s.resolve():
                    continue
                link.unlink()
            elif link.exists():
                print(f"skip {link}: a non-symlink already exists there", file=sys.stderr)
                continue
            link.symlink_to(s)
            created += 1
    print(f"linked {created} skill(s)")
    return 0


def cmd_status(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="rl skills status").parse_args(argv)
    home = Path.home()
    skills = _bundled_skills()
    targets = _install_dirs(home)
    if not targets:
        print("no supported agents detected")
        return 0
    for d in _unique_dirs(targets):
        print(f"{d}:")
        for s in skills:
            link = d / s.name
            if link.is_symlink() and link.resolve() == s.resolve():
                state = "linked"
            elif link.is_symlink():
                state = "broken/wrong"
            else:
                state = "missing"
            print(f"  {s.name}: {state}")
    return 0
```
In `ringleader/commands/__init__.py`: import `skills`, add `TABLE["skills"] = {"install": skills.cmd_install, "status": skills.cmd_status}` (as a group, alongside `session`/`mem`/`data`).

- [ ] **Step 4: Run tests + suite + dispatch check**

Run: `uv run pytest tests/test_skills_cmd.py -q && uv run pytest -q && uv run ruff check` then `uv run rl skills` (should list `install`/`status`). Expected: PASS/clean.

- [ ] **Step 5: Commit**

```bash
git add ringleader/commands/skills.py ringleader/commands/__init__.py tests/test_skills_cmd.py
git commit -m "feat: add rl skills install and status"
```

---

### Task 4: `rl skills` — update & uninstall

**Files:**
- Modify: `ringleader/commands/skills.py`, `ringleader/commands/__init__.py`
- Test: `tests/test_skills_cmd.py` (extend)

**Interfaces:**
- Consumes: Task 3 helpers (`_bundled_skills`, `_install_dirs`, `_unique_dirs`, `_owned`, `_confirm`).
- Produces: `cmd_update(argv)`, `cmd_uninstall(argv)`; registered in the `skills` group.

- [ ] **Step 1: Write failing tests**

```python
def test_uninstall_removes_only_ours(tmp_path, monkeypatch):
    from ringleader.commands import skills
    home = tmp_path
    (home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    # a foreign skill sharing the dir must survive
    foreign = home / ".claude" / "skills" / "someone-else"
    foreign.mkdir()
    assert skills.cmd_uninstall(["--yes"]) == 0
    assert not (home / ".claude" / "skills" / "ringleader-triage").exists()
    assert foreign.exists()

def test_update_removes_stale_ringleader_link(tmp_path, monkeypatch):
    from ringleader.commands import skills
    home = tmp_path
    (home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(home))
    skills.cmd_install(["--yes"])
    # simulate a previously-installed skill that's no longer bundled
    stale = home / ".claude" / "skills" / "ringleader-gone"
    stale.symlink_to(skills.bundled_skills_dir() / "ringleader-triage")
    assert skills.cmd_update(["--yes"]) == 0
    assert not stale.exists()  # removed (ours, not in bundled set)
    assert (home / ".claude" / "skills" / "ringleader-triage").is_symlink()  # kept
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_skills_cmd.py -k "uninstall or update" -q` — Expected: FAIL.

- [ ] **Step 3: Implement update & uninstall**

Add to `skills.py`:
```python
def _managed_links(d: Path):
    if not d.is_dir():
        return
    for link in sorted(d.iterdir()):
        if _owned(link):
            yield link


def cmd_uninstall(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl skills uninstall")
    ap.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args(argv)
    home = Path.home()
    dirs = _unique_dirs(_install_dirs(home))
    victims = [link for d in dirs for link in _managed_links(d)]
    if not victims:
        print("nothing installed")
        return 0
    print("Will remove:")
    for v in victims:
        print(f"  {v}")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 0
    for v in victims:
        v.unlink()
    print(f"removed {len(victims)} link(s)")
    return 0


def cmd_update(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl skills update")
    ap.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args(argv)
    home = Path.home()
    bundled = {s.name for s in _bundled_skills()}
    dirs = _unique_dirs(_install_dirs(home))
    stale = [link for d in dirs for link in _managed_links(d) if link.name not in bundled]
    if stale:
        print("Will remove stale links:")
        for v in stale:
            print(f"  {v}")
        if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 0
        for v in stale:
            v.unlink()
    # add/repair the current set (reuse install's linking with the same confirm)
    return cmd_install(["--yes"] if args.yes else [])
```
Register `"update": skills.cmd_update, "uninstall": skills.cmd_uninstall` in the `skills` group in `commands/__init__.py`.

- [ ] **Step 4: Run tests + suite**

Run: `uv run pytest tests/test_skills_cmd.py -q && uv run pytest -q && uv run ruff check` — Expected: PASS/clean.

- [ ] **Step 5: Commit**

```bash
git add ringleader/commands/skills.py ringleader/commands/__init__.py tests/test_skills_cmd.py
git commit -m "feat: add rl skills update and uninstall"
```

---

### Task 5: init integration + docs

**Files:**
- Modify: `.agents/skills/ringleader-init/SKILL.md`, `README.md`, `AGENTS.md`, `.agents/skills/SMOKE.md`

- [ ] **Step 1: Wire `rl skills install` into the init skill**

In `.agents/skills/ringleader-init/SKILL.md`, add a step after the `rl doctor` step: offer to wire the skills into the user's agents — `[SELECT]` consent, then run `rl skills install --yes`; mention `rl skills status` to check and `rl skills update` later.

- [ ] **Step 2: Update README**

Replace the README "Skills" section (which referenced `plugin/` + `just install-plugin`) with: the four `ringleader-*` skills, that they ship to Claude/Codex/OpenCode/Pi, and the `rl skills install` / `status` / `update` / `uninstall` commands. Remove any `plugin/` / `/plugin install` references.

- [ ] **Step 3: Update AGENTS.md**

Update the plugin-layout gotcha: skills now live at `.agents/skills/ringleader-*` (agent-neutral `SKILL.md` + bundled `references/playbook.md`), installed via `rl skills install` (symlinks into `~/.agents/skills` and `~/.claude/skills`); the old `plugin/`/`.claude-plugin` is gone.

- [ ] **Step 4: Update SMOKE.md**

Update `.agents/skills/SMOKE.md` for the new command surface (`rl skills install`/`status`, then exercise a skill) and the `ringleader-*` names.

- [ ] **Step 5: Verify no stale references**

Run:
```bash
grep -rnE "plugin/|install-plugin|/plugin install|plugin/skills" README.md AGENTS.md .agents/skills/ && echo "STALE FOUND" || echo "clean"
```
Expected: `clean`.

- [ ] **Step 6: Commit**

```bash
git add .agents/skills README.md AGENTS.md
git commit -m "docs: document rl skills install and the .agents/skills layout"
```

---

## Self-Review

**Spec coverage:**
- Relocate to `.agents/skills/ringleader-*` + namespace → Task 1. ✓
- Agent-neutral bodies + fallbacks → Task 2. ✓
- PLAYBOOK bundled per skill + canonical + sync + drift test → Task 1 (Steps 3-5). ✓
- Bundled into wheel + runtime resolver → Task 1 (Step 6, `bundled_skills_dir`). ✓
- `rl skills install/status` (consent-first, `--yes`, idempotent) → Task 3. ✓
- `rl skills update/uninstall` (scoped to `ringleader-*`) → Task 4. ✓
- Drop `.claude-plugin/` + `just install-plugin` → Task 1 (delete `plugin/`) + Task 5 (docs). ✓
- init integration → Task 5. ✓
- Docs (README/AGENTS/SMOKE) → Task 5. ✓
- Testing (skills cmd, playbook sync, skill hygiene) → Tasks 1-4. ✓
- Out of scope (memory-triage untouched, no marketplace/plugins) → not touched. ✓

**Placeholder scan:** No TBD/TODO. Command tasks carry full code; content tasks reference the spec's neutral phrasing with the exact substitutions and are guarded by the hygiene test.

**Type consistency:** `bundled_skills_dir() -> Path`; `_bundled_skills() -> list[Path]`; `_install_dirs(home) -> list[tuple[str, Path]]`; `_owned(link) -> bool`; `cmd_install/status/update/uninstall(argv) -> int`. `cmd_update` reuses `cmd_install`. Used consistently across Tasks 3-4.

## Out of scope

- A Claude/other plugin or marketplace channel (dropped).
- Non-Claude agent plugins/extensions (`.codex-plugin/`, OpenCode JS, Pi TS) — skills are read natively.
- `memory-triage` and any change to what the skills do.
