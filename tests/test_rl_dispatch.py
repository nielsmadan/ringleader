import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(*args):
    return subprocess.run([str(REPO / "rl"), *args],
                          cwd=str(REPO), capture_output=True, text=True)


def test_removed_verbs_hint_to_overview():
    for verb in ("projects", "project", "archived"):
        r = _run(verb)
        assert r.returncode != 0
        assert "rl overview" in (r.stdout + r.stderr)


def test_help_has_everyday_and_plumbing_sections():
    r = _run("--help")
    assert r.returncode == 0
    assert "EVERYDAY" in r.stdout and "PLUMBING" in r.stdout
    assert "rl triage" in r.stdout
