import os
import sys
import types
from contextlib import closing

import pytest
from ringleader.analysis import embed, issues, store
from ringleader.commands import analyze

from .factories import bash, fail
from .test_rollups import A, B, C, put, scope, specs


@pytest.fixture
def conn(tmp_path):
    opened, _cold = store.open_index_for_write(tmp_path / "idx", "2026-10-18T12:00:00+00:00")
    with closing(opened):
        yield opened


SIG = "make|exit_code|Error in <path>:<n>"
OTHER = "pytest|runtime_error|Boom <uuid>"


def failure(call_id, signature=SIG, text="Exit code 1", **extra):
    return fail(call_id=call_id, signature=signature, error_text=text, **extra)


def found(conn, **options):
    base = {"embedder": None, "names": {}, "min_sessions": 2, "limit": 20}
    scope_ = options.pop("scope_", None) or scope()
    return issues.recurring_issues(conn, scope_, **{**base, **options})


class FakeEmbedder:
    def __init__(self, vectors):
        self.vectors = vectors

    def encode(self, texts):
        return [self.vectors[t] for t in texts]


def test_errors_differing_only_in_path_line_and_uuid_form_one_issue_across_sessions(conn):
    put(conn, "a", session=A, tool_calls=specs("a", failure("a1", text="x /tmp/a.py:3")))
    put(conn, "b", session=B, tool_calls=specs("b", failure("b1", text="x /tmp/b.py:9")))
    (issue,) = found(conn)
    assert (issue.sessions, issue.occurrences, issue.signatures) == (2, 2, (SIG,))
    assert issue.agents == ("claude-code",)
    assert issue.example_sessions == (("claude-code", A), ("claude-code", B))


def test_a_session_lists_an_issue_it_shares_only_with_an_archived_session(conn, monkeypatch):
    def refuse(**_kwargs):
        raise AssertionError("the embed model was loaded")

    monkeypatch.setattr(embed, "load_embedder", refuse)
    put(conn, "a", session=A, tool_calls=specs("a", failure("a1")))
    put(conn, "b", session=B, archived=True, tool_calls=specs("b", failure("b1")))
    (issue,) = found(conn, scope_=scope(include_archived=True))
    assert analyze._session_issues(conn, "claude-code", A) == (issue.issue_id,)


def test_the_same_error_in_a_session_and_its_fork_copy_is_one_session(conn):
    shared = specs("dup", failure("dup1"))
    put(conn, "primary", session=A, tool_calls=shared)
    put(conn, "fork", session=B, tool_calls=shared)
    assert found(conn) == []
    (issue,) = found(conn, min_sessions=1)
    assert (issue.sessions, issue.occurrences) == (1, 1)


def test_a_signature_seen_in_one_session_is_not_an_issue(conn):
    put(conn, "a", session=A, tool_calls=specs("a", failure("a1"), failure("a2")))
    assert found(conn) == []


def test_successful_calls_never_form_issues(conn):
    put(conn, "a", session=A, tool_calls=specs("a", bash("ls")))
    put(conn, "b", session=B, tool_calls=specs("b", bash("ls")))
    assert found(conn) == []


def test_user_caused_errors_still_form_issues_and_their_category_is_in_the_signature(conn):
    signature = "Edit|user_rejected|"
    put(conn, "a", session=A, tool_calls=specs("a", failure("a1", signature)))
    put(conn, "b", session=B, tool_calls=specs("b", failure("b1", signature)))
    (issue,) = found(conn)
    assert issue.signatures == (signature,)


def test_issues_rank_by_sessions_then_occurrences(conn):
    put(conn, "a", session=A, tool_calls=specs("a", failure("a1"), failure("a2", OTHER)))
    put(conn, "b", session=B, tool_calls=specs("b", failure("b1"), failure("b2", OTHER)))
    put(conn, "c", session=C, tool_calls=specs("c", failure("c1")))
    put(
        conn,
        "d",
        session=A + "d",
        tool_calls=specs("d", failure("d1", OTHER), failure("d2", OTHER)),
    )
    ranked = found(conn)
    assert [(i.signatures, i.sessions, i.occurrences) for i in ranked] == [
        ((OTHER,), 3, 4),
        ((SIG,), 3, 3),
    ]
    assert len(found(conn, limit=1)) == 1


