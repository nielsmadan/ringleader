import json
import os
from pathlib import Path

from ringleader import contract, contract_io, presence, registry
from ringleader.slug import slugify
from tests.test_rl_gui import NOW_DT, gui_world

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
    monkeypatch.setattr(contract_io, "_presence", lambda: contract.PresenceView(live, None))
    monkeypatch.setattr(presence.PresenceClient, "require_running_session_ids", lambda self: live)
    roots = [str(tmp_path), str(tmp_path.resolve())]

    listing = _run(contract_io.cmd_gui_list, capsys, *reg_arg)
    rows = listing["sessions"]
    tails = _run(
        contract_io.cmd_gui_tail,
        capsys,
        rows[0]["id"],
        rows[1]["id"],
        "claude-code:does-not-exist",
        *reg_arg,
    )
    idle = next(r for r in rows if r["running"] is False)
    ok = _run(
        contract_io.cmd_gui_decide,
        capsys,
        "keep_going",
        idle["id"],
        f"--mtime={idle['mtime']}",
        f"--fingerprint={idle['file_fingerprint']}",
        *reg_arg,
    )
    other = next(r for r in rows if r["running"] is False and r["id"] != idle["id"])
    refused = _run(
        contract_io.cmd_gui_decide,
        capsys,
        "archive",
        other["id"],
        f"--mtime={STALE_MTIME}",
        f"--fingerprint={other['file_fingerprint']}",
        *reg_arg,
    )

    assert (ok["status"], refused["status"]) == ("ok", "refused")
    for name, doc in (
        ("gui-list.json", listing),
        ("gui-tails.json", tails),
        ("gui-outcome-ok.json", ok),
        ("gui-outcome-refused.json", refused),
    ):
        _check(name, doc, roots)
