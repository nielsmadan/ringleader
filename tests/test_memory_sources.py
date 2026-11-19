import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from ringleader import memory_catalog, memory_scan, registry
from ringleader.memory_sources import codex
from ringleader.memory_sources.claude_code import ClaudeMemorySource, ClaudeRoots, inline_content
from ringleader.memory_sources.codex import FILES, CodexMemorySource, verified_schema
from ringleader.slug import slugify

from memory_helpers import add_row, database


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("mismatch", ["missing-migration", "missing-history", "altered-table"])
def test_incomplete_or_drifted_codex_migrations_are_readable_only(tmp_path, version, mismatch):
    path = tmp_path / FILES[version]
    with closing(database(path, migrations=1 if mismatch == "missing-migration" else 2)) as db:
        add_row(db)
        if mismatch == "missing-history":
            db.execute("DELETE FROM _sqlx_migrations WHERE version=2")
        elif mismatch == "altered-table":
            db.execute("ALTER TABLE consolidation_progress ADD COLUMN unexpected INTEGER")
        db.commit()
    scan = CodexMemorySource(tmp_path, tmp_path).scan()
    (record,) = scan.records
    assert "Unverified Codex memory schema" in record.mutation_block
    assert (
        next(store for store in scan.stores if store.store_id == str(path)).state == "unsupported"
    )


def test_versioned_codex_rows_and_state_in_separate_sqlite_home(tmp_path):
    root = tmp_path / "sqlite"
    source = CodexMemorySource(tmp_path / "codex", root, {"thread": "/registry"})
    for file in FILES.values():
        with closing(database(root / file)) as db:
            assert verified_schema(db)
            add_row(db)
    with closing(sqlite3.connect(root / "state_5.sqlite")) as db:
        db.executescript(
            "CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT); INSERT INTO threads VALUES('thread','/native');"
        )
    write(source.codex_home / "memories/MEMORY.md", "derived text")
    scan = source.scan()
    assert [(s.schema_version, s.state, s.completeness) for s in scan.stores] == [
        ("v1", "ready", "complete"),
        ("v2", "ready", "complete"),
    ]
    assert len({r.ref.memory_id for r in scan.records}) == 2
    assert [
        (r.project, source.read_selected(r.ref).content, r.mutation_block) for r in scan.records
    ] == [
        ("/native", "## Raw memory\n\nmy private note\n\n## Rollout summary\n\nsummary text", None)
    ] * 2


