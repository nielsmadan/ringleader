import json
import os
import sqlite3
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from ringleader.commands import usage as usage_cmd
from ringleader.usage import aggregate
from ringleader.usage.base import record_day
from ringleader.usage.claude import ClaudeUsageReader
from ringleader.usage.codex import CodexUsageReader
from ringleader.usage.droid import DroidUsageReader
from ringleader.usage.opencode import OpenCodeUsageReader
from ringleader.usage.pi import PiUsageReader

CODEX_A = "01a0118b-f3a9-7453-af81-02d2d94a4e64"
CODEX_B = "01b0118b-f3a9-7453-af81-02d2d94a4e64"


def _epoch(day: str) -> float:
    return datetime.fromisoformat(f"{day}T12:00:00+00:00").timestamp()


def _claude_line(msg_id: str, day: str, out: int = 10) -> dict:
    return {
        "type": "assistant",
        "requestId": f"r-{msg_id}",
        "timestamp": f"{day}T10:00:00.000Z",
        "cwd": "/x/foo",
        "message": {
            "id": msg_id,
            "model": "claude-opus-5",
            "usage": {"input_tokens": 1, "output_tokens": out},
        },
    }


def _claude_file(
    root: Path, session: str, day: str, mtime_day: str | None = None, project: str = "-x-foo"
) -> Path:
    return _claude_lines(
        root, session, [_claude_line(f"m-{session}", day)], mtime_day or day, project
    )


def _claude_lines(
    root: Path, session: str, lines: list[dict], mtime_day: str, project: str = "-x-foo"
) -> Path:
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session}.jsonl"
    p.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    stamp = _epoch(mtime_day)
    os.utime(p, (stamp, stamp))
    return p


@pytest.fixture
def claude_dir(tmp_path):
    root = tmp_path / "claude"
    _claude_file(root, "old-one", "2026-01-05")
    _claude_file(root, "old-two", "2026-02-05")
    _claude_file(root, "recent-a", "2026-08-16")
    _claude_file(root, "recent-b", "2026-08-17")
    return root


def test_claude_since_skips_files_last_written_before_the_window(claude_dir):
    reader = ClaudeUsageReader(projects_dir=claude_dir)

    sessions = sorted(r.session_id for r in reader.records(since=date(2026, 8, 15)))

    assert sessions == ["recent-a", "recent-b"]


def test_claude_since_keeps_a_file_written_one_day_before_the_boundary(tmp_path):
    root = tmp_path / "claude"
    _claude_file(root, "edge", "2026-08-15", mtime_day="2026-08-14")

    sessions = [
        r.session_id for r in ClaudeUsageReader(projects_dir=root).records(since=date(2026, 8, 15))
    ]

    assert sessions == ["edge"]


def test_claude_without_since_reads_everything(claude_dir):
    assert len(list(ClaudeUsageReader(projects_dir=claude_dir).records())) == 4


def test_claude_latest_day_stops_before_opening_old_files(claude_dir, monkeypatch):
    opened: list[str] = []
    real_open = Path.open

    def spy(self, *a, **k):
        opened.append(self.name)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy)

    assert ClaudeUsageReader(projects_dir=claude_dir).latest_day() == "2026-08-17"
    assert sorted(opened) == ["recent-a.jsonl", "recent-b.jsonl"]


def test_claude_session_prefix_reads_its_own_project_up_to_the_session(claude_dir, monkeypatch):
    _claude_file(claude_dir, "elsewhere", "2026-08-17", project="-x-bar")
    opened: list[str] = []
    real_open = Path.open

    def spy(self, *a, **k):
        opened.append(self.name)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy)

    recs = list(ClaudeUsageReader(projects_dir=claude_dir).records(session_prefix="recent-a"))

    assert [r.session_id for r in recs] == ["recent-a"]
    assert opened == ["old-one.jsonl", "old-two.jsonl", "recent-a.jsonl"]