def test_issue_id_and_content_hash_are_stable_and_names_attach(conn):
    put(conn, "a", session=A, tool_calls=specs("a", failure("a1", text="one")))
    put(conn, "b", session=B, tool_calls=specs("b", failure("b1", text="two")))
    (first,) = found(conn)
    assert first.issue_id == issues.issue_id([SIG])
    assert len(first.issue_id) == len(first.content_hash) == issues.ID_CHARS
    (named,) = found(conn, names={first.issue_id: ("Make breaks", "make fails")})
    assert (named.name, named.summary) == ("Make breaks", "make fails")
    assert named.content_hash == first.content_hash


def test_issue_id_ignores_signature_order():
    assert issues.issue_id(["b", "a"]) == issues.issue_id(["a", "b"])


def two_signature_corpus(conn):
    put(conn, "a", session=A, tool_calls=specs("a", failure("a1"), failure("a2", OTHER)))
    put(conn, "b", session=B, tool_calls=specs("b", failure("b1"), failure("b2", OTHER)))


def test_an_embedder_at_cosine_095_merges_two_signatures(conn):
    two_signature_corpus(conn)
    close = FakeEmbedder({SIG: [1.0, 0.0], OTHER: [0.95, (1 - 0.95**2) ** 0.5]})
    (merged,) = found(conn, embedder=close)
    assert merged.signatures == tuple(sorted((SIG, OTHER)))
    assert (merged.sessions, merged.occurrences) == (2, 4)


@pytest.mark.parametrize("other", [[0.9, (1 - 0.9**2) ** 0.5], [0.0, 1.0]])
def test_an_embedder_at_cosine_090_or_orthogonal_keeps_them_apart(conn, other):
    two_signature_corpus(conn)
    apart = FakeEmbedder({SIG: [1.0, 0.0], OTHER: other})
    assert sorted(i.signatures for i in found(conn, embedder=apart)) == [(SIG,), (OTHER,)]


def test_a_zero_vector_never_merges():
    zero = FakeEmbedder({"a": [0.0, 0.0], "b": [0.0, 0.0]})
    assert issues.merge_signatures(["a", "b"], zero) == issues.Merge([["a"], ["b"]], None)


def test_signatures_beyond_the_embed_limit_stay_separate(monkeypatch):
    monkeypatch.setattr(issues, "MAX_EMBED_SIGNATURES", 2)
    same = FakeEmbedder({"a": [1.0], "b": [1.0], "c": [1.0]})
    assert issues.merge_signatures(["a", "b", "c"], same).groups == [["a", "b"], ["c"]]


def test_the_scope_filters_what_counts(conn):
    put(conn, "a", session=A, tool_calls=specs("a", failure("a1")))
    put(conn, "b", session=B, tool_calls=specs("b", failure("b1")), archived=True)
    assert (
        issues.recurring_issues(conn, scope(), embedder=None, names={}, min_sessions=2, limit=5)
        == []
    )
    assert (
        len(
            issues.recurring_issues(
                conn, scope(include_archived=True), embedder=None, names={}, min_sessions=2, limit=5
            )
        )
        == 1
    )


def test_load_embedder_reports_a_missing_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "model2vec", None)
    assert embed.load_embedder(allow_download=False) == embed.EmbedLoad(None, embed.NOT_INSTALLED)


def fake_model2vec(monkeypatch, from_pretrained):
    module = types.ModuleType("model2vec")
    module.StaticModel = types.SimpleNamespace(from_pretrained=from_pretrained)
    monkeypatch.setitem(sys.modules, "model2vec", module)


def test_a_model_that_is_not_downloaded_gives_the_download_reason(monkeypatch):
    seen = []

    def from_pretrained(name):
        seen.append(os.environ.get("HF_HUB_OFFLINE"))
        raise FileNotFoundError("not cached")

    fake_model2vec(monkeypatch, from_pretrained)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    result = embed.load_embedder(allow_download=False)
    assert result == embed.EmbedLoad(None, embed.NOT_DOWNLOADED)
    assert (seen, os.environ.get("HF_HUB_OFFLINE")) == (["1"], None)


def test_allow_download_loads_without_the_offline_env(monkeypatch):
    class Encoded:
        def tolist(self):
            return [[1.0, 0.0]]

    class Model:
        def encode(self, texts):
            assert texts == ["x"]
            return Encoded()

    seen = []

    def from_pretrained(name):
        seen.append((name, os.environ.get("HF_HUB_OFFLINE")))
        return Model()

    fake_model2vec(monkeypatch, from_pretrained)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    result = embed.load_embedder(allow_download=True)
    assert seen == [(embed.EMBED_MODEL, None)]
    assert result.reason is None
    assert result.embedder.encode(["x"]) == [[1.0, 0.0]]
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_a_download_failure_names_the_error(monkeypatch):
    def from_pretrained(name):
        raise OSError("network down")

    fake_model2vec(monkeypatch, from_pretrained)
    result = embed.load_embedder(allow_download=True)
    assert result.reason == "embed model failed to load: network down"


