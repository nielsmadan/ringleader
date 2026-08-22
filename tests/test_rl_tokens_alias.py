from ringleader.commands import tokens as tokens_cmd


def test_alias_forwards_to_usage_scoped_to_claude(monkeypatch):
    seen = {}

    def fake_cmd_usage(argv, prog="rl usage"):
        seen["argv"] = argv
        seen["prog"] = prog
        return 0

    monkeypatch.setattr(tokens_cmd, "cmd_usage", fake_cmd_usage)

    rc = tokens_cmd.cmd_tokens(["--days", "3", "--json"])

    assert rc == 0
    assert seen["argv"] == ["--source", "claude", "--days", "3", "--json"]
    assert seen["prog"] == "rl data tokens"
