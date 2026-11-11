import difflib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NoReturn

import pytest
from ringleader.devtools.isolation import agent_store_env
from ringleader.portability import claude
from ringleader.tui.snapshot import normalize_text

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"
GOLDENS_ROOT = Path(__file__).resolve().parent / "goldens"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="rewrite tests/goldens from the current output",
    )


def _replaced(text: str, replace: Mapping[str, str]) -> str:
    for old in sorted(replace, key=len, reverse=True):
        text = text.replace(old, replace[old])
    return text


def golden_checker(
    request: pytest.FixtureRequest, root: Path = GOLDENS_ROOT
) -> Callable[..., None]:
    def check(name: str, text: str, *, replace: Mapping[str, str] | None = None) -> None:
        path = root / f"{name}.txt"
        actual = normalize_text(_replaced(text, replace or {}))
        if request.config.getoption("--update-goldens"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(actual, encoding="utf-8")
            return
        if not path.exists():
            pytest.fail(
                f"no golden at {path}: run uv run pytest {request.node.nodeid} --update-goldens"
            )
        expected = path.read_text(encoding="utf-8")
        if actual != expected:
            diff = difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{name}.txt (golden)",
                tofile=f"{name}.txt (actual)",
            )
            pytest.fail("golden mismatch:\n" + "".join(diff), pytrace=False)

    return check


@pytest.fixture
def golden(request: pytest.FixtureRequest) -> Callable[..., None]:
    return golden_checker(request)


@pytest.fixture(autouse=True)
def _no_real_model_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(argv: Sequence[str], **_: object) -> NoReturn:
        raise AssertionError(f"a test reached a real model CLI: {argv[0]}")

    monkeypatch.setattr("ringleader.models.proc.run_model_process", refuse)


@pytest.fixture(scope="session")
def skill_dirs():
    """The bundled skills, discovered once. Shared so the files that check them cannot
    disagree about what counts as a skill."""
    return sorted(p for p in SKILLS_ROOT.glob("ringleader-*") if (p / "SKILL.md").is_file())


@pytest.fixture(autouse=True)
def isolate_agent_environment(monkeypatch, tmp_path):
    for name, value in agent_store_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    # keeps `rl doctor` from finding — and running — the machine's real hooklinesinker
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "_data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "_state"))
    for name in (
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "PI_SESSION_ID",
        "OPENCODE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "OPENCODE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "PI_CODING_AGENT_DIR",
        "QWEN_HOME",
        "KIMI_CODE_HOME",
        "ANTHROPIC_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(claude, "MANAGED_SETTINGS", {})


@pytest.fixture(autouse=True, scope="session")
def guard_live_state(tmp_path_factory):
    """Every rl command defaults `--registry` to the user's real registry, so one missing
    `--registry` in a test would read and overwrite live session state. Point the whole
    state dir at a throwaway path for the session.

    `XDG_DATA_HOME` is the one lever that works: `paths.state_dir()` reads it on every
    call, so every helper built on it moves together — registry, archive root, config,
    publish receipts — whereas patching `default_registry` would miss the copy each
    command module bound at import time.

    Prevention rather than the snapshot-and-restore this used to attempt: `rl data
    migrate` moved the live file under `~/.local/share`, where the user's own session-end
    hooks write it while the suite runs, so restoring a snapshot would sooner or later
    revert one of their writes."""
    patch = pytest.MonkeyPatch()
    patch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg-data")))
    yield
    patch.undo()
