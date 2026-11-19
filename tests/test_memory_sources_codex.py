import fcntl
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from ringleader import harvests, memory_operations, registry
from ringleader.memory_sources import codex_artifacts as artifacts

from memory_helpers import (
    GLOBAL,
    KEY,
    NOW,
    OTHER,
    OTHER_SECTION,
    SECTION,
    STAGE_RETRY_AT,
    STEM,
    SUMMARY,
    codex_world,
)


def stage1_claim(db, revision):
    current = db.execute(
        "SELECT source_updated_at FROM stage1_outputs WHERE thread_id=?", (KEY,)
    ).fetchone()
    prior = db.execute(
        "SELECT last_success_watermark FROM jobs WHERE kind='memory_stage1' AND job_key=?", (KEY,)
    ).fetchone()
    if (current and current[0] >= revision) or (
        prior and prior[0] is not None and prior[0] >= revision
    ):
        return "skipped-up-to-date"
    statement = (Path(__file__).parent / "fixtures/codex_stage1_claim.sql").read_text()
    kind = "memory_stage1"
    stamp = int(NOW.timestamp()) + 1
    claimed = db.execute(
        statement,
        (
            kind,
            KEY,
            "claim-worker",
            "claim-owner",
            stamp,
            stamp + 60,
            3,
            revision,
            kind,
            stamp,
            1,
            3,
            1,
        ),
    )
    return "claimed" if claimed.rowcount else "not-claimed"


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("stage", [True, False])
def test_native_removal_suppression_consolidation_and_restore(tmp_path, version, stage):
    w, root, path = codex_world(tmp_path, version=version, stage=stage)
    original_raw = (root / "raw_memories.md").read_bytes()
    out = w.decide("approved")
    assert (out.state, out.cleanup) == ("removed", "pending"), out
    assert {f.name for f in (root / "rollout_summaries").iterdir()} == {
        STEM.replace("-0001-", "-0002-") + ".md"
    }
    assert (root / "raw_memories.md").read_text() == (
        artifacts.HEADER + OTHER_SECTION if version == "v1" else "user v2 derived file"
    )
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT * FROM consolidation_progress").fetchall() == [(1, 7)]
        assert db.execute(
            "SELECT thread_id,raw_memory,usage_count FROM stage1_outputs"
        ).fetchall() == [(OTHER, "unrelated", 12)]
        suppression = db.execute(
            "SELECT status,last_success_watermark,worker_id,ownership_token,started_at,retry_at,retry_remaining FROM jobs WHERE kind='memory_stage1'"
        ).fetchone()
        assert (
            suppression == ("done", 100, "worker", "owner", 65, STAGE_RETRY_AT, 0)
            if stage
            else suppression == ("done", 100, None, None, None, None, 3)
        )
        target, prior, finished, remaining, status = db.execute(
            "SELECT input_watermark,last_success_watermark,finished_at,retry_remaining,status FROM jobs WHERE kind=?",
            (GLOBAL,),
        ).fetchone()
        assert target > max(prior, 110) and (finished, remaining, status) == (85, 3, "pending")
        assert stage1_claim(db, 100) == "skipped-up-to-date"
        assert stage1_claim(db, 101) == "claimed"
        assert db.execute(
            "SELECT status, input_watermark, retry_remaining FROM jobs WHERE job_key=?", (KEY,)
        ).fetchone() == ("running", 101, 3)
        db.rollback()
        db.execute("UPDATE jobs SET status='done' WHERE kind=?", (GLOBAL,))
        db.commit()
        assert w.recover(out.operation_id).cleanup == "pending"
        db.execute("UPDATE jobs SET status='error' WHERE kind=?", (GLOBAL,))
        db.commit()
        assert w.recover(out.operation_id).cleanup == "failed"
        db.execute(
            "UPDATE jobs SET status='done',last_success_watermark=? WHERE kind=?", (target, GLOBAL)
        )
        db.commit()
        assert w.recover(out.operation_id).cleanup == "completed"
        db.execute(
            "UPDATE jobs SET input_watermark=125,last_success_watermark=125 WHERE kind='memory_stage1'"
        )
        db.commit()
        assert w.recover(out.operation_id, restore=True).state == "restored"
        assert db.execute("SELECT * FROM consolidation_progress").fetchall() == [(1, 7)]
        assert db.execute(
            "SELECT usage_count,selected_for_phase2,raw_memory FROM stage1_outputs WHERE thread_id=?",
            (KEY,),
        ).fetchone() == (12, 1, "source text")
        assert db.execute(
            "SELECT last_success_watermark FROM jobs WHERE job_key=?", (KEY,)
        ).fetchone() == (125,)
        assert (
            db.execute("SELECT input_watermark FROM jobs WHERE kind=?", (GLOBAL,)).fetchone()[0]
            > target
        )
    assert (root / "raw_memories.md").read_bytes() == original_raw
    assert (root / "rollout_summaries" / (STEM + ".md")).read_text() == SUMMARY
    assert (root / "MEMORY.md").read_text() == "derived output" and (
        root / ".git/HEAD"
    ).read_text() == "native baseline"


