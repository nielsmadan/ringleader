#!/bin/sh
# ringleader: archive a session marked `finish on exit`, once the agent has stopped
# writing to its transcript. Registered as a SessionEnd hook by `rl install hooks`.
# Exits 0 unconditionally: a hook must never block an agent from shutting down.
set -u
command -v rl >/dev/null 2>&1 || exit 0
rl session sweep --from-hook >/dev/null 2>&1
exit 0
