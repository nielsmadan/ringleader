from .capabilities import Adapter, AdapterRegistry
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .opencode import OpenCodeAdapter
from .pi import PiAdapter
from .roots import RootLayout


def builtin_adapters() -> AdapterRegistry:
    registry = AdapterRegistry()
    claude = ClaudeAdapter()
    pi = PiAdapter()
    registry.register(
        Adapter(
            "claude-code",
            claude,
            claude,
            claude,
            roots=RootLayout(
                "CLAUDE_CONFIG_DIR", ".claude", "SESSION_TRACKER_PROJECTS_DIR", "projects"
            ),
        )
    )
    registry.register(
        Adapter(
            "pi",
            pi,
            pi,
            pi,
            roots=RootLayout("PI_CODING_AGENT_DIR", ".pi/agent", "PI_SESSIONS_DIR", "sessions"),
        )
    )
    codex = CodexAdapter()
    registry.register(
        Adapter(
            "codex",
            codex,
            codex,
            codex,
            codex,
            codex,
            roots=RootLayout("CODEX_HOME", ".codex", "CODEX_SESSIONS_DIR", "sessions"),
        )
    )
    opencode = OpenCodeAdapter()
    registry.register(
        Adapter(
            "opencode",
            opencode,
            opencode,
            opencode,
            opencode,
            identity_allocator=opencode,
            roots=RootLayout("", "", database=True),
        )
    )
    return registry
