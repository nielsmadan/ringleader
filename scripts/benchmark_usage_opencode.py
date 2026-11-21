import argparse
import io
import json
import platform
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import closing
from pathlib import Path

BASE = "588ae960d6e6ae88668a4aa6325955c1719173fe"
PROBE = """
import json
import resource
import sys
from ringleader.commands.usage import cmd_usage
from ringleader.usage.opencode import OpenCodeUsageReader
queries = []
original = OpenCodeUsageReader._rows
def counted(self, conditions, params, direction, *rest):
    queries.append(direction)
    yield from original(self, conditions, params, direction, *rest)
OpenCodeUsageReader._rows = counted
status = cmd_usage(sys.argv[1:])
print(json.dumps({"max_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 1024), 1),
                  "source_queries": queries}), file=sys.stderr)
raise SystemExit(status)
"""


def run(code, state, db, *flags):
    environment = {
        "HOME": str(state / "home"),
        "XDG_DATA_HOME": str(state),
        "OPENCODE_DB": str(db),
        "PYTHONPATH": str(code),
        "PATH": "/usr/bin:/bin",
        "TERM": "dumb",
        "NO_COLOR": "1",
    }
    start = time.perf_counter()
    process = subprocess.run(
        [
            sys.executable,
            "-B",
            "-P",
            "-c",
            PROBE,
            "local",
            "--source",
            "opencode",
            "--json",
            *flags,
        ],
        capture_output=True,
        check=True,
        cwd=code,
        env=environment,
        text=True,
        timeout=30,
    )
    elapsed = time.perf_counter() - start
    return {"seconds": round(elapsed, 3), **json.loads(process.stderr)}, json.loads(process.stdout)


def fixture(db, assistant_rows, sessions):
    assistant = json.dumps(
        {
            "role": "assistant",
            "modelID": "gpt-5.6-sol",
            "providerID": "openai",
            "cost": 0.012,
            "tokens": {
                "input": 1000,
                "reasoning": 112,
                "output": 284,
                "cache": {"read": 813, "write": 250},
            },
        }
    )
    user = json.dumps({"role": "user", "agent": "build"})
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.executescript("""
            CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT);
            CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, directory TEXT);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE INDEX message_time_created_idx ON message(time_created);
        """)
        projects = 50
        connection.executemany(
            "INSERT INTO project VALUES (?, ?)",
            [
                (f"project-{index}", f"/fixture/repositories/repository-{index:03d}")
                for index in range(projects)
            ],
        )
        identifiers = [f"ses_000000000000000000000000{index:06d}" for index in range(sessions)]
        connection.executemany(
            "INSERT INTO session VALUES (?, ?, ?)",
            [
                (
                    identifier,
                    f"project-{index % projects}",
                    f"/fixture/repositories/repository-{index % projects:03d}",
                )
                for index, identifier in enumerate(identifiers)
            ],
        )
        connection.executemany(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            (
                (
                    f"msg_000000000000000000000000{index:06d}_{role}",
                    identifiers[index % sessions],
                    1770998211275 + 300000 * index + role,
                    (user, assistant)[role],
                )
                for index in range(assistant_rows)
                for role in range(2)
            ),
        )


def allocation(path):
    with closing(sqlite3.connect(path)) as connection:
        pages, free_pages, page_size, vacuum = [
            connection.execute(f"PRAGMA {pragma}").fetchone()[0]
            for pragma in ("page_count", "freelist_count", "page_size", "auto_vacuum")
        ]
        sizes = [
            size
            for (size,) in connection.execute(
                "SELECT length(payload) FROM entries ORDER BY namespace"
            )
        ]
    return {
        "size_mib": round(path.stat().st_size / 2**20, 3),
        "pages": pages,
        "free_pages": free_pages,
        "page_size": page_size,
        "auto_vacuum": vacuum,
        "payload_bytes": sizes,
    }


def main():
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default=BASE)
    parser.add_argument("--initial", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="rl-cp-perf-review-") as temporary:
        scratch = Path(temporary)
        baseline = scratch / "baseline"
        initial = scratch / "initial"
        current = scratch / "current"
        baseline.mkdir()
        package = subprocess.check_output(["git", "archive", args.baseline, "ringleader"], cwd=repo)
        with tarfile.open(fileobj=io.BytesIO(package)) as archive:
            archive.extractall(baseline, filter="data")
        versions = [("current", current)]
        if args.initial is not None and (args.initial / "ringleader").is_dir():
            shutil.copytree(baseline, initial)
            shutil.copytree(args.initial / "ringleader", initial / "ringleader", dirs_exist_ok=True)
            versions.insert(0, ("initial", initial))
        shutil.copytree(
            repo / "ringleader",
            current / "ringleader",
            ignore=shutil.ignore_patterns("__pycache__", "_vendor"),
        )
        db = scratch / "opencode.sqlite"
        fixture(db, 100_000, 500)
        baseline_results = [
            run(baseline, scratch / "state-baseline", db, "--all") for _ in range(3)
        ]
        expected = baseline_results[0][1]
        assert all(data == expected for _, data in baseline_results)
        _, default_expected = run(baseline, scratch / "state-baseline", db)
        measurements = {
            "baseline": args.baseline,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "db_size_mib": round(db.stat().st_size / 2**20, 1),
            "records": sum(row["records"] for row in expected["rows"]),
            "baseline_all": [measured for measured, _ in baseline_results],
            "json_equal": True,
        }
        for name, code in versions:
            state = scratch / f"state-{name}"
            all_results = [run(code, state, db, "--all") for _ in range(4)]
            assert all(data == expected for _, data in all_results)
            (path,) = (state / "ringleader/usage").glob("local-v*.sqlite")
            report = {
                "all_cold": all_results[0][0],
                "all_repeat": [measured for measured, _ in all_results[1:]],
                "after_all": allocation(path),
            }
            default, output = run(code, state, db)
            assert output == default_expected
            report["default_after_all"] = default
            report["after_default"] = allocation(path)
            stamp = path.stat().st_mtime_ns
            default_again, output = run(code, state, db)
            assert output == default_expected
            report["default_again"] = default_again
            report["database_modified_on_default_hit"] = path.stat().st_mtime_ns != stamp
            with closing(sqlite3.connect(path)) as read_lock:
                read_lock.execute("BEGIN")
                read_lock.execute("SELECT count(*) FROM entries").fetchone()
                default_contended, output = run(code, state, db)
                assert output == default_expected
                report["default_again_with_concurrent_cache_reader"] = default_contended
            measurements[name] = report
        print(json.dumps(measurements, indent=2))


if __name__ == "__main__":
    main()
