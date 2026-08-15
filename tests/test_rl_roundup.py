import json

from ringleader.commands import roundup as roundup_mod


def test_roundup_refreshes_prunes_enriches_and_stamps(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"version": 2, "threads": [], "tombstones": []}))
    calls = []
    monkeypatch.setattr(
        roundup_mod.refresh,
        "auto_refresh",
        lambda p, now, **k: (
            calls.append("refresh") or (roundup_mod.registry.load_registry(p), None)
        ),
    )
    monkeypatch.setattr(roundup_mod.prune, "prune_candidates", lambda r, now, **k: [])
    monkeypatch.setattr(roundup_mod.enrich, "cmd_enrich", lambda argv: calls.append("enrich") or 0)
    rc = roundup_mod.cmd_roundup(["--registry", str(reg), "--yes"])
    assert rc == 0 and calls == ["refresh", "enrich"]
    assert json.loads(reg.read_text()).get("lastRoundupAt")
