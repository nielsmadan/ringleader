from .capabilities import Adapter, AdapterRegistry
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .opencode import OpenCodeAdapter
from .pi import PiAdapter


def builtin_adapters() -> AdapterRegistry:
    registry = AdapterRegistry()
    claude = ClaudeAdapter()
    pi = PiAdapter()
    registry.register(Adapter("claude-code", claude, claude, claude))
    registry.register(Adapter("pi", pi, pi, pi))
    codex = CodexAdapter()
    registry.register(Adapter("codex", codex, codex, codex, codex, codex))
    opencode = OpenCodeAdapter()
    registry.register(
        Adapter("opencode", opencode, opencode, opencode, opencode, identity_allocator=opencode)
    )
    return registry