def _pi_file(root: Path, session_id: str, day: str) -> None:
    d = root / "--x-foo--"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"2026-01-01T00-00-00_{session_id}.jsonl"
    line = {
        "timestamp": f"{day}T10:00:00Z",
        "message": {"usage": {"input": 1, "output": 2}, "model": "m", "provider": "p"},
    }
    p.write_text(json.dumps(line) + "\n", encoding="utf-8")
    os.utime(p, (_epoch(day), _epoch(day)))


def test_pi_since_prefix_and_latest_day(tmp_path):
    _pi_file(tmp_path, "aaa111", "2026-03-01")
    _pi_file(tmp_path, "bbb222", "2026-08-17")
    reader = PiUsageReader(sessions_dir=tmp_path)

    assert [r.session_id for r in reader.records(since=date(2026, 8, 1))] == ["bbb222"]
    assert [r.session_id for r in reader.records(session_prefix="aaa")] == ["aaa111"]
    assert reader.latest_day() == "2026-08-17"


def _codex_limits(i: int, plan: str, slots: tuple[str, ...]) -> dict:
    windows = {
        "primary": {"used_percent": float(i), "window_minutes": 300, "resets_at": 1787200000},
        "secondary": {
            "used_percent": float(i) + 50,
            "window_minutes": 10080,
            "resets_at": 1787213354,
        },
    }
    return {"plan_type": plan, **{slot: windows[slot] for slot in slots}}


def _codex_file(
    root: Path,
    session: str,
    stamps: list[str],
    plan: str = "pro",
    slots: tuple[str, ...] = ("primary", "secondary"),
) -> Path:
    d = root / "2026" / "08"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-2026-08-17T23-06-19-{session}.jsonl"
    lines = [
        json.dumps(
            {
                "timestamp": ts,
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 10 + i, "output_tokens": 1},
                        "total_token_usage": {"input_tokens": 10 + i},
                    },
                    "rate_limits": _codex_limits(i, plan, slots),
                },
            }
        )
        for i, ts in enumerate(stamps)
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    last = datetime.fromisoformat(stamps[-1]).timestamp()
    os.utime(p, (last, last))
    return p


@pytest.fixture
def codex_dir(tmp_path):
    root = tmp_path / "codex"
    _codex_file(root, "0" * 8 + "-0000-0000-0000-000000000001", ["2026-01-01T10:00:00Z"])
    _codex_file(root, "0" * 8 + "-0000-0000-0000-000000000002", ["2026-02-01T10:00:00Z"])
    _codex_file(root, CODEX_A, ["2026-08-16T10:00:00Z", "2026-08-16T11:00:00Z"])
    _codex_file(root, CODEX_B, ["2026-08-17T10:00:00Z", "2026-08-17T12:00:00Z"])
    return root


def _codex_id(n: int) -> str:
    return f"{'0' * 8}-0000-0000-0000-{n:012d}"


def _newest_by_kind(reader: CodexUsageReader) -> dict:
    expected: dict = {}
    for w in reader.windows():
        cur = expected.get(w.window)
        if cur is None or w.timestamp > cur.timestamp:
            expected[w.window] = w
    return expected


def test_codex_latest_windows_equals_scanning_every_file(codex_dir):
    reader = CodexUsageReader(sessions_dir=codex_dir)

    got = {w.window: w for w in reader.latest_windows()}

    assert got == _newest_by_kind(reader)
    assert got["5h"].timestamp == "2026-08-17T12:00:00Z"
    assert got["weekly"].used_percent == 51.0


def test_codex_latest_windows_stops_before_opening_old_rollouts(codex_dir, monkeypatch):
    opened: list[str] = []
    real_open = Path.open

    def spy(self, *a, **k):
        opened.append(self.name)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy)

    CodexUsageReader(sessions_dir=codex_dir).latest_windows()

    assert opened == [f"rollout-2026-08-17T23-06-19-{CODEX_B}.jsonl"]


