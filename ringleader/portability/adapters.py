from .capabilities import Adapter, AdapterRegistry, Verifier
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .droid import DroidAdapter
from .opencode import OpenCodeAdapter
from .pi import PiAdapter
from .roots import RootLayout
from .verification import VerificationRecord
from .verify import claude as claude_harness
from .verify import codex as codex_harness
from .verify import droid as droid_harness
from .verify import opencode as opencode_harness
from .verify import pi as pi_harness


def builtin_adapters(verified: tuple[VerificationRecord, ...] = ()) -> AdapterRegistry:
    registry = AdapterRegistry()
    claude = ClaudeAdapter(verified=verified)
    pi = PiAdapter(verified=verified)
    registry.register(
        Adapter(
            "claude-code",
            claude,
            claude,
            claude,
            roots=RootLayout(
                "CLAUDE_CONFIG_DIR", ".claude", "SESSION_TRACKER_PROJECTS_DIR", "projects"
            ),
            verifier=Verifier(claude.executable, claude_harness.run),
        )
    )
    registry.register(
        Adapter(
            "pi",
            pi,
            pi,
            pi,
            roots=RootLayout("PI_CODING_AGENT_DIR", ".pi/agent", "PI_SESSIONS_DIR", "sessions"),
            verifier=Verifier(pi.executable, pi_harness.run),
        )
    )
    codex = CodexAdapter(verified=verified)
    registry.register(
        Adapter(
            "codex",
            codex,
            codex,
            codex,
            codex,
            codex,
            roots=RootLayout("CODEX_HOME", ".codex", "CODEX_SESSIONS_DIR", "sessions"),
            verifier=Verifier(codex.executable, codex_harness.run),
        )
    )
    opencode = OpenCodeAdapter(verified=verified)
    registry.register(
        Adapter(
            "opencode",
            opencode,
            opencode,
            opencode,
            opencode,
            identity_allocator=opencode,
            roots=RootLayout("", "", database=True),
            verifier=Verifier(opencode.executable, opencode_harness.run),
        )
    )
    droid = DroidAdapter(verified=verified)
    registry.register(
        Adapter(
            "droid",
            droid,
            droid,
            droid,
            roots=RootLayout("FACTORY_HOME_OVERRIDE", ".factory", names_home=True),
            verifier=Verifier(droid.executable, droid_harness.run),
        )
    )
    return registry
