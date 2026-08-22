def test_config_roundtrip(tmp_path):
    from ringleader.config import Config, load_config, save_config

    p = tmp_path / "config.json"
    assert load_config(p) == Config()  # defaults when absent
    save_config(Config(ai="codex", model="gpt-x", archive_retention_days=30), p)
    got = load_config(p)
    assert got.ai == "codex" and got.model == "gpt-x"
    assert got.archive_retention_days == 30
