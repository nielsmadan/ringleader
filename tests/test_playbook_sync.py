from conftest import SKILLS_ROOT

# `just sync-playbook` copies to exactly the skills whose SKILL.md names this path, so the
# test has to decide by the same rule — deciding by "a copy exists" instead lets the two
# drift apart, and the drift surfaces as a sync failure that running sync cannot fix.
# The corollary for skill authors: do not mention this path in a skill that should not get
# a playbook (ringleader-publish, which ships references/protocol.md instead).
PLAYBOOK_REF = "references/playbook.md"


def test_a_skill_bundles_a_playbook_exactly_when_it_references_one(skill_dirs):
    assert skill_dirs, "no ringleader-* skill directories found under .agents/skills"
    for skill_dir in skill_dirs:
        wants = PLAYBOOK_REF in (skill_dir / "SKILL.md").read_text()
        copy = skill_dir / "references" / "playbook.md"
        assert copy.is_file() == wants, (
            f"{skill_dir.name}: SKILL.md "
            f"{'references' if wants else 'does not reference'} {PLAYBOOK_REF}, but the "
            f"bundled copy {'is missing' if wants else 'exists'} — "
            f"{'run `just sync-playbook`' if wants else 'delete the stale copy'}"
        )


def test_playbook_copies_match_canonical(skill_dirs):
    canonical = (SKILLS_ROOT / "PLAYBOOK.md").read_text()
    checked = 0
    for skill_dir in skill_dirs:
        copy = skill_dir / "references" / "playbook.md"
        if not copy.is_file():
            continue
        checked += 1
        assert copy.read_text() == canonical, (
            f"{copy} drifted from canonical PLAYBOOK.md — run `just sync-playbook`"
        )
    assert checked, "no bundled playbooks were checked"
