import re

from conftest import SKILLS_ROOT


def _frontmatter(skill_dir):
    body = (skill_dir / "SKILL.md").read_text()
    parts = body.split("---")
    assert len(parts) >= 3, f"{skill_dir.name}: no `---` frontmatter block"
    return parts[1], body


def test_every_skill_directory_carries_a_skill_file(skill_dirs):
    present = {d.name for d in skill_dirs}
    on_disk = {d.name for d in SKILLS_ROOT.glob("ringleader-*") if d.is_dir()}
    assert present == on_disk, f"skill directories without a SKILL.md: {on_disk - present}"
    assert len(present) >= 7, f"only {len(present)} skills discovered"


def test_frontmatter_only_spec_fields(skill_dirs):
    for d in skill_dirs:
        fm, _ = _frontmatter(d)
        keys = set(re.findall(r"^([a-z-]+):", fm, re.MULTILINE))
        assert keys <= {"name", "description"}, f"{d.name}: non-spec frontmatter {keys}"
        assert {"name", "description"} <= keys, f"{d.name}: missing required frontmatter"
        assert re.search(rf"^name: {re.escape(d.name)}$", fm, re.MULTILINE), (
            f"{d.name}: frontmatter name does not match the directory"
        )


def test_no_hard_claude_mapping(skill_dirs):
    # Claude may appear as an example, but not as the sole directive nor a "Claude wrapper" framing.
    for d in skill_dirs:
        b = (d / "SKILL.md").read_text()
        assert "Claude wrapper" not in b, f"{d.name}: still framed as a Claude wrapper"
        assert "Claude Code sessions" not in b, (
            f"{d.name}: says 'Claude Code sessions' (use 'agent sessions')"
        )


def test_select_fanout_neutral_with_fallback(skill_dirs):
    for d in skill_dirs:
        b = (d / "SKILL.md").read_text()
        if "[SELECT]" in b:
            assert "numbered list" in b, f"{d.name}: [SELECT] lacks the numbered-list fallback"
        if "[FAN-OUT]" in b:
            assert re.search(r"sequential|one at a time|in sequence", b), (
                f"{d.name}: [FAN-OUT] lacks the sequential fallback"
            )
