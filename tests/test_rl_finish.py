import json

from ringleader.commands import finish


def _registry(tmp_path, decision):
    p = tmp_path / "registry.json"
    p.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [
                    {
                        "sessionId": "s1",
                        "file": str(tmp_path / "t.jsonl"),
                        "decision": decision,
                    }
                ],
                "archived": [],
                "deleted": [],
            }
        )
    )
    return p


def test_finish_marks_a_harvested_thread(tmp_path):
    reg = _registry(tmp_path, "harvested")
    assert finish.cmd_finish(["s1", "--registry", str(reg)]) == 0
    assert json.loads(reg.read_text())["threads"][0]["finishOnExit"] is True


def test_finish_refuses_an_unharvested_thread(tmp_path, capsys):
    reg = _registry(tmp_path, None)
    assert finish.cmd_finish(["s1", "--registry", str(reg)]) == 1
    assert "finishOnExit" not in json.loads(reg.read_text())["threads"][0]
    assert "--force" in capsys.readouterr().err


def test_force_marks_an_unharvested_thread(tmp_path):
    reg = _registry(tmp_path, None)
    assert finish.cmd_finish(["s1", "--registry", str(reg), "--force"]) == 0
    assert json.loads(reg.read_text())["threads"][0]["finishOnExit"] is True