@pytest.mark.parametrize("block", ["workspace", "database", "extraction", "consolidation", "newer"])
def test_jobs_and_locks_are_durable_pending_and_recover(tmp_path, block):
    w, root, path = codex_world(tmp_path)
    lock = root.with_suffix(".lock")
    lock.touch()
    with lock.open("rb") as handle, closing(sqlite3.connect(path)) as db:
        if block == "workspace":
            fcntl.flock(handle, fcntl.LOCK_EX)
        elif block == "database":
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE stage1_outputs SET usage_count=25 WHERE thread_id=?", (KEY,))
            assert path.with_name(path.name + "-journal").is_file()
        elif block == "newer":
            db.execute(
                "UPDATE jobs SET status='pending',input_watermark=101 WHERE job_key=?", (KEY,)
            )
            db.commit()
        else:
            db.execute(
                "UPDATE jobs SET status='running',lease_until=99999999999 WHERE job_key=?",
                ("global" if block == "consolidation" else KEY,),
            )
            db.commit()
        out = w.decide("approved")
        assert out.state == "removal-pending", out
        assert (
            registry.load_registry(w.path)["memoryDecisions"][out.operation_id]["sourceState"]
            == "removal-pending"
        )
        assert harvests.snapshot_harvests().items[0].content == "approved"
        if block == "database":
            retry = w.recover(out.operation_id)
            assert retry.state == "removal-pending" and "rollback journal" in retry.message
        assert db.execute(
            "SELECT raw_memory FROM stage1_outputs WHERE thread_id=?", (KEY,)
        ).fetchone() == ("source text",)
        db.rollback()
        fcntl.flock(handle, fcntl.LOCK_UN)
        db.execute("UPDATE jobs SET status='done',lease_until=NULL,input_watermark=100")
        db.commit()
    assert w.recover(out.operation_id).state == "removed"
    assert lock.exists()


@pytest.mark.parametrize("key", [KEY, OTHER, "global"])
@pytest.mark.parametrize("lease", ["expired", "boundary", "missing"])
def test_expired_native_job_leases_allow_removal(tmp_path, key, lease):
    w, _, path = codex_world(tmp_path)
    clock = NOW + timedelta(days=365)
    expiry = {"expired": 10, "boundary": int(clock.timestamp()), "missing": None}[lease]
    with closing(sqlite3.connect(path)) as db:
        if key == OTHER:
            db.execute(
                "INSERT INTO jobs VALUES ('memory_stage1',?,'running','other','owner',1,NULL,?,NULL,3,NULL,100,NULL)",
                (key, expiry),
            )
        else:
            db.execute(
                "UPDATE jobs SET status='running',lease_until=? WHERE job_key=?", (expiry, key)
            )
        db.commit()
    outcome = w.decide(decision="discard", now=clock)
    assert outcome.state == "removed", outcome
    if key == OTHER:
        with closing(sqlite3.connect(path)) as db:
            assert db.execute(
                "SELECT status,lease_until FROM jobs WHERE job_key=?", (key,)
            ).fetchone() == (
                "running",
                expiry,
            )


def test_replayed_codex_intent_uses_current_lease_clock_without_retargeting(tmp_path, monkeypatch):
    w, _, path = codex_world(tmp_path)
    write = memory_operations.Operation.write

    def crash(op):
        write(op)
        if op.payload.get("intent") and not op.progress:
            raise InterruptedError("crash after journaling native intent")

    with monkeypatch.context() as mp:
        mp.setattr(memory_operations.Operation, "write", crash)
        first = w.decide(decision="discard")
    assert first.state == "failed"
    target = int(NOW.timestamp())
    deadline = NOW + timedelta(seconds=10)
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "INSERT INTO jobs VALUES ('memory_stage1',?,'running','other','owner',1,NULL,?,NULL,3,NULL,100,NULL)",
            (OTHER, int(deadline.timestamp())),
        )
        db.commit()
    assert w.recover(first.operation_id, now=NOW + timedelta(seconds=1)).state == "removal-pending"
    assert w.recover(first.operation_id, now=deadline).state == "removed"
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            "SELECT input_watermark FROM jobs WHERE kind=?", (GLOBAL,)
        ).fetchone() == (target,)