def test_wal_read_observes_committed_data_and_ignores_usage(tmp_path):
    source = CodexMemorySource(tmp_path, tmp_path)
    file = tmp_path / FILES["v1"]
    with closing(database(file)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        add_row(writer)
        main_bytes = file.read_bytes()
        native_files = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
        (row,) = source.scan().records
        assert source.read_selected(row.ref).content.startswith("## Raw memory\n\nmy private note")
        assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == native_files
        writer.execute(
            "UPDATE stage1_outputs SET usage_count=30,last_usage=123,selected_for_phase2=1"
        )
        writer.commit()
        assert source.scan().records == (row,)
        assert file.read_bytes() == main_bytes
        assert not (tmp_path / FILES["v2"]).exists()


def test_quiescent_wal_databases_leave_native_directories_intact(tmp_path):
    file = tmp_path / FILES["v1"]
    with closing(database(file)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        add_row(db)
    state = tmp_path / "state_5.sqlite"
    with closing(sqlite3.connect(state)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            "CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT); INSERT INTO threads VALUES('thread','/native');"
        )
    native_files = {path.name: path.read_bytes() for path in (file, state)}
    (row,) = CodexMemorySource(tmp_path, tmp_path).scan().records
    assert row.project == "/native"
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == native_files


@pytest.mark.parametrize(
    "temp_root_type", ["sqlite-home", "codex-home", "subdirectory", "symlink-to-native"]
)
def test_codex_scratch_stays_outside_native_roots(tmp_path, temp_root_type):
    native = tmp_path / "sqlite"
    config = tmp_path / "codex"
    config.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(native, target_is_directory=True)
    nested = native / "nested"
    nested.mkdir(parents=True)
    scratch = {
        "sqlite-home": native,
        "codex-home": config,
        "subdirectory": nested,
        "symlink-to-native": alias,
    }
    file = native / FILES["v1"]
    with closing(database(file)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        add_row(db)
    stamps = {path: path.stat().st_mtime_ns for path in (native, config, nested)}
    script = (
        "import sys; from pathlib import Path; "
        "from ringleader.memory_sources.codex import CodexMemorySource; "
        "scan = CodexMemorySource(Path(sys.argv[1]), Path(sys.argv[2])).scan(); "
        "(row,) = scan.records; print(row.ref.source_ref)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(config), str(native)],
        env={**os.environ, "TMPDIR": str(scratch[temp_root_type])},
        timeout=10,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "thread"
    assert {path: path.stat().st_mtime_ns for path in stamps} == stamps
    assert {path.name for path in native.iterdir()} == {file.name, nested.name}


def test_codex_reports_unavailable_when_isolated_scratch_cannot_be_created(tmp_path, monkeypatch):
    native = tmp_path / "sqlite"
    with closing(database(native / FILES["v1"])) as db:
        add_row(db)
    monkeypatch.setenv("TMPDIR", str(native))
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)

    def unavailable(*, dir, prefix):
        assert not dir.is_relative_to(native)
        raise OSError("scratch write failed")

    monkeypatch.setattr(codex, "TemporaryDirectory", unavailable)
    scan = CodexMemorySource(native, native).scan()
    store = scan.stores[0]
    assert (scan.records, store.state, store.completeness) == ((), "unreadable", "unavailable")
    assert store.reason == "No temporary directory outside native Codex roots"


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod does not restrict root")
def test_explicit_codex_sqlite_home_is_readable_with_inaccessible_config(tmp_path, monkeypatch):
    config = tmp_path / "private" / "config"
    config.mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    native = tmp_path / "sqlite"
    file = native / FILES["v1"]
    with closing(database(file)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        add_row(db)
    stamps = {path: path.stat().st_mtime_ns for path in (native, config)}
    monkeypatch.setenv("CODEX_HOME", str(config))
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(scratch))
    config.parent.chmod(0o600)
    try:
        with pytest.raises(PermissionError):
            config.stat()
        sources = memory_scan.new_sources({}, codex_sqlite_home=native, installed=lambda _: True)
        scan = next(source for source in sources if source.name == "codex").scan()
    finally:
        config.parent.chmod(0o700)
    store = next(s for s in scan.stores if s.schema_version == "v1")
    assert (store.state, store.completeness) == ("ready", "complete")
    assert "workspace unavailable" in store.reason
    (row,) = scan.records
    assert row.ref.source_ref == "thread"
    assert "workspace unavailable" in row.mutation_block
    assert {path.name for path in native.iterdir()} == {file.name}
    assert {path: path.stat().st_mtime_ns for path in stamps} == stamps


def test_database_replacement_during_read_discards_observation(tmp_path, monkeypatch):
    source = CodexMemorySource(tmp_path, tmp_path)
    file = tmp_path / FILES["v1"]
    replacement = tmp_path / "replacement.sqlite"
    for path in (file, replacement):
        with closing(database(path)) as db:
            add_row(db)
    (original,) = source.scan().records

    def replace_after_verification(db):
        verified = verified_schema(db)
        replacement.replace(file)
        return verified

    with monkeypatch.context() as patch:
        patch.setattr(codex, "verified_schema", replace_after_verification)
        scan = source.scan()
    status = scan.stores[0]
    assert (scan.records, status.state, status.completeness) == ((), "unreadable", "partial")
    assert "changed during scan" in status.reason
    (new,) = source.scan().records
    assert new.ref.fingerprint == original.ref.fingerprint
    assert new.ref.incarnation != original.ref.incarnation


def test_checkpoint_during_wal_capture_discards_observation(tmp_path, monkeypatch):
    source = CodexMemorySource(tmp_path, tmp_path)
    file = tmp_path / FILES["v1"]
    copy_snapshot = codex._copy_snapshot
    with closing(database(file)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        add_row(writer)

        def checkpoint_after_copy(path, dest, stamp, paths):
            copy_snapshot(path, dest, stamp, paths)
            if path == file:
                assert writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)

        with monkeypatch.context() as patch:
            patch.setattr(codex, "_copy_snapshot", checkpoint_after_copy)
            scan = source.scan()
        status = scan.stores[0]
        assert (scan.records, status.state, status.completeness) == ((), "unreadable", "partial")
        assert "changed during scan" in status.reason
        (row,) = source.scan().records
        assert row.ref.source_ref == "thread"


@pytest.mark.parametrize(
    "commit_order",
    ["first-absence-return", "first-return-absence", "first-delayed-past-absence-capture"],
)
def test_confirmed_absence_starts_fresh_review_for_identical_codex_row(tmp_path, commit_order):
    path = tmp_path / "registry.json"
    source = CodexMemorySource(tmp_path, tmp_path)

    def project(at):
        baseline = registry.load_registry(path)
        return registry.reconcile_native_memory(baseline, source.scan(), at)

    with closing(database(tmp_path / FILES["v1"])) as db:
        add_row(db)
        first = project("2026-11-18T12:00:00+00:00")
        if commit_order != "first-delayed-past-absence-capture":
            registry.commit_registry(path, first)
        db.execute("DELETE FROM stage1_outputs WHERE thread_id = 'thread'")
        db.commit()
        missing = project("2026-11-18T12:01:00+00:00")
        if commit_order == "first-delayed-past-absence-capture":
            registry.commit_registry(path, first)
        (row,) = first["memoryEntries"]
        old = memory_catalog.reviewed_ref(row)
        if commit_order == "first-absence-return":
            registry.commit_registry(path, missing)
            assert missing["memoryEntries"] == []
        add_row(db)
        seen = project("2026-11-18T12:02:00+00:00")
        registry.commit_registry(path, seen)
        if commit_order != "first-absence-return":
            registry.commit_registry(path, missing)
        saved = registry.load_registry(path)
        (current,) = saved["memoryEntries"]
        assert memory_catalog.native_ref(current) == old
        fresh = memory_catalog.reviewed_ref(current)
        assert fresh.incarnation != old.incarnation
        assert fresh.fingerprint == old.fingerprint
        repeated = project("2026-11-18T12:03:00+00:00")
        registry.commit_registry(path, repeated)
        assert memory_catalog.reviewed_ref(repeated["memoryEntries"][0]) == fresh


@pytest.mark.parametrize(
    "schema_change",
    ["ALTER TABLE jobs ADD COLUMN unknown INTEGER", "UPDATE _sqlx_migrations SET checksum=X'00'"],
)
def test_schema_drift_retains_diagnostic_rows_and_blocks_mutation(tmp_path, schema_change):
    with closing(database(tmp_path / FILES["v1"])) as db:
        add_row(db)
        db.execute(schema_change)
        db.commit()
    scan = CodexMemorySource(tmp_path, tmp_path).scan()
    assert (scan.stores[0].state, scan.stores[0].completeness) == ("unsupported", "partial")
    assert "Unverified" in scan.records[0].mutation_block


def test_claude_shared_checkout_unlinked_topic_and_subagent_stores(tmp_path):
    project, linked = tmp_path / "main", tmp_path / "linked"
    linked_git = project / ".git/worktrees/linked"
    write(linked_git / "commondir", "../..")
    write(linked / ".git", f"gitdir: {linked_git}")
    roots = ClaudeRoots(
        tmp_path / "config", tmp_path / "sessions", tmp_path, known_projects=(linked,)
    )
    store = roots.projects / slugify(project) / "memory"
    write(store / "MEMORY.md", "# Memory\n- [one](note.md)\n")
    write(
        store / "note.md",
        "---\nname: note\ndescription: short\ntype: feedback\nmetadata:\n  type: legacy\n---\nCheck red first\n",
    )
    write(store / "unlinked.md", "no index dependency")
    write(roots.projects / "unresolvable" / "memory/other.md", "unresolved")
    write(roots.config / "agent-memory/build/MEMORY.md", "# Lessons\nPrefer static binaries.\n")
    write(
        linked / ".claude/agent-memory/build/note.md",
        "---\nmetadata:\n  type: project\n---\nproject note",
    )
    write(linked / ".claude/agent-memory-local/build/note.md", "local note")
    write(linked / ".claude/agents/build.md", "agent definition")
    write(store / "CLAUDE.md", "instructions")
    (store / "escape.md").symlink_to(linked / ".claude/agents/build.md")
    scan = ClaudeMemorySource(roots).scan()
    assert sorted(
        (r.name, r.project or "", r.scope, r.mem_type or "") for r in scan.records
    ) == sorted(
        [
            ("note", str(project), "project", "feedback"),
            ("unlinked", str(project), "project", ""),
            ("other", "", "project", ""),
            ("Inline memory", "", "global", ""),
            ("note", str(linked), "project", "project"),
            ("note", str(linked), "project", ""),
        ]
    )
    assert next(r for r in scan.records if r.mem_type == "feedback").description == "short"


def test_only_trusted_project_settings_and_explicit_native_overrides(tmp_path):
    home = tmp_path / "home"
    config = home / ".claude"
    yes, no = tmp_path / "trusted", tmp_path / "untrusted"
    roots = ClaudeRoots(
        config,
        config / "projects",
        home,
        known_projects=(yes, no),
        trust_file=home / ".claude.json",
    )
    write(
        roots.trust_file,
        json.dumps(
            {
                "projects": {
                    str(yes): {"hasTrustDialogAccepted": True},
                    str(no): {"hasTrustDialogAccepted": False},
                }
            }
        ),
    )
    write(config / "settings.json", json.dumps({"autoMemoryDirectory": "~/user-notes"}))
    for project in (yes, no):
        write(
            project / ".claude/settings.local.json",
            json.dumps({"autoMemoryDirectory": str(project / "notes")}),
        )
        write(project / "notes/note.md", project.name)
    write(home / "user-notes/note.md", "user config")
    scan = ClaudeMemorySource(roots).scan()
    assert sorted(
        (ClaudeMemorySource(roots).read_selected(r.ref).content, r.project) for r in scan.records
    ) == [
        ("trusted", str(yes)),
        ("user config", None),
    ]
    write(
        config / "settings.json",
        json.dumps({"permissions": {"blockReadsOutsideWorkingDirectories": True}}),
    )
    assert ClaudeMemorySource(roots).scan().records == ()


@pytest.mark.parametrize("config_name", [".claude", "custom-config"])
@pytest.mark.parametrize("namespace", ["", "nested/namespace"])
@pytest.mark.parametrize("command_dir", ["commands", "CoMMaNds"])
def test_claude_auto_memory_directory_refuses_slash_commands(
    tmp_path, config_name, namespace, command_dir
):
    config = tmp_path / config_name
    native = config / "commands" / namespace
    write(native / "build.md", "---\ndescription: Build\n---\nRun the build command.")
    selected = config / command_dir / namespace
    same_directory = selected.is_dir() and selected.samefile(native)
    if not same_directory:
        write(selected / "topic.md", "A note in a separate directory.")
    write(config / "settings.json", json.dumps({"autoMemoryDirectory": str(selected)}))
    roots = ClaudeRoots(config, config / "projects", tmp_path)
    scan = ClaudeMemorySource(roots).scan()
    if not same_directory:
        (record,) = scan.records
        assert (
            ClaudeMemorySource(roots).read_selected(record.ref).content
            == "A note in a separate directory."
        )
        return
    diagnostic = next(s for s in scan.stores if s.store_id == str(config / "settings.json"))
    assert (scan.records, diagnostic.state) == ((), "unreadable")
    assert diagnostic.reason == f"unsafe autoMemoryDirectory: {selected}"


def test_custom_config_trust_status_leaves_availability_to_native_stores(tmp_path):
    roots = ClaudeRoots(tmp_path / "custom-config", tmp_path / "projects", tmp_path)
    blocked = (roots.config / "agent-memory", roots.projects)
    for path in blocked:
        write(path, "a regular file")
    source = ClaudeMemorySource(roots)
    scan = source.scan()
    statuses = {s.store_id: (s.state, s.completeness) for s in scan.stores}
    assert statuses == {
        f"project-trust:{roots.config}": ("ready", "complete"),
        **{str(path): ("unreadable", "unavailable") for path in blocked},
    }
    (harness,) = scan.statuses
    assert (harness.status, harness.reason) == ("unavailable", "No readable supported store")
    note = write(tmp_path / "notes" / "topic.md", "readable store")
    write(roots.config / "settings.json", json.dumps({"autoMemoryDirectory": str(note.parent)}))
    scan = source.scan()
    (harness,) = scan.statuses
    (record,) = scan.records
    assert (harness.status, harness.reason, source.read_selected(record.ref).content) == (
        "ready",
        None,
        "readable store",
    )


@pytest.mark.parametrize("shared_projects_root", [False, True])
@pytest.mark.parametrize("trust_evidence", ["trust-file", "injected-project"])
def test_custom_config_trust_diagnostic_is_cached_with_readable_stores(
    tmp_path, monkeypatch, shared_projects_root, trust_evidence
):
    config = tmp_path / "custom-config"
    project = tmp_path / "checkout"
    projects_root = config if shared_projects_root else config / "projects"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("SESSION_TRACKER_PROJECTS_DIR", str(projects_root))
    delayed = write(project / "custom-notes" / "note.md", "trusted project note")
    write(
        project / ".claude/settings.local.json",
        json.dumps({"autoMemoryDirectory": str(delayed.parent)}),
    )
    write(projects_root / slugify(project) / "memory/note.md", "default project note")
    path = tmp_path / "registry.json"
    base = registry.load_registry(path)
    sources = memory_scan.new_sources(base, project_roots=(project,), installed=lambda _: True)
    scan = memory_scan.scan(sources)
    (note,) = scan.records
    assert sources[0].read_selected(note.ref).content == "default project note"
    (status,) = [s for s in scan.stores if "memory.claude_trust_file" in (s.reason or "")]
    key = memory_catalog.store_key("claude-code", status.store_id)
    assert (status.state, status.completeness) == ("unsupported", "unavailable")
    assert "memory.claude_trust_file" in status.reason
    projection = registry.reconcile_native_memory(base, scan, "2026-11-18T12:00:00+00:00")
    registry.commit_registry(path, projection)
    cached = registry.load_registry(path)
    assert cached["memoryStores"][key]["reason"] == status.reason
    assert cached["memorySources"]["claude-code"]["status"] == "ready"

    evidence = {}
    if trust_evidence == "trust-file":
        evidence["claude_trust_file"] = write(
            config / "trust.json",
            json.dumps({"projects": {str(project): {"hasTrustDialogAccepted": True}}}),
        )
    else:
        evidence["trusted_claude_projects"] = (project,)
    sources = memory_scan.new_sources(
        cached, project_roots=(project,), installed=lambda _: True, **evidence
    )
    scan = memory_scan.scan(sources)
    assert {sources[0].read_selected(record.ref).content for record in scan.records} == {
        "default project note",
        "trusted project note",
    }
    projection = registry.reconcile_native_memory(cached, scan, "2026-11-18T12:01:00+00:00")
    registry.commit_registry(path, projection)
    status = registry.load_registry(path)["memoryStores"][key]
    assert (status["state"], status["reason"]) == ("ready", None)


def test_inline_prose_reports_ambiguous_link_grammar():
    assert inline_content("# Memory\n- [topic](topic.md)\nChoose a bounded wait.\n") == (
        "# Memory\nChoose a bounded wait.",
        None,
    )
    prose, reason = inline_content("Choose [this](topic.md) for the shared checkout.\n")
    assert prose.startswith("Choose") and reason


@pytest.mark.parametrize(
    "index",
    ["# Always save in UTF-8\n", "# Memory\n## Keep hashes\n- [topic](topic.md)\n"],
)
def test_inline_heading_content_remains_visible_and_blocked(tmp_path, index):
    roots = ClaudeRoots(tmp_path / "config", tmp_path / "projects", tmp_path)
    write(roots.projects / "heading" / "memory/MEMORY.md", index)
    write(roots.projects / "index" / "memory/MEMORY.md", "# Memory\n- [topic](topic.md)\n")
    (record,) = ClaudeMemorySource(roots).scan().records
    assert ClaudeMemorySource(roots).read_selected(record.ref).content == index.strip()
    assert "headings" in record.mutation_block


def test_fixture_boundary_includes_configs_and_project_subagent_roots(tmp_path, monkeypatch):
    world = tmp_path / "world"
    from ringleader.devtools.isolation import agent_store_env

    for name, value in agent_store_env(world).items():
        monkeypatch.setenv(name, value)
    outside = tmp_path / "outside"
    write(outside / "secret.md", "outside note")
    write(outside / ".claude/agent-memory/build/secret.md", "outside subagent")
    write(
        Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json",
        json.dumps({"autoMemoryDirectory": str(outside)}),
    )
    sources = memory_scan.new_sources({}, project_roots=(outside,), installed=lambda _: True)
    scan = memory_scan.scan(sources)
    assert scan.records == ()
    assert any("outside the fixture root" in (s.reason or "") for s in scan.stores)
    assert {s.source: s.status for s in scan.statuses} == {
        "codex": "ready",
        "claude-code": "ready",
        "pi": "not-supported",
        "opencode": "not-supported",
        "cursor": "not-supported",
        "droid": "not-supported",
    }
    assert all(
        s.status == "not-installed"
        for s in memory_scan.scan(memory_scan.new_sources({}, installed=lambda _: False)).statuses
    )


def test_native_root_precedence_and_ambiguous_profile(tmp_path, monkeypatch):
    native = tmp_path / "codex"
    config_root = tmp_path / "dbs"
    monkeypatch.setenv("CODEX_HOME", str(native))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "env-root"))
    monkeypatch.setenv("CODEX_SESSIONS_DIR", str(tmp_path / "unrelated"))
    write(native / "config.toml", f'sqlite_home = "{config_root}"')
    reg = registry.empty_registry()
    source = next(s for s in memory_scan.new_sources(reg) if s.name == "codex")
    assert {s.store_id for s in source.scan().stores} == {
        str(config_root / file) for file in FILES.values()
    }
    write(native / "work.config.toml", f'sqlite_home = "{tmp_path}/work-dbs"')
    source = next(s for s in memory_scan.new_sources(reg) if s.name == "codex")
    (store,) = source.scan().stores
    assert store.state == "unsupported" and "explicit" in store.reason
    source = next(
        s for s in memory_scan.new_sources(reg, codex_sqlite_home=config_root) if s.name == "codex"
    )
    assert all(s.state == "absent" for s in source.scan().stores)


def test_model_only_codex_profiles_preserve_provable_environment_root(tmp_path, monkeypatch):
    native = tmp_path / "codex"
    env_root = tmp_path / "dbs"
    monkeypatch.setenv("CODEX_HOME", str(native))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(env_root))
    config = write(native / "config.toml", 'profile = "work"\n[profiles.work]\nmodel = "gpt-5"')
    write(native / "work.config.toml", 'model = "gpt-5"')
    with closing(database(env_root / FILES["v1"])) as db:
        add_row(db)
    source = next(s for s in memory_scan.new_sources({}) if s.name == "codex")
    (record,) = source.scan().records
    assert record.ref.store_id == str(env_root / FILES["v1"])

    with config.open("a") as stream:
        stream.write(f'\nsqlite_home = "{tmp_path / "work-dbs"}"\n')
    source = next(s for s in memory_scan.new_sources({}) if s.name == "codex")
    (status,) = source.scan().stores
    assert status.state == "unsupported"
    assert "memory.codex_sqlite_home" in status.reason


