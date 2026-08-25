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
    @uv run rl data migrate
    @uv tool install .
    @rl skills update --yes
    @echo "Installed: $(which rl)"

# Install `rl` editable: it tracks the working tree, so it never goes stale.
install-editable:
    @uv run rl data migrate
    @uv tool install --reinstall --force -e .
    @rl skills update --yes
    @echo "Installed (editable): $(which rl)"

refresh-local:
    @uv tool install --reinstall --force .
    @rl skills update --yes

reset-local:
    @uv tool uninstall ringleader

# Regenerate the committed sandbox fixture from your own archive (rarely needed).
sandbox-refreeze *ARGS:
    @uv run python scripts/sandbox.py --refreeze {{ARGS}}

# Throwaway world for testing the skills from zero state (freezes real sessions on first run).
sandbox *ARGS:
    @uv run python scripts/sandbox.py {{ARGS}}

clean:
    @rm -rf .pytest_cache .ruff_cache .coverage htmlcov

sync-playbook:
    @for s in init triage cleanup overview usage; do \
      cp .agents/skills/PLAYBOOK.md ".agents/skills/ringleader-$s/references/playbook.md"; \
    done
