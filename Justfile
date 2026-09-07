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
install-local:
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
    @uv tool install --reinstall --force -e .
    @rl update all --yes
    -@rl data presence install --yes
    @echo "Installed (editable): $(which rl)"

refresh-local:
    @just vendor
    @uv tool install --reinstall --force .
    @rl update all --yes
    -@rl data presence install --yes

# Stage the pinned hooklinesinker artifacts into ringleader/_vendor. Without a sibling
# release build there is nothing to stage, and `rl` installs without presence support.
vendor *ARGS:
    -@bash scripts/vendor-hooklinesinker.sh {{ARGS}}

reset-local:
    @uv tool uninstall ringleader

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
