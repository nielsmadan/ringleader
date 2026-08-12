import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / ".agents" / "skills"
SKILLS = ["ringleader-init", "ringleader-triage", "ringleader-cleanup", "ringleader-overview"]


def _body(name):
    return (ROOT / name / "SKILL.md").read_text()


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
        assert "Claude Code sessions" not in b, (
            f"{name}: says 'Claude Code sessions' (use 'agent sessions')"
        )


def test_select_fanout_neutral_with_fallback():
    for name in SKILLS:
        b = _body(name)
        if "[SELECT]" in b:
            assert "numbered list" in b, f"{name}: [SELECT] lacks the numbered-list fallback"
        if "[FAN-OUT]" in b:
            assert re.search(r"sequential|one at a time|in sequence", b), (
                f"{name}: [FAN-OUT] lacks the sequential fallback"
            )
