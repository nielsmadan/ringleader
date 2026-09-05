from __future__ import annotations

import argparse
import json
import sys

from ..presence import (
    AGENTS,
    CONSUMER,
    SETUP_COMMAND,
    PresenceClient,
    PresenceUnavailable,
    detected_agents,
)


def _make_client() -> PresenceClient:
    return PresenceClient()


def _confirm(yes: bool) -> bool:
    return True if yes else input("Proceed? [y/N] ").strip().lower() in ("y", "yes")


def _unattended(args) -> bool:
    if args.json and not args.yes:
        print(
            json.dumps(
                {"ok": False, "error": "--json needs --yes: this command edits agent configuration"}
            )
        )
        return True
    return False


def cmd_install(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl data presence install")
    ap.add_argument(
        "--agent",
        action="append",
        dest="agents",
        choices=list(AGENTS),
        help="install hooks for this agent (repeatable); default: every detected agent",
    )
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--json", action="store_true", help="print the outcome as JSON")
    args = ap.parse_args(argv)
    if _unattended(args):
        return 2

    agents = args.agents or list(detected_agents())
    if not args.json:
        print(f"Will register the `{CONSUMER}` consumer with hooklinesinker and install hooks for:")
        for agent in agents or ["(no agents detected)"]:
            print(f"  - {agent}")
        print("This rewrites each agent's own hook configuration.")
    if not _confirm(args.yes):
        print("aborted")
        return 0

    try:
        outcome = _make_client().install_consumer(agents)
    except PresenceUnavailable as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(str(e), file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "ok": outcome.ok,
                    "consumer": CONSUMER,
                    "binary": outcome.binary,
                    "reused_active": outcome.reused_active,
                    "agents": [h.agent for h in outcome.hooks],
                    "hooks": [
                        {"agent": h.agent, "ok": h.ok, "detail": h.detail} for h in outcome.hooks
                    ],
                }
            )
        )
        return 0 if outcome.ok else 1

    print(outcome.detail or f"registered consumer {CONSUMER}")
    for hook in outcome.hooks:
        print(f"  {hook.agent}: {'ok' if hook.ok else 'failed'} {hook.detail}")
    return 0 if outcome.ok else 1


def _status(client: PresenceClient) -> dict:
    artifact: str | None = None
    artifact_error: str | None = None
    candidate: str | None = None
    candidate_error: str | None = None
    try:
        artifact = client.artifact
    except PresenceUnavailable as e:
        artifact_error = str(e)
    if artifact is not None:
        try:
            candidate = str(client.candidate_path())
        except PresenceUnavailable as e:
            candidate_error = str(e)

    report = client.doctor()
    consumers = client.consumers()
    sessions = client.sessions()
    return {
        "platform": f"{client.system}/{client.machine}",
        "artifact": artifact,
        "artifact_error": artifact_error,
        "candidate": candidate,
        "candidate_error": candidate_error,
        "executable": str(client.executable),
        "installed": client.is_installed() and report.installed,
        "version": report.version,
        "registered": CONSUMER in consumers.names,
        "consumers": list(consumers.names),
        "hooks": {agent: client.hook_state(agent) for agent in detected_agents()},
        "running_sessions": sum(1 for s in sessions.sessions if s.running),
        "problems": list(
            dict.fromkeys([*report.problems, *consumers.problems, *sessions.problems])
        ),
        "setup_command": SETUP_COMMAND,
    }


def _print_status(state: dict) -> None:
    print(f"platform: {state['platform']} -> {state['artifact'] or state['artifact_error']}")
    print(f"bundled artifact: {state['candidate'] or state['candidate_error'] or 'none'}")
    if state["installed"]:
        print(f"active binary: {state['executable']} ({state['version'] or 'unknown version'})")
    else:
        print(f"active binary: not installed — run `{state['setup_command']}`")
    if state["registered"]:
        print(f"consumer {CONSUMER}: registered (all consumers: {', '.join(state['consumers'])})")
    else:
        print(f"consumer {CONSUMER}: not registered — run `{state['setup_command']}`")
    for agent, hook_state in state["hooks"].items():
        print(f"  hooks {agent}: {hook_state}")
    print(f"running sessions: {state['running_sessions']}")
    for problem in state["problems"]:
        print(f"  problem: {problem}")


def cmd_status(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl data presence status")
    ap.add_argument("--json", action="store_true", help="print the status as JSON")
    args = ap.parse_args(argv)
    state = _status(_make_client())
    if args.json:
        print(json.dumps(state, indent=2))
    else:
        _print_status(state)
    return 0 if state["registered"] else 1


def cmd_uninstall(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rl data presence uninstall")
    ap.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--json", action="store_true", help="print the outcome as JSON")
    args = ap.parse_args(argv)
    if _unattended(args):
        return 2

    if not args.json:
        print(f"Will remove only the `{CONSUMER}` consumer from hooklinesinker.")
        print("Hooks and the shared binary go only if ringleader was the last consumer.")
    if not _confirm(args.yes):
        print("aborted")
        return 0

    outcome = _make_client().uninstall_consumer()
    if args.json:
        print(json.dumps({"ok": outcome.ok, "ran": outcome.ran, "detail": outcome.detail}))
    else:
        print(outcome.detail)
    return 0 if outcome.ok else 1