@pytest.mark.parametrize("column", ["input_watermark", "last_success_watermark"])
def test_consolidation_watermark_overflow_refuses_before_native_file_changes(tmp_path, column):
    w, root, path = codex_world(tmp_path)
    max_sqlite_integer = (1 << 63) - 1
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"UPDATE jobs SET {column}=? WHERE kind=?", (max_sqlite_integer, GLOBAL))
        db.commit()
    summary = root / "rollout_summaries" / (STEM + ".md")
    raw = root / "raw_memories.md"
    expected = summary.read_bytes(), raw.read_bytes()
    outcome = w.decide("approved despite maximum native watermark")
    assert outcome.state == "refused" and "watermark cannot be incremented" in outcome.message
    assert (summary.read_bytes(), raw.read_bytes()) == expected
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            "SELECT raw_memory FROM stage1_outputs WHERE thread_id=?", (KEY,)
        ).fetchone() == ("source text",)
    op = memory_operations.load(outcome.operation_id, w.path)
    assert op.progress == [] and set(op.payload) == {"snapshot"}
    assert w.recover(op.id).state == "refused"
    assert (
        harvests.snapshot_harvests().items[0].content == "approved despite maximum native watermark"
    )


@pytest.mark.parametrize("change", ["version", "schema", "summary", "raw", "symlink", "revision"])
def test_unverified_native_target_preserves_every_component(tmp_path, change):
    w, root, path = codex_world(tmp_path)
    file = root / "rollout_summaries" / (STEM + ".md")
    if change == "version":
        w.service.sources = lambda _: [replace(w.source, version_probe=lambda: "codex-cli 0.168.0")]
    elif change == "summary":
        file.write_text(SUMMARY + "unknown")
    elif change == "raw":
        (root / "raw_memories.md").write_text("unknown layout")
    elif change == "symlink":
        file.unlink()
        file.symlink_to(root / "MEMORY.md")
    else:
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN extra"
                if change == "schema"
                else "UPDATE stage1_outputs SET source_updated_at=110"
            )
            db.commit()
    out = w.decide("approved")
    assert out.state in ("refused", "failed"), out
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT thread_id FROM stage1_outputs ORDER BY thread_id").fetchall() == [
            (KEY,),
            (OTHER,),
        ]
    assert file.exists()


@pytest.mark.parametrize("action", ["remove", "restore"])
@pytest.mark.parametrize("component", ["summary", "raw", "database"])
def test_resume_each_codex_component(tmp_path, monkeypatch, action, component):
    w, root, path = codex_world(tmp_path)
    op = w.decide(decision="discard").operation_id if action == "restore" else None
    original = memory_operations.Operation.component

    def stop(self, name):
        if name == component:
            raise InterruptedError("injected checkpoint failure")
        original(self, name)

    with monkeypatch.context() as mp:
        mp.setattr(memory_operations.Operation, "component", stop)
        out = w.recover(op, restore=True) if op else w.decide("approved before native effects")
        assert out.state == "failed", out
    again = w.recover(out.operation_id)
    assert again.state == ("removed" if action == "remove" else "restored"), again
    with closing(sqlite3.connect(path)) as db:
        keys = db.execute("SELECT thread_id FROM stage1_outputs ORDER BY thread_id").fetchall()
        assert keys == ([(OTHER,)] if action == "remove" else [(KEY,), (OTHER,)])
    assert (root / "MEMORY.md").read_text() == "derived output"


def test_native_workspace_lock_is_cross_process_and_keeps_inode(tmp_path):
    w, root, _ = codex_world(tmp_path)
    lock = root.with_suffix(".lock")
    code = (
        "import sys,fcntl; h=open(sys.argv[1],'ab'); fcntl.flock(h,fcntl.LOCK_EX | fcntl.LOCK_NB)"
    )
    with w.source.mutation(w.source.scan().records[0].ref):
        original = lock.stat().st_ino
        result = subprocess.run(
            [sys.executable, "-c", code, str(lock)], check=False, capture_output=True, timeout=5
        )
        assert result.returncode != 0 and b"BlockingIOError" in result.stderr
    result = subprocess.run(
        [sys.executable, "-c", code, str(lock)], check=False, capture_output=True, timeout=5
    )
    assert result.returncode == 0 and lock.stat().st_ino == original


