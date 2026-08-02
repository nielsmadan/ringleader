[private]
default:
    @just --list

test:
    @uv run pytest -q

cov:
    @uv run pytest --cov --cov-report=term-missing -q

lint:
    @uv run ruff check

fmt:
    @uv run ruff format

# Everything CI runs.
check:
    @uv run ruff check
    @uv run ruff format --check
    @uv run pytest -q

# Optional; not part of `check`.
typecheck:
    @uv run mypy ringleader || true

# Migrate legacy repo state, then install the global `rl`.
install-local:
    @uv run rl migrate
    @uv tool install .
    @echo "Installed: $(which rl)"

refresh-local:
    @uv tool install --reinstall --force .

reset-local:
    @uv tool uninstall ringleader

clean:
    @rm -rf .pytest_cache .ruff_cache .coverage htmlcov
