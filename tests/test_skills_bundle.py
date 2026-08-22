from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent / ".agents" / "skills"


def test_usage_skill_is_bundled_with_frontmatter_and_playbook():
    skill = SKILLS / "ringleader-usage" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\nname: ringleader-usage\n")
    assert "description:" in text
    assert (SKILLS / "ringleader-usage" / "references" / "playbook.md").exists()


def test_usage_skill_drives_the_real_commands():
    text = (SKILLS / "ringleader-usage" / "SKILL.md").read_text(encoding="utf-8")
    assert "rl usage" in text
    assert "rl usage session" in text
