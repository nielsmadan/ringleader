from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / ".agents" / "skills"


def test_playbook_copies_match_canonical():
    canonical = (ROOT / "PLAYBOOK.md").read_text()
    for s in ("init", "triage", "cleanup", "overview"):
        copy = ROOT / f"ringleader-{s}" / "references" / "playbook.md"
        assert copy.read_text() == canonical, (
            f"{copy} drifted from canonical PLAYBOOK.md — run `just sync-playbook`"
        )
