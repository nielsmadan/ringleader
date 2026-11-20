# Provider usage protocols

Provider behavior checked on 2026-11-19 and 2026-11-20 against the revisions and
schemas below. Automated checks use isolated profiles, subprocesses, and reply fixtures.

## Claude Code subscription

The Claude adapter uses `GET https://api.anthropic.com/api/oauth/usage` with a bearer
OAuth access token and `anthropic-beta: oauth-2025-04-20`. It reads the overall five-hour
and weekly windows and model-specific weekly windows. The app uses `user:profile` OAuth
access. An inference-only token cannot supply profile access. The endpoint and credential
format are observed from CodexBar, so compatibility depends on Claude Code and Anthropic's
service behavior. [Claude Code authentication](https://code.claude.com/docs/en/authentication)
[CodexBar adaptation and revision](../../THIRD_PARTY_NOTICES.md)

Agent mode checks `claudeAiOauth` in the selected profile, including `expiresAt` in epoch
milliseconds and the `scopes` list. A custom profile uses its exact `.credentials.json`.
On macOS, the native lookup is limited to the login user's default profile with both
Claude directory overrides unset. It uses the service `Claude Code-credentials` and sets
`kSecUseAuthenticationUI` to `kSecUseAuthenticationUIFail`. If macOS requires a prompt, the
adapter reports the denial. Multiple matches are ambiguous and require another credential
source. Expired credentials require renewal in Claude Code. [Apple Security constant](https://developer.apple.com/documentation/security/ksecuseauthenticationuifail)

## Codex subscription

Codex CLI 0.170.0's generated protocol was checked offline. The adapter starts
`codex app-server --listen stdio:// -c analytics.enabled=false` and sends `initialize`
with `experimentalApi: false`, `requestAttestation: false`, and
`explicitGatewayOauth: true`. After `initialized`, it sends `account/rateLimits/read`
with `excludeResetCreditDetails: true`. It reads `rateLimitsByLimitId` when present and
can interpret the legacy `rateLimits` reply. A nonempty `accountId` and the selected
profile scope the saved snapshot. If `accountId` is missing or empty, any quota snapshot
is saved with an unknown identity. [Pinned account protocol](https://github.com/openai/codex/blob/29eb79e9866b3262a777fdf8195c1b9e63684c91/codex-rs/app-server-protocol/src/protocol/v2/account.rs)
[Pinned request processor](https://github.com/openai/codex/blob/29eb79e9866b3262a777fdf8195c1b9e63684c91/codex-rs/app-server/src/request_processors/account_processor.rs)

The running server inherits the user's `CODEX_HOME` and configuration. It can load
plugins and refresh its existing sign-in as part of normal Codex startup and account
access. The version probe and app-server exchange share a 20-second budget.

## OpenRouter

| Report | GET endpoint at `https://openrouter.ai` | Credential and scope |
| --- | --- | --- |
| Key | `/api/v1/key` | `OPENROUTER_API_KEY` for that key's spend, BYOK spend and limit. [Key API](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key) |
| Activity | `/api/v1/activity` | `OPENROUTER_MANAGEMENT_KEY` for the account or workspace visible to the management key. [Activity API](https://openrouter.ai/docs/api/api-reference/analytics/get-user-activity-grouped-by-endpoint) |
| Credits | `/api/v1/credits` | `OPENROUTER_MANAGEMENT_KEY` for the authenticated user's purchased and used credits. [Credits API](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits) |

The activity schema describes `date` as “Date of the activity (YYYY-MM-DD format)”.
The endpoint returns the last 30 completed UTC days, grouped by date, model and endpoint.
Ringleader uses a single response and narrows that period locally for `--days`. If the
request spans UTC midnight, a later invocation can obtain a snapshot for the new period.
Amounts for OpenRouter and external BYOK stay separate. Prompt and completion counts
can overlap cached and reasoning counts. Credits balance is purchased minus used.
[Activity API](https://openrouter.ai/docs/api/api-reference/analytics/get-user-activity-grouped-by-endpoint)

## Manual checks with configured credentials

Use a terminal with the intended profile and credentials supplied by your environment or
secret manager. Cached commands work in any terminal. They show the last fetched scope
and timestamp when one exists.

```sh
rl usage local windows --json
rl usage subscription --cached --json
rl usage token --cached --json
```

For Claude environment auth, select `env` (the default) and supply
`CLAUDE_CODE_OAUTH_TOKEN` with `user:profile`. The value shown here is a placeholder.

```sh
rl config set usage.claude_auth env
export CLAUDE_CODE_OAUTH_TOKEN='<OAuth access token with user:profile>'
rl usage subscription --source claude --refresh --json
```

To check agent auth, select the desired signed-in profile before starting the command.
On macOS, the default Claude profile with both directory overrides unset exercises the
native Keychain path. Successful access yields Claude windows. A Keychain denial or an
expired credential should produce a scoped diagnostic.

```sh
rl config set usage.claude_auth agent
rl usage subscription --source claude --refresh --json
rl config set usage.codex_auth agent
rl usage subscription --source codex --refresh --json
```

For OpenRouter, supply the ordinary and management keys independently. These example
values are placeholders.

```sh
export OPENROUTER_API_KEY='<ordinary API key>'
export OPENROUTER_MANAGEMENT_KEY='<management key>'
rl usage token --report key --refresh --json
rl usage token --report activity --days 7 --refresh --json
rl usage token --report credits --refresh --json
```

Check `fetched_at`, the opaque `identity` and the report's scope. For activity, confirm
the date range ends at yesterday UTC. Then open `rl inbox`, press `7`, and select
Subscription or Token. Confirm the saved report appears and `r` starts a refresh.

To restore the default login preferences after an agent-auth check, use
`rl config set usage.claude_auth env` and `rl config set usage.codex_auth disabled`.