def test_wal_revision_is_visible_to_native_mutation(tmp_path):
    w, _, path = codex_world(tmp_path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE stage1_outputs SET usage_count=40 WHERE thread_id=?", (KEY,))
        writer.commit()
        assert w.decide(decision="discard").state == "removed"
    assert registry.load_registry(w.path)["memoryEntries"][0]["sourceRef"] == OTHER


def test_restore_checks_native_artifact_grammar_before_opening_writable_database(
    tmp_path, monkeypatch
):
    w, root, path = codex_world(tmp_path)
    op = w.decide(decision="discard").operation_id
    (root / "raw_memories.md").write_text("unrecognized native input")
    connect = sqlite3.connect

    def guarded(database, *args, **kwargs):
        if database == path.as_uri() + "?mode=rw":
            pytest.fail("opened native writable database before validating artifact format")
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded)
    result = w.recover(op, restore=True)
    assert result.state == "refused" and "raw memories header" in result.message


def test_cleanup_observation_refuses_unknown_or_recreated_native_inputs(tmp_path):
    w, root, path = codex_world(tmp_path)
    op = w.decide(decision="discard").operation_id
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "UPDATE jobs SET status='done',last_success_watermark=input_watermark WHERE kind=?",
            (GLOBAL,),
        )
        db.commit()
    raw = root / "raw_memories.md"
    original = raw.read_text()
    raw.write_text(artifacts.HEADER + "unknown section\n")
    assert w.recover(op).cleanup == "unknown"
    raw.write_text(original)
    renamed = root / "rollout_summaries" / "renamed.md"
    renamed.write_text(SUMMARY)
    assert w.recover(op).cleanup == "unknown"
    renamed.unlink()
    assert w.recover(op).cleanup == "completed"


def test_v1_restore_inserts_between_new_unrelated_sections(tmp_path):
    w, root, path = codex_world(tmp_path)
    op = w.decide(decision="discard")
    assert op.state == "removed"
    earlier = KEY[:-1] + "0"
    section = SECTION.replace(KEY, earlier).replace("-0001-", "-0000-")
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "INSERT INTO stage1_outputs VALUES (?,100,'source text','summary text','Native Slug',105,0,0,0,0)",
            (earlier,),
        )
        db.commit()
    raw = root / "raw_memories.md"
    raw.write_text(artifacts.HEADER + section + OTHER_SECTION)
    assert w.recover(op.operation_id, restore=True).state == "restored"
    assert raw.read_bytes() == (artifacts.HEADER + section + SECTION + OTHER_SECTION).encode()


