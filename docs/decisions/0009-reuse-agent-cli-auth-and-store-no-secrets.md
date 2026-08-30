# 0009: Reuse the agent CLI's auth; store no secrets

## Context

`rl session enrich` and `rl session harvest` need a model. The default approach — ask for an
API key and keep it in a config file — would make a session-curation tool into a credential
store, on a machine that already has several authenticated agent CLIs installed.

## Decision

Drive an **already-installed agent CLI** one-shot as the model, reusing that CLI's own auth.
`active_model()` auto-picks the first available of `claude`, then `codex`;
`rl config set ai <claude|codex|llm>` pins a choice.

The `llm` engine is the escape hatch for calling a provider API directly through the
[`llm`](https://llm.datasette.io/) library with the user's own key. It is deliberately **not**
a dependency — `rl config set ai llm` reports an install hint when it is missing.

Ringleader's config stores an engine name, a model name and a provider name. It never stores a
credential.

## Consequences

- No key management, and no secret in the config file to leak into a backup or a bug report.
- Model access depends on an agent CLI being installed; with none, `enrich`/`harvest` no-op and
  classification stays a manual step in the triage skill.
- One-shot CLI invocation is slower than an API call and its output format is the CLI's, not
  ours — the `Model` seam absorbs that.

See [tech/models.md](../tech/models.md).
