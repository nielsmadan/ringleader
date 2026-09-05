[private]
default:
    @just --list

# A stale shebang (a moved checkout) makes uv skip the console script in favour of
# whatever is on PATH, which then fails with a missing project dependency rather than
# saying the venv is broken.
# Run the test suite through `python -m`.
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

# --reinstall because the version never changes: without it uv audits the existing
# install and keeps shipping the old code.
# Migrate legacy repo state, then install the global `rl`.
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
