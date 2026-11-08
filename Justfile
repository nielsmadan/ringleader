[private]
default:
    @just --list

# Prepare this checkout for work: dependencies, hooks, then verify.
setup:
    @uv sync
    @lefthook install
    @just doctor

# Verify the tools and checkout state this repo needs.
doctor:
    #!/usr/bin/env bash
    set -uo pipefail
    fail=0
    need() {
        if command -v "$1" >/dev/null 2>&1; then
            printf '  ok       %s\n' "$1"
        else
            printf '  MISSING  %-12s install: %s\n' "$1" "$2"; fail=1
        fi
    }
    need uv "https://docs.astral.sh/uv/getting-started/installation/"
    need lefthook "brew install lefthook"
    if [ -f "$(git rev-parse --git-path hooks/pre-commit)" ]; then
        printf '  ok       git hooks\n'
    else
        printf '  MISSING  %-12s run: just setup\n' 'git hooks'; fail=1
    fi
    [ "$fail" -eq 0 ] && printf 'Everything in place.\n'
    exit $fail

# A stale shebang (a moved checkout) makes uv skip the console script in favour of
# whatever is on PATH, which then fails with a missing project dependency rather than
# saying the venv is broken.
# Run the test suite through `python -m`.
test:
    @uv run python -m pytest -q

coverage:
    @uv run python -m pytest --cov --cov-report=term-missing -q

lint:
    @uv run ruff check

format:
    @uv run ruff format

# Everything CI runs.
check:
    @uv run ruff check
    @uv run ruff format --check
    @uv run python -m mypy
    @uv run python -m pytest -q

typecheck:
    @uv run python -m mypy

# --reinstall because the version never changes: without it uv audits the existing
# install and keeps shipping the old code.
# Migrate legacy repo state, then install the global `rl`.
install:
    @uv run rl data migrate
    @just vendor
    @uv tool install --reinstall --force .
    @rl update all --yes
    -@rl data presence install --yes
    @echo "Installed: $(which rl)"

# Install `rl` editable: it tracks the working tree, so it never goes stale.
install-editable:
    @uv run rl data migrate
    @just vendor
    @uv tool install --reinstall --force --editable .
    @rl update all --yes
    -@rl data presence install --yes
    @echo "Installed (editable): $(which rl)"

# Stage the hooklinesinker release pinned in scripts/hooklinesinker.json into
# ringleader/_vendor (`--dist DIR` or $HOOKLINESINKER_DIST stages a local build instead).
# Offline, `rl` still installs, just without presence support.
vendor *ARGS:
    -@uv run python scripts/hooklinesinker.py vendor {{ARGS}}

# Pin a published hooklinesinker release: the latest, or the given version.
update-hooklinesinker *ARGS:
    @uv run python scripts/hooklinesinker.py update {{ARGS}}

# Warn when a newer hooklinesinker release is published than the one pinned.
check-hooklinesinker:
    @uv run python scripts/hooklinesinker.py check

uninstall:
    @uv tool uninstall ringleader

# Cut a release with curtains: --dry-run, patch|minor|major or X.Y.Z, --yes.
# The policy's checks already ran `just check`, so the release commit and push skip the hooks.
release *ARGS:
    @LEFTHOOK=0 curtains {{ARGS}}

# Regenerate CHANGELOG.md as if VERSION were tagged.
changelog VERSION:
    @env UV_TOOL_DIR="$PWD/.uv/tools" UV_CACHE_DIR="$PWD/.uv/cache" UV_PYTHON_INSTALL_DIR="$PWD/.uv/python" uvx git-cliff@2.13.1 --config cliff.toml --tag 'v{{VERSION}}' -o CHANGELOG.md

# Enter the throwaway skill-testing world without resetting its state.
sandbox:
    @uv run rl install skills --yes
    @uv run python scripts/sandbox.py --shell

# Reset the sandbox to the frozen fixture and empty registry.
sandbox-reset *ARGS:
    @uv run python scripts/sandbox.py {{ARGS}}

clean:
    @rm -rf .pytest_cache .ruff_cache .coverage htmlcov

# Copy the playbook to each skill whose SKILL.md asks for it.
sync-playbook:
    @for d in .agents/skills/ringleader-*; do \
      if grep -qs 'references/playbook.md' "$d/SKILL.md"; then \
        mkdir -p "$d/references"; \
        cp .agents/skills/PLAYBOOK.md "$d/references/playbook.md"; \
      fi; \
    done
