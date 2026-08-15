def test_notify_invokes_osascript(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "ringleader.notify.subprocess.run", lambda cmd, **k: seen.setdefault("cmd", cmd)
    )
    monkeypatch.setattr("ringleader.notify.sys.platform", "darwin")
    from ringleader import notify

    notify.notify("t", "m")
    assert "osascript" in seen["cmd"][0] or seen["cmd"][0] == "osascript"


def test_notify_noop_off_mac(monkeypatch):
    monkeypatch.setattr("ringleader.notify.sys.platform", "linux")
    from ringleader import notify

    notify.notify("t", "m")  # must not raise