def test_missing_vs_supported_empty_and_unreadable_per_store(tmp_path):
    src = CodexMemorySource(tmp_path, tmp_path)
    assert [s.state for s in src.scan().stores] == ["absent", "absent"]
    with closing(database(tmp_path / FILES["v1"])):
        pass
    write(tmp_path / FILES["v2"], "broken sqlite")
    assert [(s.state, s.completeness) for s in src.scan().stores] == [
        ("ready", "complete"),
        ("unreadable", "unavailable"),
    ]


def test_source_discovery_imports_stay_cold():
    script = "import sys, ringleader.memory_scan;print(sorted(n for n in sys.modules if n.startswith(('ringleader.models','ringleader.decisions'))))"
    result = subprocess.run(
        [sys.executable, "-c", script], text=True, capture_output=True, timeout=10, check=True
    )
    assert result.stdout.strip() == "[]"


def test_catalog_retains_only_metadata_and_selected_read_opens_one_note(tmp_path, monkeypatch):
    from ringleader.memory_sources.base import NativePaths
    from ringleader.memory_types import MemoryObservation

    config = tmp_path / "config"
    root = config / "projects" / "p" / "memory"
    source = ClaudeMemorySource(ClaudeRoots(config, config / "projects", tmp_path))
    for i in range(40):
        write(root / f"note-{i}.md", f"full native note {i}")
    scan = source.scan()
    assert {type(record) for record in scan.records} == {MemoryObservation}
    ref = next(record.ref for record in scan.records if record.name == "note-12")
    native_read = NativePaths.read
    opened = []

    def record_read(paths, path):
        if path.parent == root:
            opened.append(path.name)
        return native_read(paths, path)

    monkeypatch.setattr(NativePaths, "read", record_read)
    assert source.read_selected(ref).content == "full native note 12"
    assert opened == ["note-12.md"]


