from .capabilities import Adapter, AdapterRegistry
from .claude import ClaudeAdapter
from .pi import PiAdapter


def builtin_adapters() -> AdapterRegistry:
    registry = AdapterRegistry()
    claude = ClaudeAdapter()
    pi = PiAdapter()
    registry.register(Adapter("claude-code", claude, claude, claude))
    registry.register(Adapter("pi", pi, pi, pi))
    return registry
