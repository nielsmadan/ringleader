from ringleader import scanner
from ringleader.devtools.isolation import agent_store_env
from ringleader.sources.base import SplitStoreSource


def test_every_default_source_reads_inside_the_isolated_root(tmp_path, monkeypatch):
    world = tmp_path / "world"
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(world / "xdg-data"))
    for name, value in agent_store_env(world).items():
        monkeypatch.setenv(name, value)

    roots = {
        source.name: source.store_roots if isinstance(source, SplitStoreSource) else (source.root,)
        for source in scanner._new_sources()
    }

    assert {name: [r.is_relative_to(world) for r in rs] for name, rs in roots.items()} == {
        name: [True] * len(rs) for name, rs in roots.items()
    }


def test_fixture_scans_trust_managed_settings_and_subagents_inside_its_boundary(
    tmp_path, monkeypatch
):
    import json
    from pathlib import Path

    from ringleader import memory_scan

    root = tmp_path / "world"
    project = root / "checkout"
    managed = root / "managed-memory"
    configured = root / "project-memory"
    local = project / ".claude" / "agent-memory-local" / "local-agent"
    project_agent = project / ".claude" / "agent-memory" / "project-agent"
    env = agent_store_env(root)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    user_agent = Path(env["CLAUDE_CONFIG_DIR"]) / "agent-memory" / "user-agent"
    for folder in (managed, configured, local, project_agent, user_agent):
        folder.mkdir(parents=True)
        (folder / "topic.md").write_text(folder.name)
    trust = root / "native-trust.json"
    trust.write_text(json.dumps({"projects": {str(project): {"hasTrustDialogAccepted": True}}}))
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"autoMemoryDirectory": str(configured)})
    )
    managed_settings = root / "managed-claude" / "managed-settings.json"
    managed_settings.parent.mkdir()
    managed_settings.write_text(json.dumps({"autoMemoryDirectory": str(managed)}))
    (root / "etc" / "codex").mkdir(parents=True)
    (root / "etc" / "codex" / "config.toml").write_text(f'sqlite_home = "{tmp_path / "outside"}"')

    def observe(project_roots=()):
        readers = memory_scan.new_sources(
            {}, claude_trust_file=trust, project_roots=project_roots, installed=lambda _: True
        )
        scan = memory_scan.scan(readers)
        sources = {source.name: source for source in readers}
        return scan, {
            sources[row.ref.source].read_selected(row.ref).content: row.scope
            for row in scan.records
        }

    result, scopes = observe((project,))
    assert scopes == {
        "managed-memory": "project",
        "local-agent": "project",
        "project-agent": "project",
        "user-agent": "global",
    }
    codex = next(store for store in result.stores if store.source == "codex")
    assert codex.state == "unsupported"
    assert "explicit memory.codex_sqlite_home" in codex.reason
    assert {status.source for status in result.statuses if status.status == "not-supported"} == {
        "cursor",
        "droid",
        "opencode",
        "pi",
    }

    managed_settings.write_text("{}")
    _, scopes = observe()
    assert scopes == {
        "project-memory": "project",
        "local-agent": "project",
        "project-agent": "project",
        "user-agent": "global",
    }
