#!/usr/bin/env bash
# ringleader dispatcher — run from the repo (or via an absolute path).
# Generic passthrough: `rl <sub> <args...>` -> `.venv/bin/python bin/rl-<sub> <args...>`.
# Repo-local on purpose (not on PATH yet).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$REPO/.venv/bin/python"

sub="${1:-}"
[ "$#" -gt 0 ] && shift || true

usage() {
  cat <<EOF
ringleader — usage: ./rl <command> [args]

EVERYDAY
  rl overview [<selector>] [-s] [-a] [--archived] [--checkouts]
                                  the view — rollup (default), -s session list,
                                  <selector> one-repo deep-dive, --archived, -a incl. parked
  rl triage   [--project <sel>] [-u]   sessions awaiting your decision (auto-refreshed);
                                  -u / --unclassified lists only never-triaged sessions
  rl prune    [--project <sel>] [--delete]   delete tiny + stale sessions (preview by default)

PLUMBING
  rl export   <id> [--out <path>]        render a session to markdown
  rl archive  <id> [--summary-file <md>] move a finished session to archive/ (not resumable)
  rl delete   <id>                       delete one session (files + tombstone)
  rl mark-harvested <id> [--paths a,b]   mark a session harvested (records docs)
  rl mark-reviewed  <id>                 mark an open session reviewed (skip until it changes)
  rl cleaned  <selector>                 record a project as just-triaged
  rl relocate <id> <path>                re-point a session after moving its dir
  rl snooze   <id>                       park a session (hide from default views)
  rl wake     <id>                       un-snooze a session
  rl sync     scan [--project <sel>]     reconcile + emit sessions to classify
  rl name     [--project <sel>] [--dry-run]   apply/refresh session names
  rl tokens   [--days N] [--by day|project|session] [--archived] [--all]
                                  Claude token spend, by real message date (this machine)

<selector> = a git-project label (e.g. juggler) or a cwd path.
EOF
}

case "$sub" in
  ""|-h|--help|help) usage; exit 0 ;;
esac

case "$sub" in
  projects|project|archived)
    echo "rl: '$sub' was merged into 'rl overview'." >&2
    echo "  try: rl overview            (project rollup)" >&2
    echo "       rl overview -s         (session list)" >&2
    echo "       rl overview <selector> (one repo)" >&2
    echo "       rl overview --archived (archived)" >&2
    exit 2 ;;
  review)
    echo "rl: 'review' is now 'rl mark-reviewed'." >&2
    exit 2 ;;
esac

target="$REPO/bin/rl-$sub"
if [ ! -f "$target" ]; then
  echo "rl: unknown command '$sub'" >&2
  usage >&2
  exit 2
fi

exec "$PY" "$target" "$@"