def test_empty_claude_stores_skip_advisory_reads(tmp_path, monkeypatch):
    config = tmp_path / "config"
    for name in ("absent", "empty", "non-note"):
        (config / "projects" / name).mkdir(parents=True)
    (config / "projects/empty/memory").mkdir()
    write(config / "projects/non-note/memory/CLAUDE.md", "instructions")
    source = ClaudeMemorySource(ClaudeRoots(config, config / "projects", tmp_path))
    monkeypatch.setattr(
        ClaudeMemorySource, "_advisory_context", lambda *_: pytest.fail("read advisory")
    )
    assert {
        Path(s.store_id).parent.name: s.state
        for s in source.scan().stores
        if s.schema_version == "markdown" or s.store_id.endswith("/memory")
    } == {"absent": "absent", "empty": "ready", "non-note": "ready"}


def test_oversized_note_and_nested_settings_preserve_cached_rows(tmp_path):
    from ringleader.memory_sources.base import MAX_NATIVE_BYTES

    roots = ClaudeRoots(tmp_path / "config", tmp_path / "projects", tmp_path)
    note = write(roots.projects / "p/memory/a.md", "cached")
    write(roots.projects / "p/memory/b.md", "healthy")
    source = ClaudeMemorySource(roots)
    reg = registry.empty_registry()
    memory_catalog.apply_scan(reg, source.scan(), "a")
    with note.open("r+b") as file:
        file.truncate(MAX_NATIVE_BYTES + 1)
    settings = write(
        roots.config / "settings.json", '{"setting":' + "[" * 2000 + "0" + "]" * 2000 + "}"
    )
    scan = source.scan()
    problems = {s.store_id: s for s in scan.stores if s.reason}
    assert "byte limit" in problems[str(note.parent)].reason
    assert problems[str(note.parent)].completeness == "partial"
    assert "Excessively nested" in problems[str(settings)].reason
    memory_catalog.apply_scan(reg, scan, "b")
    assert sorted(r["name"] for r in reg["memoryEntries"]) == ["a", "b"]


