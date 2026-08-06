def test_config_roundtrip(tmp_path):
    from ringleader.config import Config, load_config, save_config

    p = tmp_path / "config.json"
    assert load_config(p) == Config()  # defaults when absent
    save_config(Config(backend="codex", model="gpt-x"), p)
    got = load_config(p)
    assert got.backend == "codex" and got.model == "gpt-x"
