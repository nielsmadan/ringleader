import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "bin" / "python"


def _setup(tmp, sid="a"):
    f = tmp / f"{sid}.jsonl"
    f.write_text(json.dumps({"type": "user", "message":
                 {"role": "user", "content": "hello world"}}) + "\n", encoding="utf-8")
    reg = tmp / "registry.json"
    reg.write_text(json.dumps({"version": 1, "lastScan": None, "tombstones": [],
                   "threads": [{"sessionId": sid, "file": str(f)}]}), encoding="utf-8")
    return reg


def _run(reg, *args):
    return subprocess.run([str(PY), str(REPO / "bin" / "rl-export"),
                           "--registry", str(reg), *args],
                          cwd=str(REPO), capture_output=True, text=True)


def test_export_to_stdout(tmp_path):
    reg = _setup(tmp_path)
    r = _run(reg, "a")
    assert r.returncode == 0 and "hello world" in r.stdout


def test_export_to_file(tmp_path):
    reg = _setup(tmp_path)
    out = tmp_path / "conv.md"
    r = _run(reg, "a", "--out", str(out))
    assert r.returncode == 0
    assert "hello world" in out.read_text()


def test_unknown_id_exits_1(tmp_path):
    reg = _setup(tmp_path)
    r = _run(reg, "nope")
    assert r.returncode == 1 and "unknown session" in r.stderr
