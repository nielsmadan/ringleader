import os
import sqlite3
from dataclasses import replace

import pytest
from ringleader.portability import PreservationError
from ringleader.portability.snapshot import (
    SnapshotFile,
    SourceSnapshot,
    TransformOptions,
    database_snapshot,
    snapshot_files,
)

from .fixtures import conversation


def test_snapshot_captures_owned_bytes_and_bounds(tmp_path):
    source = tmp_path / "source.jsonl"
    attachment = tmp_path / "image.png"
    source.write_bytes(b"history\n")
    attachment.write_bytes(b"image")
    files = snapshot_files((("history", source), ("image", attachment)), max_bytes=13)
    source.unlink()
    attachment.unlink()
    assert files == (SnapshotFile("history", b"history\n"), SnapshotFile("image", b"image"))
    source.write_bytes(b"too large")
    with pytest.raises(PreservationError, match="byte limit"):
        snapshot_files((("history", source),), max_bytes=4)


def test_snapshot_retries_changed_bundle_and_fails_after_bound(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"initial")
    original_fstat = os.fstat
    calls = []

    def change_during_read(fd):
        calls.append(fd)
        if len(calls) % 2 == 0:
            source.write_bytes(b"changed" * len(calls))
        return original_fstat(fd)

    monkeypatch.setattr(os, "fstat", change_during_read)
    with pytest.raises(PreservationError, match="after 2 attempts"):
        snapshot_files((("history", source),), max_bytes=100, attempts=2)
    assert len(calls) == 4


def test_snapshot_identity_includes_branch_assets_metadata_and_transform():
    snapshot = SourceSnapshot(
        conversation(), (SnapshotFile("history", b"native"),), "pi-v3", (("cwd", "/work"),)
    )
    identity = snapshot.identity()
    assert identity == snapshot.identity()
    variants = (
        replace(snapshot, conversation=replace(snapshot.conversation, branch_id="other")),
        replace(snapshot, files=(SnapshotFile("history", b"different"),)),
        replace(snapshot, metadata=(("cwd", "/elsewhere"),)),
        replace(snapshot, format_profile="pi-v4"),
        replace(
            snapshot,
            conversation=replace(
                snapshot.conversation,
                attachments=(replace(snapshot.conversation.attachments[0], data=b"different"),),
            ),
        ),
    )
    assert (
        len(
            {
                identity,
                *(item.identity() for item in variants),
                snapshot.identity(TransformOptions(compact=True)),
            }
        )
        == 7
    )


def test_database_snapshot_holds_one_read_transaction(tmp_path):
    path = tmp_path / "sessions.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE messages (text TEXT)")
    writer.execute("INSERT INTO messages VALUES ('before')")
    writer.commit()
    with database_snapshot(path) as reader:
        assert reader.execute("SELECT * FROM messages").fetchall() == [("before",)]
        writer.execute("INSERT INTO messages VALUES ('after')")
        writer.commit()
        assert reader.execute("SELECT * FROM messages").fetchall() == [("before",)]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM messages")
    with database_snapshot(path) as reader:
        assert reader.execute("SELECT * FROM messages").fetchall() == [("before",), ("after",)]
    writer.close()
