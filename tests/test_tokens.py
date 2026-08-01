import json

from ringleader import tokens


def _assistant(msg_id, request_id, model, ts, **usage):
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "requestId": request_id,
        "message": {"id": msg_id, "model": model, "usage": usage},
    })


def _write(dirpath, name, lines):
    f = dirpath / name
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def test_model_family():
    assert tokens.model_family("claude-opus-4-8") == "opus"
    assert tokens.model_family("claude-haiku-4-5-20251001") == "haiku"
    assert tokens.model_family("claude-sonnet-5") == "sonnet"
    assert tokens.model_family("claude-fable-5") == "fable"
    assert tokens.model_family("gpt-5.2-codex") is None
    assert tokens.model_family(None) is None


def test_pricing(tmp_path):
    proj = tmp_path / "-Users-x-wrksp-demo"
    proj.mkdir()
    # 1M output tokens on opus = $25
    _write(proj, "s1.jsonl", [
        _assistant("m1", "r1", "claude-opus-4-8", "2026-07-24T10:00:00Z",
                   output_tokens=1_000_000),
    ])
    cells = tokens.collect(root=tmp_path)
    total = sum(c.b.cost for c in cells.values())
    assert round(total, 2) == 25.00


def test_dedup(tmp_path):
    proj = tmp_path / "-Users-x-wrksp-demo"
    proj.mkdir()
    dup = _assistant("m1", "r1", "claude-opus-4-8", "2026-07-24T10:00:00Z",
                     output_tokens=1_000_000)
    # same (id, requestId) written twice (resume/summary rewrite) -> counted once
    _write(proj, "s1.jsonl", [dup, dup])
    cells = tokens.collect(root=tmp_path)
    assert sum(c.b.messages for c in cells.values()) == 1
    assert round(sum(c.b.cost for c in cells.values()), 2) == 25.00


def test_non_claude_skipped(tmp_path):
    proj = tmp_path / "-Users-x-wrksp-demo"
    proj.mkdir()
    _write(proj, "s1.jsonl", [
        _assistant("m1", "r1", "gpt-5.5", "2026-07-24T10:00:00Z",
                   output_tokens=1_000_000),
    ])
    assert tokens.collect(root=tmp_path) == {}


def test_buckets_by_date_and_project(tmp_path):
    a = tmp_path / "-Users-x-wrksp-aaa"
    b = tmp_path / "-Users-x-wrksp-bbb"
    a.mkdir()
    b.mkdir()
    _write(a, "s1.jsonl", [
        _assistant("m1", "r1", "claude-opus-4-8", "2026-07-23T10:00:00Z",
                   output_tokens=1_000_000),
        _assistant("m2", "r2", "claude-opus-4-8", "2026-07-24T10:00:00Z",
                   output_tokens=1_000_000),
    ])
    _write(b, "s2.jsonl", [
        _assistant("m3", "r3", "claude-opus-4-8", "2026-07-24T10:00:00Z",
                   output_tokens=1_000_000),
    ])
    cells = tokens.collect(root=tmp_path)
    by_day = tokens.rollup(cells, key=lambda c: c.date)
    assert round(by_day["2026-07-23"].cost, 2) == 25.00
    assert round(by_day["2026-07-24"].cost, 2) == 50.00
    by_proj = tokens.rollup(cells, key=lambda c: c.project)
    assert {p.rsplit("/", 1)[-1] for p in by_proj} == {"aaa", "bbb"}


def test_decode_strips_home(tmp_path, monkeypatch):
    monkeypatch.setattr(tokens.Path, "home", staticmethod(lambda: tokens.Path("/Users/x")))
    proj = tmp_path / "-Users-x-wrksp-demo"
    proj.mkdir()
    _write(proj, "s1.jsonl", [
        _assistant("m1", "r1", "claude-opus-4-8", "2026-07-24T10:00:00Z",
                   output_tokens=1),
    ])
    cells = tokens.collect(root=tmp_path)
    assert next(iter(cells.values())).project == "wrksp/demo"
