from ringleader import memory


def test_parse_frontmatter_splits_meta_and_body():
    text = (
        "---\nname: foo\ndescription: a thing\nmetadata:\n  type: feedback\n---\n\nThe body line.\n"
    )
    meta, body = memory.parse_frontmatter(text)
    assert meta["name"] == "foo"
    assert meta["description"] == "a thing"
    assert meta["metadata"]["type"] == "feedback"
    assert body.strip() == "The body line."


def test_parse_frontmatter_no_frontmatter():
    meta, body = memory.parse_frontmatter("just a bullet\n")
    assert meta == {}
    assert body.strip() == "just a bullet"


def test_detect_obsolete_flags_fixed_and_status_snapshot():
    assert "fixed" in memory.detect_obsolete(
        "Bug X — FIXED 2026-06-10", repo_gone=False, repo_doc_text=None
    )
    assert "status-snapshot" in memory.detect_obsolete(
        "Status (as of 2026-06-23): 23 tests passing", repo_gone=False, repo_doc_text=None
    )


def test_detect_obsolete_repo_gone():
    assert "repo-gone" in memory.detect_obsolete("anything", repo_gone=True, repo_doc_text=None)


def test_detect_obsolete_already_in_doc():
    body = "Always run yarn check-all before done."
    doc = "# CLAUDE.md\n\nAlways run yarn check-all before done.\n"
    assert "already-in-doc" in memory.detect_obsolete(body, repo_gone=False, repo_doc_text=doc)


def test_detect_obsolete_clean_body_has_no_signals():
    assert (
        memory.detect_obsolete(
            "A durable convention about ports.", repo_gone=False, repo_doc_text="# unrelated\n"
        )
        == []
    )