def test_codex_latest_windows_finds_a_kind_only_an_older_rollout_carries(tmp_path):
    root = tmp_path / "codex"
    _codex_file(root, _codex_id(1), ["2026-08-17T12:00:00Z"], slots=("primary",))
    _codex_file(root, _codex_id(2), ["2026-08-14T12:00:00Z"], slots=("secondary",))
    _codex_file(root, _codex_id(3), ["2026-08-12T12:00:00Z"], slots=("secondary",))
    reader = CodexUsageReader(sessions_dir=root)

    got = {w.window: w for w in reader.latest_windows()}

    assert got == _newest_by_kind(reader)
    assert got["weekly"].timestamp == "2026-08-14T12:00:00Z"


def test_codex_latest_windows_without_a_weekly_window_stops_a_week_back(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    stamps = ["2026-08-17T12:00:00Z", "2026-08-12T12:00:00Z", "2026-08-05T12:00:00Z"]
    for n, stamp in enumerate([*stamps, "2026-07-01T12:00:00Z"], start=1):
        _codex_file(root, _codex_id(n), [stamp], slots=("primary",))
    reader = CodexUsageReader(sessions_dir=root)
    expected = _newest_by_kind(reader)
    opened: list[str] = []
    real_open = Path.open

    def spy(self, *a, **k):
        opened.append(self.name)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy)

    got = {w.window: w for w in reader.latest_windows()}

    assert got == expected
    assert sorted(opened) == [f"rollout-2026-08-17T23-06-19-{_codex_id(n)}.jsonl" for n in (1, 2)]


def test_codex_latest_windows_tie_keeps_the_earlier_path(tmp_path):
    root = tmp_path / "codex"
    _codex_file(root, CODEX_B, ["2026-08-17T10:00:00Z"], plan="late-path")
    _codex_file(root, CODEX_A, ["2026-08-17T10:00:00Z"], plan="early-path")
    reader = CodexUsageReader(sessions_dir=root)
    first = next(iter(reader.windows()))

    assert {w.plan_type for w in reader.latest_windows()} == {first.plan_type}


def test_codex_since_prefix_and_latest_day(codex_dir):
    reader = CodexUsageReader(sessions_dir=codex_dir)

    assert {r.session_id for r in reader.records(since=date(2026, 8, 1))} == {CODEX_A, CODEX_B}
    assert {r.session_id for r in reader.records(session_prefix="01a0")} == {CODEX_A}
    assert reader.latest_day() == "2026-08-17"


