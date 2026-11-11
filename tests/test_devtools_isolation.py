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
