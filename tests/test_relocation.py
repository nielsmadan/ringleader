from __future__ import annotations

import os
from pathlib import Path

import pytest
from ringleader import relocation
from ringleader.slug import slugify


@pytest.fixture
def stores():
    roots = {
        name: Path(os.environ[var])
        for name, var in (
            ("claude", "SESSION_TRACKER_PROJECTS_DIR"),
            ("codex", "CODEX_SESSIONS_DIR"),
        )
    }
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    return roots


def test_a_claude_move_previews_its_new_transcript_path(stores):
    old = stores["claude"] / slugify("/w/api") / "s1.jsonl"
    thread = {"sessionId": "s1", "source": "claude-code", "file": str(old), "project": "/w/api"}

    planned = relocation.planned_storage(thread, "/w/here")

    assert planned == f"{old} → {stores['claude'] / slugify('/w/here') / 's1.jsonl'}"


def test_a_claude_session_already_filed_under_the_destination_says_so(stores):
    filed = stores["claude"] / slugify("/w/here") / "s1.jsonl"
    thread = {"sessionId": "s1", "source": "claude-code", "file": str(filed), "project": "/w/api"}

    assert (
        relocation.planned_storage(thread, "/w/here") == f"{filed} is already filed under /w/here"
    )


def test_a_codex_move_previews_a_rebind(stores):
    thread = {"sessionId": "c1", "source": "codex", "file": "/r/rollout.jsonl", "project": "/w/api"}

    assert relocation.planned_storage(thread, "/w/here") == "rebind codex's recorded cwd to /w/here"


@pytest.mark.parametrize(
    ("thread", "planned"),
    [
        (
            {"sessionId": "s1", "source": "claude-code", "project": "/w/api"},
            "no transcript recorded",
        ),
        (
            {"sessionId": "p1", "source": "pi", "file": "/p/s.jsonl", "project": "/w/api"},
            "its storage cannot follow a session",
        ),
    ],
)
def test_a_move_its_storage_cannot_make_previews_a_registry_only_move(stores, thread, planned):
    assert relocation.planned_storage(thread, "/w/here") == f"registry record only — {planned}"


def test_a_move_follows_storage_only_where_the_agent_can_take_it_along(stores):
    assert (relocation.storage_follows("claude-code"), relocation.storage_follows("pi")) == (
        True,
        False,
    )
