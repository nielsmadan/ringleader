import json

from ringleader.usage.claude import ClaudeUsageReader


def _write(dir_, project, session, records):
    d = dir_ / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _assistant(  # noqa: PLR0913 -- test data-builder helper, one arg per fixture field
    msg_id, req_id, *, inp=100, out=20, cr=500, cw=50, model="claude-opus-5"
):
    return {
        "type": "assistant",
        "requestId": req_id,
        "timestamp": "2026-08-17T10:00:00.000Z",
        "cwd": "/Users/x/wrksp/foo",
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cw,
            },
        },
    }


def test_maps_claude_buckets_as_disjoint(tmp_path):
    _write(tmp_path, "-Users-x-wrksp-foo", "sess1", [_assistant("m1", "r1")])

    recs = list(ClaudeUsageReader(projects_dir=tmp_path).records())

    assert len(recs) == 1
    r = recs[0]
    assert (r.input, r.output, r.cache_read, r.cache_write) == (100, 20, 500, 50)
    assert r.reasoning == 0  # Claude folds thinking into output
    assert r.total == 670  # all four buckets are disjoint
    assert r.source == "claude"
    assert r.session_id == "sess1"
    assert r.model == "claude-opus-5"


def test_dedupes_repeated_message_id_and_request_id(tmp_path):
    dup = _assistant("m1", "r1")
    _write(tmp_path, "-Users-x-wrksp-foo", "sess1", [dup, dup, _assistant("m2", "r2")])

    recs = list(ClaudeUsageReader(projects_dir=tmp_path).records())

    assert len(recs) == 2


def test_skips_non_claude_and_usageless_records(tmp_path):
    _write(
        tmp_path,
        "-Users-x-wrksp-foo",
        "sess1",
        [
            _assistant("m1", "r1", model="<synthetic>"),
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"id": "m9", "model": "claude-opus-5"}},
        ],
    )

    assert list(ClaudeUsageReader(projects_dir=tmp_path).records()) == []


def test_decodes_project_label(tmp_path):
    _write(tmp_path, "-Users-x-wrksp-foo", "sess1", [_assistant("m1", "r1")])

    r = next(iter(ClaudeUsageReader(projects_dir=tmp_path).records()))

    assert r.project == "/Users/x/wrksp/foo"  # taken from the record's cwd


def test_available_is_false_without_the_directory(tmp_path):
    assert ClaudeUsageReader(projects_dir=tmp_path / "nope").available() is False


def test_malformed_line_is_skipped(tmp_path):
    d = tmp_path / "-Users-x-wrksp-foo"
    d.mkdir(parents=True)
    (d / "sess1.jsonl").write_text(
        "{not json\n" + json.dumps(_assistant("m1", "r1")) + "\n", encoding="utf-8"
    )

    assert len(list(ClaudeUsageReader(projects_dir=tmp_path).records())) == 1


def test_project_fallback_is_an_absolute_path(tmp_path):
    rec = _assistant("m1", "r1")
    del rec["cwd"]
    _write(tmp_path, "-Users-x-wrksp-foo", "sess1", [rec])

    r = next(iter(ClaudeUsageReader(projects_dir=tmp_path).records()))

    assert r.project == "/Users/x/wrksp/foo"  # matches what the cwd branch yields


def test_record_id_pairs_message_id_with_request_id(tmp_path):
    _write(tmp_path, "-Users-x-wrksp-foo", "sess1", [_assistant("m1", "r1")])

    r = next(iter(ClaudeUsageReader(projects_dir=tmp_path).records()))

    assert r.record_id == "m1|r1"
