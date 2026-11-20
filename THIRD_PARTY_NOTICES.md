# Third-party notices

## CodexBar

Ringleader adapts Claude Code OAuth credential and profile selection, usage
request headers, and quota parsing from CodexBar revision
[`1f6da9bec44455288ddee5ef6f3617a803d8eb2a`](https://github.com/steipete/CodexBar/tree/1f6da9bec44455288ddee5ef6f3617a803d8eb2a).

The Python code in `ringleader/usage/claude_credentials.py` adapts
`Sources/CodexBarCore/Providers/Claude/ClaudeConfigPaths.swift` and
`Sources/CodexBarCore/Providers/Claude/ClaudeOAuth/ClaudeOAuthCredentialModels.swift`.
The Python code in `ringleader/usage/claude_subscription.py` and its fixture tests
adapt the request and response schema in
`Sources/CodexBarCore/Providers/Claude/ClaudeOAuth/ClaudeOAuthUsageFetcher.swift`,
with model-scoped weekly limits from
`Sources/CodexBarCore/Providers/Claude/ClaudeScopedWeeklyLimitMapper.swift` and
`Sources/CodexBarCore/Providers/Claude/ClaudeWeb/ClaudeWebExtraRateWindowParser.swift`.

MIT License

Copyright (c) 2026 Peter Steinberger

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