def test_codex_combined_snapshot_limit_and_optional_project_enrichment(tmp_path, monkeypatch):
    source = CodexMemorySource(tmp_path, tmp_path)
    memory = tmp_path / FILES["v1"]
    state = tmp_path / "state_5.sqlite"
    with state.open("wb") as file:
        file.truncate(codex.MAX_SNAPSHOT + 1)
    original = codex._snapshot_stamp
    seen = []

    def observe(path, paths):
        seen.append(path)
        return original(path, paths)

    monkeypatch.setattr(codex, "_snapshot_stamp", observe)
    assert [s.state for s in source.scan().stores] == ["absent", "absent"]
    assert state not in seen
    with closing(database(memory)) as db:
        add_row(db)
    scan = source.scan()
    assert [r.name for r in scan.records] == ["my-topic"]
    assert scan.stores[0].completeness == "complete"
    assert (
        scan.stores[-1].completeness == "partial" and "project enrichment" in scan.stores[-1].reason
    )
    with memory.with_name(memory.name + "-wal").open("wb") as file:
        file.truncate(codex.MAX_SNAPSHOT - memory.stat().st_size + 1)
    refused = source.scan()
    assert refused.stores[0].completeness == "unavailable"
    assert "database and WAL exceed" in refused.stores[0].reason