@pytest.mark.parametrize("attempt", ["approve", "pending", "restore", "observe"])
def test_shared_sqlite_home_requires_reviewed_workspace_before_presence_or_writes(
    tmp_path, monkeypatch, attempt
):
    from ringleader.presence import PresenceResult

    w, root, path = codex_world(tmp_path)
    if attempt == "pending":
        w.presence = replace(w.quiet, sessions=PresenceResult(available=False))
    op = w.decide(decision="discard") if attempt != "approve" else None
    new_home = tmp_path / "second-profile"
    (new_home / root.name).mkdir(parents=True)
    w.service.sources = lambda _: [replace(w.source, codex_home=new_home)]
    w.service.presence = lambda _: pytest.fail("checked the wrong profile's presence")
    connect = sqlite3.connect

    def readonly(file, *args, **kwargs):
        assert file != path.as_uri() + "?mode=rw"
        return connect(file, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", readonly)
    if attempt == "observe":
        assert w.recover(op.operation_id).cleanup == "unknown"
    else:
        outcome = (
            w.recover(op.operation_id, restore=attempt == "restore")
            if op
            else w.decide(decision="discard")
        )
        assert outcome.state == "refused" and "workspace" in outcome.message
    assert list((new_home / root.name).iterdir()) == []
    assert sorted(p.name for p in new_home.iterdir()) == [root.name]


def test_selected_codex_reads_one_key(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from ringleader.memory_sources import codex

    w, _, path = codex_world(tmp_path)
    snapshot = codex.open_readonly
    queries, stores = [], []

    @contextmanager
    def observed(file, *args, **kwargs):
        with snapshot(file, *args, **kwargs) as (db, identity):
            stores.append(file)
            db.set_trace_callback(queries.append)
            yield db, identity

    monkeypatch.setattr(codex, "open_readonly", observed)
    assert "source text" in w.source.read_selected(w.ref).content
    assert stores == [path]
    assert [q.partition(" FROM ")[2] for q in queries if " FROM stage1_outputs" in q] == [
        f"stage1_outputs WHERE thread_id='{KEY}'"
    ]


def test_raw_validation_reads_unrelated_sections_in_bounded_batches(tmp_path):
    from ringleader.memory_sources.codex_rows import StageRow

    _, _, path = codex_world(tmp_path)
    keys = [f"00000000-0000-4000-8000-{n:012x}" for n in range(3, 72)]
    retained = OTHER_SECTION + "".join(
        SECTION.replace(KEY, key).replace(STEM, artifacts.stem(key, 100, "Native Slug"))
        for key in keys
    )
    queries, columns = [], set()
    with closing(sqlite3.connect(path)) as db:
        db.executemany(
            "INSERT INTO stage1_outputs "
            "(thread_id,source_updated_at,raw_memory,rollout_summary,rollout_slug,generated_at) "
            "VALUES (?,100,'source text','summary text','Native Slug',105)",
            [(key,) for key in keys],
        )
        selected = StageRow.decode(
            db.execute("SELECT * FROM stage1_outputs WHERE thread_id=?", (KEY,)).fetchone()
        )

        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == "stage1_outputs":
                columns.add(column)
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        db.set_trace_callback(queries.append)
        selected_text, after = artifacts.raw_section(
            artifacts.HEADER + SECTION + retained, selected, db
        )

    assert (selected_text, after) == (SECTION, artifacts.HEADER + retained)
    assert columns == {"thread_id", "source_updated_at", "raw_memory", "rollout_slug"}
    batches = [re.findall(r"'([0-9a-f-]{36})'", query) for query in queries]
    assert len(batches) == 3
    assert all(1 <= len(batch) <= 32 for batch in batches)
    assert sorted(key for batch in batches for key in batch) == [OTHER, *keys]


def test_summary_metadata_grammar_compares_exact_native_text(tmp_path):
    from ringleader.memory_sources.codex_rows import StageRow

    body = ("Unicode ✓ [abc]*.+? " * 4096).strip()
    row = StageRow(KEY, 100, "raw", body, "slug", 105, None, None, 0, None)
    summary = SUMMARY.replace("summary text", body)
    for text in (summary, summary.replace("git_branch: main\n", "")):
        artifacts.validate_summary(text, row)
    for changed in (
        summary + "\n",
        summary.replace(body[-5:], "moved"),
        summary.replace("cwd: /fixture", "cwd: "),
    ):
        with pytest.raises(ValueError, match="canonical rollout summary"):
            artifacts.validate_summary(changed, row)


def test_workspace_identity_changes_at_same_path_and_legacy_ref_requires_review(tmp_path):
    w, root, path = codex_world(tmp_path)
    old = root.with_name("old-workspace")
    root.rename(old)
    root.mkdir()
    with pytest.raises(ValueError, match="workspace cannot be verified"):
        w.source.read_selected(w.ref)
    root.rmdir()
    old.rename(root)
    legacy = replace(w.ref, incarnation=w.ref.incarnation.partition(":")[2])
    with pytest.raises(ValueError, match="refresh and review"), w.source.mutation(legacy):
        pytest.fail("unbound legacy revision reached a native transaction")
    assert (root / "rollout_summaries" / (STEM + ".md")).read_text() == SUMMARY
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            "SELECT raw_memory FROM stage1_outputs WHERE thread_id=?", (KEY,)
        ).fetchone() == ("source text",)


def test_raw_body_header_example_is_consumed_as_selected_content(tmp_path):
    from ringleader.memory_sources.codex_rows import StageRow

    _, _, path = codex_world(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        row = StageRow.decode(
            db.execute("SELECT * FROM stage1_outputs WHERE thread_id=?", (KEY,)).fetchone()
        )
        embedded = row._replace(
            raw_memory="example follows\n## Thread `" + OTHER + "`\nquoted text"
        )
        section = SECTION.replace("source text", embedded.raw_memory)
        assert artifacts.raw_section(artifacts.HEADER + section + OTHER_SECTION, embedded, db) == (
            section,
            artifacts.HEADER + OTHER_SECTION,
        )
        with pytest.raises(ValueError, match="duplicate"):
            artifacts.raw_section(artifacts.HEADER + section + section, embedded, db)