def test_a_load_failure_without_download_names_the_error(monkeypatch):
    def from_pretrained(name):
        raise ValueError("bad weights")

    fake_model2vec(monkeypatch, from_pretrained)
    result = embed.load_embedder(allow_download=False)
    assert result.reason == "embed model failed to load: bad weights"


def test_a_broken_model2vec_import_degrades(monkeypatch):
    class Broken(types.ModuleType):
        def __getattr__(self, name):
            raise RuntimeError("abi mismatch")

    monkeypatch.setitem(sys.modules, "model2vec", Broken("model2vec"))
    result = embed.load_embedder(allow_download=False)
    assert result.reason == "embed model failed to load: abi mismatch"


def test_the_hub_offline_constant_follows_the_load_and_is_restored(monkeypatch):
    constants = types.ModuleType("huggingface_hub.constants")
    constants.HF_HUB_OFFLINE = False
    monkeypatch.setitem(sys.modules, "huggingface_hub.constants", constants)
    seen = []

    def from_pretrained(name):
        seen.append(constants.HF_HUB_OFFLINE)
        raise FileNotFoundError(name)

    fake_model2vec(monkeypatch, from_pretrained)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    embed.load_embedder(allow_download=False)
    assert (seen, constants.HF_HUB_OFFLINE) == ([True], False)


def test_an_embedder_that_fails_to_encode_leaves_exact_signatures():
    class Failing:
        def encode(self, texts):
            raise RuntimeError("boom")

    assert issues.merge_signatures(["a", "b"], Failing()) == issues.Merge(
        [["a"], ["b"]], "embedding failed: boom"
    )


def test_an_embedder_returning_too_few_vectors_leaves_exact_signatures():
    short = FakeEmbedder({"a": [1.0], "b": [1.0]})
    short.encode = lambda texts: [[1.0]]
    assert issues.merge_signatures(["a", "b"], short) == issues.Merge(
        [["a"], ["b"]], "embedder returned 1 vectors for 2 signatures"
    )


def test_find_issues_reports_the_embed_failure(conn):
    two_signature_corpus(conn)

    class Failing:
        def encode(self, texts):
            raise RuntimeError("boom")

    result = issues.find_issues(
        conn, scope(), embedder=Failing(), names={}, min_sessions=2, limit=5
    )
    assert sorted(i.signatures for i in result.issues) == [(SIG,), (OTHER,)]
    assert result.embed_failure == "embedding failed: boom"


def test_a_missing_file_while_downloading_names_the_error(monkeypatch):
    def from_pretrained(name):
        raise FileNotFoundError("missing shard")

    fake_model2vec(monkeypatch, from_pretrained)
    result = embed.load_embedder(allow_download=True)
    assert result.reason == "embed model failed to load: missing shard"


def test_two_threads_of_one_codex_session_count_as_one_session(conn):
    put(
        conn,
        "a",
        session=A,
        agent="codex",
        tool_calls=specs("a", failure("a1"), failure("a2")),
    )
    put(
        conn,
        "a2",
        session=A,
        agent="codex",
        thread="spawn-1",
        tool_calls=specs("a2", failure("a3")),
    )
    put(conn, "b", session=B, agent="codex", tool_calls=specs("b", failure("b1")))
    (issue,) = found(conn)
    assert (issue.sessions, issue.occurrences, issue.agents) == (2, 4, ("codex",))


def test_an_issue_keeps_the_earliest_sessions_as_its_examples(conn):
    sessions = [f"{i}{i}{i}{i}{i}{i}{i}{i}-2222-3333-4444-555555555555" for i in range(5)]
    minutes = (40, 10, 30, 20, 50)
    for sid, minute in zip(sessions, minutes, strict=True):
        at = f"2026-10-01T10:{minute}:00Z"
        put(conn, sid, session=sid, tool_calls=specs(sid, failure(sid, timestamp=at)))
    (issue,) = found(conn)
    assert issue.sessions == 5
    assert issue.example_sessions == tuple(("claude-code", sessions[i]) for i in (1, 3, 2))
