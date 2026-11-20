from ringleader.commands import tokens as tokens_cmd


def test_alias_forwards_to_local_usage_scoped_to_claude(monkeypatch):
    seen = {}

    def fake_local_summary(argv, prog="rl usage"):
        seen["argv"] = argv
        seen["prog"] = prog
        return 0

    monkeypatch.setattr(tokens_cmd, "local_summary", fake_local_summary)

    rc = tokens_cmd.cmd_tokens(["--days", "3", "--json"])

    assert rc == 0
    assert seen["argv"] == ["--source", "claude", "--days", "3", "--json"]
    assert seen["prog"] == "rl data tokens"
