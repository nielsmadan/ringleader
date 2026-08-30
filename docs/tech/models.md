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

`active_model(cfg)` is the single resolution point:

1. `ai = llm` → build `LlmModel` from `llm_provider` + `model`; return it only if available.
2. `ai = claude` / `codex` → build that class; return it only if available.
3. `ai` unset → walk `MODELS` (`[ClaudeCliModel, CodexCliModel]`, in that order) and return the
   first available one.
4. Otherwise `None`.

Returning `None` rather than raising is what makes the no-model path a clean no-op: `enrich` and
`harvest` check for `None` and report that nothing was configured, leaving classification to the
triage skill.

`rl config show` prints the resolved model and its availability, which is the fastest way to see
what a run would actually use.

## Key entry points

- `models/__init__.py` — `MODELS` (the auto-pick order) and `active_model()`.
- `models/base.py` — `Model`, `ModelError`.
- `commands/config.py` — `_VALID_ENGINES = ("claude", "codex", "llm")`.

## Gotchas

- **`MODELS` is the auto-pick order, not the valid-engine list.** It holds only the two agent
  CLIs; `llm` is reachable only by pinning it explicitly, because auto-picking something that
  spends the user's API credit would be wrong.
- **`available()` is checked on every resolution**, so uninstalling an agent CLI silently changes
  which model a run uses when `ai` is unset. Pin it if that matters.
- **The config stores no secret** — an engine name, a model name, a provider name. Credentials
  belong to the agent CLI or to `llm`.

## Why

A protocol with an availability check, rather than a required credential, keeps a
session-curation tool from becoming a credential store on a machine that already has two
authenticated agent CLIs sitting on `PATH`.

## Related

- [0009: reuse agent CLI auth](../decisions/0009-reuse-agent-cli-auth-and-store-no-secrets.md)
- [features/harvest-and-publish.md](../features/harvest-and-publish.md)
