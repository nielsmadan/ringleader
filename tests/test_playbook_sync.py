from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / ".agents" / "skills"


def test_playbook_copies_match_canonical():
    canonical = (ROOT / "PLAYBOOK.md").read_text()
    skill_dirs = sorted(p for p in ROOT.glob("ringleader-*") if p.is_dir())
    assert skill_dirs, "no ringleader-* skill directories found under .agents/skills"
    checked = 0
    for skill_dir in skill_dirs:
        copy = skill_dir / "references" / "playbook.md"
        if not copy.exists():
            continue  # ringleader-publish ships its own protocol reference instead
        checked += 1
        assert copy.read_text() == canonical, (
            f"{copy} drifted from canonical PLAYBOOK.md — run `just sync-playbook`"
        )
    assert checked, "no bundled playbooks were checked"