def _oc_db(tmp_path: Path, rows: list[tuple[str, str, int]]) -> Path:
    path = tmp_path / "opencode.db"
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL);
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, directory TEXT NOT NULL);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
        INSERT INTO project VALUES ('p', '/repo');
        """
    )
    for mid, session, ms in rows:
        c.execute("INSERT OR IGNORE INTO session VALUES (?, 'p', '/repo')", (session,))
        data = {"role": "assistant", "tokens": {"input": 5, "output": 5, "cache": {}}}
        c.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)", (mid, session, ms, ms, json.dumps(data))
        )
    c.commit()
    c.close()
    return path


def _ms(day: str) -> int:
    return int(_epoch(day) * 1000)


def test_opencode_since_prefix_and_latest_day(tmp_path):
    db = _oc_db(
        tmp_path,
        [
            ("m1", "ses_old", _ms("2026-01-01")),
            ("m2", "ses_new", _ms("2026-08-16")),
            ("m3", "ses_new", _ms("2026-08-17")),
        ],
    )
    reader = OpenCodeUsageReader(db_path=db)

    assert [r.record_id for r in reader.records(since=date(2026, 8, 16))] == ["m2", "m3"]
    assert [r.record_id for r in reader.records(session_prefix="ses_o")] == ["m1"]
    assert [r.record_id for r in reader.records()] == ["m1", "m2", "m3"]
    assert reader.latest_day() == "2026-08-17"


def test_opencode_since_is_day_exact_not_padded(tmp_path):
    midnight = int(datetime(2026, 8, 16, tzinfo=UTC).timestamp() * 1000)
    db = _oc_db(tmp_path, [("m1", "s", midnight - 1), ("m2", "s", midnight)])

    ids = [r.record_id for r in OpenCodeUsageReader(db_path=db).records(since=date(2026, 8, 16))]

    assert ids == ["m2"]


def _patched(monkeypatch, claude_dir):
    monkeypatch.setitem(
        aggregate.READERS, "claude", partial(ClaudeUsageReader, projects_dir=claude_dir)
    )


def _run(capsys, *argv):
    usage_cmd.cmd_usage(["--source", "claude", "--json", *argv])
    return json.loads(capsys.readouterr().out)


def _droid_session(root: Path, directory: str, session_id: str, day: str, inp: int) -> Path:
    d = root / directory
    d.mkdir(parents=True, exist_ok=True)
    transcript = d / f"{session_id}.jsonl"
    start = {"type": "session_start", "id": session_id, "cwd": "/x/foo"}
    transcript.write_text(json.dumps(start) + "\n", encoding="utf-8")
    sidecar = {"model": "m", "tokenUsage": {"inputTokens": inp, "outputTokens": 1}}
    (d / f"{session_id}.settings.json").write_text(json.dumps(sidecar), encoding="utf-8")
    os.utime(transcript, (_epoch(day), _epoch(day)))
    return transcript


DROID_TWICE = "dddd0000-0000-4000-8000-000000000001"
DROID_OTHER = "eeee0000-0000-4000-8000-000000000002"


def _droid_dir(root: Path) -> Path:
    _droid_session(root, "-x-a", DROID_TWICE, "2026-02-01", inp=100)
    _droid_session(root, "-x-b", DROID_TWICE, "2026-08-17", inp=100)
    _droid_session(root, "-x-c", DROID_OTHER, "2026-08-16", inp=7)
    return root


def _resumed_pair(root: Path, parent: str, child: str, parent_day: str, child_day: str) -> None:
    shared = [_claude_line("m-p1", parent_day, out=100), _claude_line("m-p2", parent_day, out=200)]
    _claude_lines(root, parent, shared, parent_day)
    _claude_lines(root, child, [*shared, _claude_line("m-c1", child_day, out=5)], child_day)


@pytest.fixture
def mixed_store(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    for session, day in [("c-old", "2026-01-05"), ("c-mid", "2026-08-11"), ("c-new", "2026-08-17")]:
        _claude_file(claude, session, day)
    _resumed_pair(claude, "aaaa-parent", "bbbb-child", "2026-02-01", "2026-08-16")
    codex = tmp_path / "codex"
    _codex_file(codex, _codex_id(1), ["2026-03-01T10:00:00Z"])
    _codex_file(codex, CODEX_A, ["2026-08-15T10:00:00Z", "2026-08-16T11:00:00Z"])
    pi = tmp_path / "pi"
    _pi_file(pi, "pi-old", "2026-04-01")
    _pi_file(pi, "pi-new", "2026-08-14")
    monkeypatch.setattr(
        aggregate,
        "READERS",
        {
            "claude": partial(ClaudeUsageReader, projects_dir=claude),
            "codex": partial(CodexUsageReader, sessions_dir=codex),
            "droid": partial(DroidUsageReader, sessions_dir=_droid_dir(tmp_path / "droid")),
            "pi": partial(PiUsageReader, sessions_dir=pi),
        },
    )


def _full_read_window(days: int, by: str) -> tuple[str, dict[str, int]]:
    records = [r for make in aggregate.READERS.values() for r in make().records()]
    newest = date.fromisoformat(max(record_day(r) for r in records))
    start = (newest - timedelta(days=days - 1)).isoformat()
    rows = aggregate.rollup([r for r in records if record_day(r) >= start], key=usage_cmd._KEYS[by])
    return start, {k: b.total for k, b in rows.items()}


@pytest.mark.parametrize("by", ["session", "day"])
@pytest.mark.parametrize("days", [1, 2, 7, 400])
def test_default_window_matches_reading_everything_then_filtering(mixed_store, capsys, days, by):
    usage_cmd.cmd_usage(["--days", str(days), "--by", by, "--json"])
    out = json.loads(capsys.readouterr().out)

    got = (out["since"], {row["key"]: row["tokens"] for row in out["rows"]})

    assert got == _full_read_window(days, by)


def test_droid_credits_a_session_filed_twice_to_its_first_path_under_a_window(tmp_path):
    root = _droid_dir(tmp_path)

    windowed = list(DroidUsageReader(sessions_dir=root).records(since=date(2026, 8, 10)))
    full = DroidUsageReader(sessions_dir=root).records()

    assert windowed == [r for r in full if record_day(r) >= "2026-08-10"]
    assert [r.session_id for r in windowed] == [DROID_OTHER]


def test_droid_latest_day_and_records_read_each_transcript_once(tmp_path, monkeypatch):
    root = _droid_dir(tmp_path)
    opened: list[str] = []
    real_open = Path.open

    def spy(self, *a, **k):
        opened.append(f"{self.parent.name}/{self.name}")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy)
    reader = DroidUsageReader(sessions_dir=root)

    assert reader.latest_day() == "2026-08-16"
    assert [r.session_id for r in reader.records(since=date(2026, 8, 16))] == [DROID_OTHER]
    assert sorted(o for o in opened if not o.endswith(".settings.json")) == [
        f"-x-a/{DROID_TWICE}.jsonl",
        f"-x-b/{DROID_TWICE}.jsonl",
        f"-x-c/{DROID_OTHER}.jsonl",
    ]


def test_default_window_never_opens_files_older_than_the_window(claude_dir, monkeypatch, capsys):
    _patched(monkeypatch, claude_dir)
    opened: list[str] = []
    real_open = Path.open

    def spy(self, *a, **k):
        opened.append(self.name)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy)

    _run(capsys)

    assert sorted(opened) == [
        "recent-a.jsonl",
        "recent-a.jsonl",
        "recent-b.jsonl",
        "recent-b.jsonl",
    ]


def test_all_flag_still_reads_every_file(claude_dir, monkeypatch, capsys):
    _patched(monkeypatch, claude_dir)

    assert _run(capsys, "--all")["total_tokens"] == 44


def test_default_window_anchors_on_newest_record_not_today(tmp_path, monkeypatch, capsys):
    root = tmp_path / "claude"
    _claude_file(root, "only", "2020-03-10")
    _patched(monkeypatch, root)

    got = _run(capsys, "--days", "1")

    assert got["since"] == "2020-03-10"
    assert got["total_tokens"] == 11


def test_session_command_finds_session_by_filename_prefix(claude_dir, monkeypatch, capsys):
    _patched(monkeypatch, claude_dir)
    monkeypatch.setattr(
        aggregate,
        "READERS",
        {"claude": partial(ClaudeUsageReader, projects_dir=claude_dir)},
    )

    rc = usage_cmd.cmd_usage(["session", "recent-a", "--json"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["session_id"] == "recent-a"


def _by_session_tokens(capsys) -> dict[str, int]:
    usage_cmd.cmd_usage(["--all", "--by", "session", "--json"])
    return {
        row["key"].split()[0]: row["tokens"] for row in json.loads(capsys.readouterr().out)["rows"]
    }


def _session_tokens(capsys, session: str) -> int:
    usage_cmd.cmd_usage(["session", session, "--json"])
    tokens: int = json.loads(capsys.readouterr().out)["tokens"]
    return tokens


@pytest.mark.parametrize(
    ("parent", "child", "expected"),
    [
        ("aaaa1111-p", "bbbb2222-c", {"aaaa1111": 302, "bbbb2222": 6, "cccc3333": 11}),
        ("bbbb2222-p", "aaaa1111-c", {"aaaa1111": 308, "cccc3333": 11}),
    ],
)
def test_session_command_credits_a_resumed_childs_copied_history_like_by_session(
    tmp_path, monkeypatch, capsys, parent, child, expected
):
    root = tmp_path / "claude"
    _resumed_pair(root, parent, child, "2026-08-10", "2026-08-17")
    _claude_file(root, "cccc3333-other", "2026-08-12", project="-x-bar")
    monkeypatch.setattr(
        aggregate, "READERS", {"claude": partial(ClaudeUsageReader, projects_dir=root)}
    )

    by_session = _by_session_tokens(capsys)
    per_session = {key: _session_tokens(capsys, key) for key in by_session}

    assert by_session == expected
    assert per_session == expected
