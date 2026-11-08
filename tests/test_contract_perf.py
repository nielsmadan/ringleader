import json
import subprocess
import sys
import time

from ringleader import cli, contract, registry
from tests.test_json_commands import gui_world, subprocess_env

FORBIDDEN = {
    "textual",
    "ringleader.portability",
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
        ringleader.cli.main(["inbox", "--json", "--registry", reg]),
        ringleader.cli.main(["inbox", "--json", "--scan", "--registry", reg]),
        ringleader.cli.main(["data", "export", row_id, "--json", "--registry", reg]),
        ringleader.cli.main(
            ["handoff", "triage", "--project", "/w", "--agent", "claude", "--dry-run", "--json",
             "--registry", reg]
        ),
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


def test_json_dispatch_stays_off_the_heavy_modules(monkeypatch, tmp_path):
    fixture = gui_world(monkeypatch, tmp_path, threads=40, transcripts=True)
    row_id = registry.qualified_id(registry.load_registry(fixture.registry_path)["threads"][0])
    result = _run(DISPATCH_SCRIPT, subprocess_env(fixture), str(fixture.registry_path), row_id)
    assert result.returncode == 0, result.stderr
    *_, codes, loaded = result.stderr.strip().splitlines()
    assert json.loads(codes) == [0, 0, 0, 0]
    assert json.loads(loaded) == []


def test_list_over_two_thousand_threads_meets_its_budget(monkeypatch, tmp_path, capsysbinary):
    fixture = gui_world(
        monkeypatch,
        tmp_path,
        threads=2000,
        transcripts=False,
        presence=contract.PresenceView.ok(frozenset()),
    )
    times = []
    for _ in range(3):
        start = time.perf_counter()
        assert cli.main(["inbox", "--json", "--registry", str(fixture.registry_path)]) == 0
        times.append(time.perf_counter() - start)
        doc = json.loads(capsysbinary.readouterr().out)
    assert min(times) <= LIST_BUDGET_SECONDS
    assert len(doc["sessions"]) >= 1000


GUARDED_SCRIPT = """
import contextlib, json, os, sys
import ringleader.cli
from ringleader import presence
presence.PresenceClient.require_running_session_ids = lambda self: frozenset()
reg, row_id, mtime = sys.argv[1], sys.argv[2], sys.argv[3]
with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
    code = ringleader.cli.main(
        ["session", "mark", "reviewed", row_id, "--json", f"--if-mtime={mtime}", "--registry", reg]
    )
print(json.dumps([code, "ringleader.guarded" in sys.modules]), file=sys.stderr)
"""

DECISION_STACK = ("ringleader.contract_io", "ringleader.guarded", "ringleader.decisions")
PLAIN_SCRIPT = f"""
import contextlib, json, os, sys
import ringleader.cli
reg, notes, *ids = sys.argv[1:]
verbs = [
    ["session", "archive"],
    ["session", "delete"],
    ["session", "harvest"],
    ["session", "snooze"],
    ["session", "keep"],
    ["session", "mark", "reviewed"],
    ["data", "export"],
]
extra = {{"harvest": ["--from-file", notes], "snooze": ["--for=3"]}}
steps = []
for verb, row_id in zip(verbs, ids, strict=True):
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
        argv = [*verb, row_id, *extra.get(verb[-1], []), "--registry", reg]
        code = ringleader.cli.main(argv)
    steps.append([" ".join(verb), code, sorted(set(sys.modules) & set({DECISION_STACK!r}))])
print(json.dumps(steps), file=sys.stderr)
"""
PLAIN_VERBS = (
    "session archive",
    "session delete",
    "session harvest",
    "session snooze",
    "session keep",
    "session mark reviewed",
    "data export",
)


def _first_thread(fixture):
    return registry.load_registry(fixture.registry_path)["threads"][0]


def test_a_guarded_verb_loads_its_stack_on_demand(monkeypatch, tmp_path):
    fixture = gui_world(monkeypatch, tmp_path, threads=4, transcripts=False)
    thread = _first_thread(fixture)
    result = _run(
        GUARDED_SCRIPT,
        subprocess_env(fixture),
        str(fixture.registry_path),
        registry.qualified_id(thread),
        thread["mtime"],
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stderr.strip().splitlines()[-1]) == [0, True]


def test_plain_verbs_stay_off_the_decision_stack(monkeypatch, tmp_path):
    fixture = gui_world(monkeypatch, tmp_path, threads=20, transcripts=True)
    threads = registry.load_registry(fixture.registry_path)["threads"]
    ids = [registry.qualified_id(t) for t in threads if t.get("source") == "claude-code"]
    notes = tmp_path / "notes.md"
    notes.write_text("# notes\n", encoding="utf-8")
    result = _run(
        PLAIN_SCRIPT,
        subprocess_env(fixture),
        str(fixture.registry_path),
        str(notes),
        *ids[: len(PLAIN_VERBS)],
    )
    assert result.returncode == 0, result.stderr
    steps = json.loads(result.stderr.strip().splitlines()[-1])
    assert steps == [[verb, 0, []] for verb in PLAIN_VERBS]
