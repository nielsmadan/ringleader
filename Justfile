[private]
default:
    @just --list

# `python -m`, not the console script: a script whose shebang went stale (a moved
# checkout) is skipped by uv in favour of whatever is on PATH, which then fails with
# a missing project dependency rather than saying the venv is broken.
test:
    @uv run python -m pytest -q

cov:
    @uv run python -m pytest --cov --cov-report=term-missing -q

lint:
    @uv run ruff check

fmt:
    @uv run ruff format

# Everything CI runs.
check:
    @uv run ruff check
    @uv run ruff format --check
    @uv run python -m mypy
    @uv run python -m pytest -q

typecheck:
    @uv run python -m mypy

# Install git hooks (lefthook).
hooks:
    @lefthook install

# Migrate legacy repo state, then install the global `rl`.
# --reinstall because the version never changes: without it uv audits the existing
# install and keeps shipping the old code.
install-local:
    @uv run rl data migrate
    @uv tool install --reinstall --force .
    @rl update all --yes
    @echo "Installed: $(which rl)"

# Install `rl` editable: it tracks the working tree, so it never goes stale.
install-editable:
    @uv run rl data migrate
    @uv tool install --reinstall --force -e .
    @rl update all --yes
    @echo "Installed (editable): $(which rl)"

refresh-local:
    @uv tool install --reinstall --force .
    @rl update all --yes

reset-local:
    @uv tool uninstall ringleader

# Enter the throwaway skill-testing world without resetting its state.
sandbox:
    @uv run rl skills install --yes
    @uv run python scripts/sandbox.py --shell

# Reset the sandbox to the frozen fixture and empty registry.
sandbox-reset *ARGS:
    @uv run python scripts/sandbox.py {{ARGS}}

clean:
    @rm -rf .pytest_cache .ruff_cache .coverage htmlcov

sync-playbook:
    @for s in init triage cleanup overview usage finish; do \
      cp .agents/skills/PLAYBOOK.md ".agents/skills/ringleader-$s/references/playbook.md"; \
    done
