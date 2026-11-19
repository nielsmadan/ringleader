import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from ringleader import memory_catalog, memory_scan, registry
from ringleader.memory_lifecycle import MemoryLifecycle, MemoryPresence
from ringleader.memory_sources import codex_artifacts as artifacts
from ringleader.memory_sources.claude_code import ClaudeMemorySource, ClaudeRoots
from ringleader.memory_sources.codex import FILES, CodexMemorySource
from ringleader.presence import HookStatus, PresenceResult

SCHEMA = Path(__file__).parent / "fixtures/memory"


def database(path, *, migrations=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE _sqlx_migrations (version BIGINT PRIMARY KEY, description TEXT NOT NULL, installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, success BOOLEAN NOT NULL, checksum BLOB NOT NULL, execution_time BIGINT NOT NULL)"
    )
    for file in sorted(SCHEMA.glob("*.sql"))[:migrations]:
        version, description = file.stem.split("_", 1)
        schema = file.read_bytes()
        db.executescript(schema.decode())
        db.execute(
            "INSERT INTO _sqlx_migrations(version,description,success,checksum,execution_time) VALUES(?,?,1,?,400)",
            (int(version), description.replace("_", " "), hashlib.sha384(schema).digest()),
        )
    db.commit()
    return db


def add_row(db, key="thread", raw="my private note"):
    db.execute(
        "INSERT INTO stage1_outputs(thread_id,source_updated_at,raw_memory,rollout_summary,rollout_slug,generated_at) VALUES(?,100,?,'summary text','my-topic',105)",
        (key, raw),
    )
    db.commit()


NOW = datetime(2026, 11, 18, tzinfo=UTC)
LINK = "- [Note](note.md) — review this topic\n"
OTHER_LINK = "- [Other](other.md) — another topic\n"


class World:
    def __init__(self, root, source, selector):
        self.path = root / "registry.json"
        self.source = source
        self.selector = selector
        hook_file = source.hook_path
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        if not hook_file.exists():
            hook_file.write_text("{}")
        self.quiet = MemoryPresence(
            PresenceResult(), HookStatus("installed", "fixture", hook_file), ()
        )
        self.presence = self.quiet
        self.service = MemoryLifecycle(sources=lambda _: [source], presence=lambda _: self.presence)
        self.ref = self.scan()

    def scan(self, now=NOW):
        reg = registry.load_registry(self.path)
        scanned = memory_scan.scan([self.source])
        reg = registry.reconcile_native_memory(reg, scanned, now.isoformat())
        registry.commit_registry(self.path, reg)
        row = next(r for r in reg["memoryEntries"] if self.selector(r))
        return memory_catalog.reviewed_ref(row)

    def decide(self, markdown=None, *, decision="harvest", remove=True, ref=None, **kwargs):
        return self.service.decide(
            ref or self.ref,
            registry_path=self.path,
            now=kwargs.pop("now", NOW),
            decision=decision,
            markdown=markdown,
            remove=remove,
            expected_harvest_token=kwargs.pop("expected", None),
            **kwargs,
        )

    def recover(self, op, *, restore=False, now=NOW):
        return self.service.recover(op, registry_path=self.path, now=now, restore=restore)


def claude_world(root, *, inline=False):
    config = root / "claude"
    store = config / "projects" / "test" / "memory"
    store.mkdir(parents=True)
    (store / "note.md").write_bytes(b"native private text\r\n")
    (store / "other.md").write_text("other native content")
    (store / "MEMORY.md").write_text(
        "# Memory\n" + ("inline private text\n" if inline else "") + LINK + OTHER_LINK
    )
    source = ClaudeMemorySource(ClaudeRoots(config, config / "projects", root))
    target = store / ("MEMORY.md" if inline else "note.md")
    return World(root, source, lambda r: r["path"] == str(target)), store


KEY = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
STEM = "1970-01-01T00-01-40-0001-native_slug"
SUMMARY = f"thread_id: {KEY}\nupdated_at: 1970-01-01T00:01:40+00:00\nrollout_path: /fixture/roll.jsonl\ncwd: /fixture\ngit_branch: main\n\nsummary text\n"
SECTION = f"## Thread `{KEY}`\nupdated_at: 1970-01-01T00:01:40+00:00\ncwd: /fixture\nrollout_path: /fixture/roll.jsonl\nrollout_summary_file: {STEM}.md\n\nsource text\n\n"
OTHER_SECTION = (
    SECTION.replace(KEY, OTHER).replace("-0001-", "-0002-").replace("source text", "unrelated")
)
GLOBAL = "memory_consolidate_global"
STAGE_RETRY_AT = int(NOW.timestamp()) + 333


def codex_world(tmp, *, version="v1", stage=True):
    home = tmp / "codex"
    root = home / ("memories" if version == "v1" else "memories_v2")
    summaries = root / "rollout_summaries"
    summaries.mkdir(parents=True)
    (summaries / (STEM + ".md")).write_text(SUMMARY)
    (summaries / (STEM.replace("-0001-", "-0002-") + ".md")).write_text(SUMMARY.replace(KEY, OTHER))
    (root / "raw_memories.md").write_text(
        artifacts.HEADER + SECTION + OTHER_SECTION if version == "v1" else "user v2 derived file"
    )
    (root / "MEMORY.md").write_text("derived output")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("native baseline")
    path = tmp / "sqlite" / FILES[version]
    with closing(database(path)) as db:
        for k, raw in ((KEY, "source text"), (OTHER, "unrelated")):
            db.execute(
                "INSERT INTO stage1_outputs VALUES (?,100,?,'summary text','Native Slug',105,12,80,1,100)",
                (k, raw),
            )
        if stage:
            db.execute(
                "INSERT INTO jobs VALUES ('memory_stage1',?,'done','worker','owner',65,82,NULL,?,0,NULL,100,100)",
                (KEY, STAGE_RETRY_AT),
            )
        db.execute(
            "INSERT INTO jobs VALUES (?,'global','done','worker','owner',70,85,NULL,333,1,NULL,110,400)",
            (GLOBAL,),
        )
        db.execute("UPDATE consolidation_progress SET max_thread_count=7 WHERE singleton=1")
        db.commit()
    source = CodexMemorySource(home, path.parent, version_probe=lambda: "codex-cli 0.167.1")
    return World(tmp, source, lambda r: r["sourceRef"] == KEY), root, path


def set_file_identity(monkeypatch, strength):
    from ringleader.memory_sources import base, claude_code, claude_mutation, transactions

    def identity(info):
        return f"{strength}:{info.st_dev}:{info.st_ino}"

    for module in (base, claude_code, claude_mutation, transactions):
        monkeypatch.setattr(module, "file_identity", identity)