def test_case_aliases_of_known_protected_root_and_physical_store(tmp_path):
    checkout = tmp_path / "Checkout"
    config = tmp_path / "Config"
    alias = tmp_path / "cHECKOUT"
    write(checkout / "build.md", "project documentation")
    if not alias.is_dir():
        write(alias / "separate.md", "different directory")
    settings = write(config / "settings.json", json.dumps({"autoMemoryDirectory": str(alias)}))
    source = ClaudeMemorySource(ClaudeRoots(config, config / "projects", tmp_path, (checkout,)))
    scan = source.scan()
    if alias.samefile(checkout):
        assert [s.reason for s in scan.stores if s.store_id == str(settings)] == [
            f"unsafe autoMemoryDirectory: {alias}"
        ]
    else:
        assert [r.name for r in scan.records] == ["separate"]
    store = config / "projects" / "p" / "memory"
    write(store / "note.md", "a note")
    alternate = store.with_name("mEmOry")
    settings.write_text(json.dumps({"autoMemoryDirectory": str(alternate)}))
    if not alternate.is_dir():
        write(alternate / "other.md", "another note")
    expected = ["note"] if store.samefile(alternate) else ["note", "other"]
    assert sorted(r.name for r in source.scan().records) == expected


@pytest.mark.parametrize("ending", [b"\r", b"\n", b"\r\n"])
def test_index_line_budget_preserves_cached_inline_row_and_other_notes(
    tmp_path, monkeypatch, ending
):
    from ringleader.memory_sources import claude_index

    roots = ClaudeRoots(tmp_path / "config", tmp_path / "projects", tmp_path)
    index = write(roots.projects / "p/memory/MEMORY.md", "cached inline")
    write(index.with_name("other.md"), "healthy")
    source = ClaudeMemorySource(roots)
    reg = registry.empty_registry()
    memory_catalog.apply_scan(reg, source.scan(), "a")
    monkeypatch.setattr(claude_index, "MAX_INDEX_LINES", 3)
    index.write_bytes(ending.join([b"one", b"two", b"three", b""]))
    assert {r.name for r in source.scan().records} == {"Inline memory", "other"}
    index.write_bytes(index.read_bytes() + b"four")

    def unexpected(*args, **kwargs):
        pytest.fail("over-budget index reached line allocation or Markdown parser")

    with monkeypatch.context() as fault:
        fault.setattr(claude_index, "StringIO", unexpected)
        fault.setattr(claude_index, "MarkdownIt", unexpected)
        scan = source.scan()
    (store,) = [s for s in scan.stores if s.store_id == str(index.parent)]
    assert store.completeness == "partial"
    assert "3 physical lines" in store.reason
    assert [r.name for r in scan.records] == ["other"]
    memory_catalog.apply_scan(reg, scan, "b")
    assert {r["name"] for r in reg["memoryEntries"]} == {"Inline memory", "other"}
