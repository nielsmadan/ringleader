import json
import os
from pathlib import Path

from ringleader import cli, contract, contract_io, presence, registry, resume
from ringleader.slug import slugify
from tests.test_json_commands import NOW_DT, gui_world

GOLDEN = Path(__file__).parent / "golden"
STALE_MTIME = "2000-01-01T00:00:00+00:00"


def _normalized(value, roots):
    if isinstance(value, str):
        return _replace_roots(value, roots)
    if isinstance(value, list):
        return [_normalized(v, roots) for v in value]
    if isinstance(value, dict):
        return {k: _normalized(v, roots) for k, v in value.items()}
    return value


def _replace_roots(text, roots):
    for root in sorted(roots, key=len, reverse=True):
        text = text.replace(root, "/fixture").replace(slugify(root), slugify("/fixture"))
    return text


def _check(name, doc, roots):
    text = json.dumps(_normalized(doc, roots), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path = GOLDEN / name
    if os.environ.get("RINGLEADER_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def _run(command, capsys, *argv):
    assert command([*argv]) == 0
    return json.loads(capsys.readouterr().out)


def test_documents_match_the_committed_goldens(monkeypatch, tmp_path, capsys):
    fixture = gui_world(monkeypatch, tmp_path, threads=20, transcripts=False)
    reg_arg = ["--registry", str(fixture.registry_path)]
    reg = registry.load_registry(fixture.registry_path)
    first_claude = next(
        t for t in contract.inbox_threads(reg, NOW_DT.isoformat()) if t["source"] == "claude-code"
    )
    live = frozenset({("claude", first_claude["sessionId"])})
    monkeypatch.setattr(contract_io, "_presence", lambda: contract.PresenceView.ok(live))
    monkeypatch.setattr(presence.PresenceClient, "require_running_session_ids", lambda self: live)
    agents = tuple(
        contract.AgentView(name, name == "claude-code") for name in resume.launchable_agents()
    )
    monkeypatch.setattr(contract_io, "_agents", lambda: agents)
    roots = [str(tmp_path), str(tmp_path.resolve())]

    listing = _run(cli.main, capsys, "inbox", "--json", *reg_arg)
    rows = listing["sessions"]
    tails = _run(
        cli.main,
        capsys,
        "data",
        "export",
        rows[0]["id"],
        rows[1]["id"],
        "claude-code:does-not-exist",
        "--json",
        *reg_arg,
    )
    idle = next(r for r in rows if r["running"] is False)
    ok = _run(
        cli.main,
        capsys,
        "session",
        "mark",
        "reviewed",
        idle["id"],
        "--json",
        f"--if-mtime={idle['mtime']}",
        f"--if-fingerprint={idle['file_fingerprint']}",
        *reg_arg,
    )
    other = next(r for r in rows if r["running"] is False and r["id"] != idle["id"])
    refused = _run(
        cli.main,
        capsys,
        "session",
        "archive",
        other["id"],
        "--json",
        f"--if-mtime={STALE_MTIME}",
        f"--if-fingerprint={other['file_fingerprint']}",
        *reg_arg,
    )
    handoff = _run(
        cli.main,
        capsys,
        "handoff",
        "triage",
        "--project",
        listing["home"]["projects"][0]["key"],
        "--agent",
        "claude",
        "--dry-run",
        "--json",
        *reg_arg,
    )

    assert (ok["status"], refused["status"], handoff["status"]) == ("ok", "refused", "ok")
    for name, doc in (
        ("contract-list.json", listing),
        ("contract-tails.json", tails),
        ("contract-outcome-ok.json", ok),
        ("contract-outcome-refused.json", refused),
        ("contract-handoff.json", handoff),
    ):
        _check(name, doc, roots)
