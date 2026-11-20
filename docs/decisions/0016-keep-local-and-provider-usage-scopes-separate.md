# 0016: Keep local and provider usage scopes separate

## Context

Local transcripts and databases contain token accounting and occasional quota captures.
Provider requests can supply current subscription quota, API key spend, activity, and
credits. Each measurement has its own units, time window, and account or credential scope.
Opening the inbox is a frequent offline operation. A provider lookup may read a credential
or start an authenticated app-server.

## Decision

- Local usage keeps the existing aggregation and JSON. `rl usage local` gives it an
  explicit command name alongside its existing aliases.
- `rl usage subscription` and `rl usage token` use scope-specific models and a standalone
  version 1 JSON document. Provider figures stay separate from local token estimates.
- Agent login reuse is explicit per provider. Claude also accepts an environment OAuth
  token, and OpenRouter takes each key from the environment. Ringleader stores preferences
  and normalized reports with opaque scope fingerprints. The [model authentication
  decision](0009-reuse-agent-cli-auth-and-store-no-secrets.md) still governs inference.
- Remote cache entries track successful snapshots separately from attempts and retry times.
  Online reuse needs a verified scope match. Offline reads show the last fetched scope
  and timestamp. Codex establishes identity through its app-server response, so an online
  request always queries it.
- Opening a remote view in the inbox or palette reads saved reports. The Usage screen
  makes an online request only when the user presses `r` on a remote view.

## Consequences

The TUI can open reports offline and mark earlier results when a refresh fails or is
cancelled. A user who switches credentials can still see the last fetched scope in an
offline view. Provider integrations have their own expiry, permissions, compatibility,
and rate limits. Those constraints surface in report diagnostics. See [usage
internals](../tech/usage.md) and [provider reference](../reference/provider-usage.md).
