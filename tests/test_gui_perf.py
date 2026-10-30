import json
import subprocess
import sys
import time

from ringleader import contract, contract_io, registry
from tests.test_rl_gui import gui_world, subprocess_env

FORBIDDEN = {
    "textual",
    "ringleader.portability",
    "ringleader.commands",
    "ringleader.commands.conversion",
    "ringleader.home.app",
    "ringleader.roundup",
    "ringleader.decisions",
    "ringleader.enrichment",
    "ringleader.models",
}
FORBIDDEN_SET = repr(sorted(FORBIDDEN))
LIST_BUDGET_SECONDS = 0.4

IMPORT_SCRIPT = f"""
import json, sys
import ringleader.contract_io
print(json.dumps(sorted(set(sys.modules) & set({FORBIDDEN_SET}))))
"""

DISPATCH_SCRIPT = f"""
import contextlib, json, os, sys
import ringleader.cli
reg, row_id = sys.argv[1], sys.argv[2]
with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
    codes = [
        ringleader.cli.main(["gui", "list", "--registry", reg]),
        ringleader.cli.main(["gui", "tail", row_id, "--registry", reg]),
    ]
print(json.dumps(codes), file=sys.stderr)
print(json.dumps(sorted(set(sys.modules) & set({FORBIDDEN_SET}))), file=sys.stderr)
"""


def _run(script, env, *args):
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_importing_contract_io_stays_off_the_heavy_modules(monkeypatch, tmp_path):
    fixture = gui_world(monkeypatch, tmp_path, threads=4, transcripts=False)
    result = _run(IMPORT_SCRIPT, subprocess_env(fixture))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_gui_dispatch_stays_off_the_heavy_modules(monkeypatch, tmp_path):
    fixture = gui_world(monkeypatch, tmp_path, threads=40, transcripts=True)
    row_id = registry.qualified_id(registry.load_registry(fixture.registry_path)["threads"][0])
    result = _run(DISPATCH_SCRIPT, subprocess_env(fixture), str(fixture.registry_path), row_id)
    assert result.returncode == 0, result.stderr
    *_, codes, loaded = result.stderr.strip().splitlines()
    assert json.loads(codes) == [0, 0]
    assert json.loads(loaded) == []


def test_list_over_two_thousand_threads_meets_its_budget(monkeypatch, tmp_path, capsysbinary):
    fixture = gui_world(
        monkeypatch,
        tmp_path,
        threads=2000,
        transcripts=False,
        presence=contract.PresenceView(frozenset(), None),
    )
    times = []
    for _ in range(3):
        start = time.perf_counter()
        assert contract_io.cmd_gui_list(["--registry", str(fixture.registry_path)]) == 0
        times.append(time.perf_counter() - start)
        doc = json.loads(capsysbinary.readouterr().out)
    assert min(times) <= LIST_BUDGET_SECONDS
    assert len(doc["sessions"]) >= 1000


DECIDE_SCRIPT = """
import contextlib, json, os, sys
import ringleader.cli
from ringleader import presence
presence.PresenceClient.require_running_session_ids = lambda self: frozenset()
reg, row_id, mtime = sys.argv[1], sys.argv[2], sys.argv[3]
with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
    code = ringleader.cli.main(
        ["gui", "decide", "keep_going", row_id, f"--mtime={mtime}", "--registry", reg]
    )
print(json.dumps([code, "ringleader.guarded" in sys.modules]), file=sys.stderr)
"""


def test_decide_loads_its_stack_on_demand(monkeypatch, tmp_path):
    fixture = gui_world(monkeypatch, tmp_path, threads=4, transcripts=False)
    thread = registry.load_registry(fixture.registry_path)["threads"][0]
    result = _run(
        DECIDE_SCRIPT,
        subprocess_env(fixture),
        str(fixture.registry_path),
        registry.qualified_id(thread),
        thread["mtime"],
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stderr.strip().splitlines()[-1]) == [0, True]
