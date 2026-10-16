# Model seam

`ringleader/models/`: how ringleader reaches a language model without owning a credential.

## Purpose

Two verbs need a model — `rl session enrich` (classify status / whatItIs / name) and
`rl session harvest` (distill a finished session into markdown). Both go through a `Model`
protocol with three implementations, and both **no-op cleanly** when none is available
([0009](../decisions/0009-reuse-agent-cli-auth-and-store-no-secrets.md)).

## How it works

### The protocol

`Model` (`base.py`) is the whole interface: run a prompt, report whether it is usable.
`ModelError` is the failure type callers catch.

### Implementations

| Class | Mechanism |
|---|---|
| `ClaudeCliModel` | drives an installed `claude` binary one-shot, reusing its auth |
| `CodexCliModel` | same, for `codex` |
| `LlmModel` | calls a provider API through the [`llm`](https://llm.datasette.io/) library with the user's own key |

`llm` is **not a dependency**. `LlmModel.available()` returns `False` when it is not installed,
and `rl config set ai llm` reports the install hint.

### Resolution

`active_model(cfg, task)` is the single resolution point (`task` is `"harvest"` by default, or `"enrich"`):

1. `ai = llm` → build `LlmModel` from `llm_provider` + `model`; return it only if available.
2. `ai = claude` / `codex` → build that class; return it only if available.
3. `ai` unset → walk `MODELS` (`[ClaudeCliModel, CodexCliModel]`, in that order) and return the
   first available one.
4. Otherwise `None`.

The model name is per task (`model_name_for`): harvest uses `model`; enrich uses `enrich_model`,
else the engine default in `ENRICH_DEFAULT_MODELS` (`claude` → `haiku`, others none), else `model`.
CLI adapters get `CALL_TIMEOUT_SECONDS[task]` (enrich 120 s, harvest 300 s). `describe()` renders
the choice, and `with_cancel()` returns a copy of a CLI adapter bound to a cancel check.

### Enrich classification

`enrichment.classify` sends `enrich_prompt(thread, tail)` with `ENRICH_SCHEMA`. The prompt is the
project, branch, last prompt and message count, followed by the last `TAIL_CHARS` (6,000)
characters of the rendered transcript. `parse_classification` then accepts only one of
`STATUSES` and non-empty `whatItIs`/`suggestedName`. `ESTIMATED_SECONDS_PER_CALL` (claude 8 s,
codex 15 s, llm 5 s) sizes the roundup's enrich warning.

The defaults are **provisional**: `ENRICH_DEFAULT_MODELS` (`claude` → `haiku`), `TAIL_CHARS`
and `ESTIMATED_SECONDS_PER_CALL` stay as they are until the Claude legs of the
[enrich benchmark](../tests/enrich/README.md) run. A nested `claude -p` cannot authenticate in
the sandboxed session that built the harness. The benchmark's bar decides the defaults: on
held-out labelled sessions, status ≥ 90 % and a mean field pass rate ≥ 90 %.

Returning `None` rather than raising is what makes the no-model path a clean no-op: `enrich` and
`harvest` check for `None` and report that nothing was configured, leaving classification to the
triage skill.

`rl config show` prints the resolved enrich and harvest models, which is the fastest way to see
what a run would actually use.

## Key entry points

- `models/__init__.py` — `MODELS` (the auto-pick order) and `active_model()`.
- `models/base.py` — `Model`, `ModelError`, `CallStats`, `ModelTask`.
- `models/proc.py` — `run_model_process` (the one place a model CLI is spawned) and `kill_all_children`.
- `enrichment.py` — `enrich_prompt`, `TAIL_CHARS`, `ESTIMATED_SECONDS_PER_CALL`; `scripts/benchmark_enrich.py` measures them.
- `commands/config.py` — `_VALID_ENGINES = ("claude", "codex", "llm")`.

## Gotchas

- **`MODELS` is the auto-pick order, not the valid-engine list.** It holds only the two agent
  CLIs; `llm` is reachable only by pinning it explicitly, because auto-picking something that
  spends the user's API credit would be wrong.
- **`available()` is checked on every resolution**, so uninstalling an agent CLI silently changes
  which model a run uses when `ai` is unset. Pin it if that matters.
- **CLI calls are trimmed so they leave nothing behind.** The claude argv adds
  `--no-session-persistence` (no stray session in the picker), `--strict-mcp-config` with an empty
  server list, `--settings '{"disableAllHooks":true}'`, `--setting-sources ""`,
  `--disable-slash-commands` and `--no-chrome`; never `--bare`, which needs `ANTHROPIC_API_KEY`.
  Codex gets `--ephemeral --ignore-rules` (verified on codex-cli 0.162.1; an older codex fails every call with `unexpected argument`) but keeps the user's config,
  which carries their model and provider.
- **Calls run in their own process group in a neutral cwd** (the system temp dir, so no project
  `AGENTS.md`/`CLAUDE.md` loads). Cancel and timeout kill the whole group (SIGTERM, then SIGKILL
  after 2 s), so a grandchild cannot outlive the call; `kill_all_children()` does the same for
  every call still running.
- **The config stores no secret** — an engine name, a model name, a provider name. Credentials
  belong to the agent CLI or to `llm`.

## Why

A protocol with an availability check, rather than a required credential, keeps a
session-curation tool from becoming a credential store on a machine that already has two
authenticated agent CLIs sitting on `PATH`.

## Related

- [0009: reuse agent CLI auth](../decisions/0009-reuse-agent-cli-auth-and-store-no-secrets.md)
- [features/harvest-and-publish.md](../features/harvest-and-publish.md)
- [tests/enrich/](../tests/enrich/README.md) — the enrich model benchmark and its runs
