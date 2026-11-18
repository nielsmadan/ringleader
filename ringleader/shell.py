from __future__ import annotations

import os
import stat
from collections.abc import MutableMapping
from pathlib import Path

CWD_FILE_VAR = "RINGLEADER_CWD_FILE"
SHELLS = ("zsh", "bash")

_WRAPPER = """\
rl() {
  case "${1-}" in
    ""|inbox|resume) ;;
    *) command rl "$@"; return ;;
  esac
  local __rl_file __rl_status __rl_dir
  __rl_file=$(mktemp "${TMPDIR:-/tmp}/rl-cwd.XXXXXX") || { command rl "$@"; return; }
  RINGLEADER_CWD_FILE=$__rl_file command rl "$@"
  __rl_status=$?
  { __rl_dir=$(<"$__rl_file"); } 2>/dev/null
  rm -f -- "$__rl_file"
  if [ -n "$__rl_dir" ] && [ -d "$__rl_dir" ] && [ "$__rl_dir" != "$PWD" ]; then
    builtin cd -- "$__rl_dir" || true
  fi
  return $__rl_status
}
"""


def init_script(shell: str) -> str:
    if shell not in SHELLS:
        raise ValueError(f"unsupported shell '{shell}' — pick one of {', '.join(SHELLS)}")
    return _WRAPPER


def _fresh(st: os.stat_result) -> bool:
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == os.getuid()
        and not st.st_size
        and st.st_nlink == 1
    )


def take_cwd_file(environ: MutableMapping[str, str]) -> Path | None:
    # removed at once, so no child of rl — model CLIs, nested rl runs, the agent — inherits it
    value = environ.pop(CWD_FILE_VAR, None)
    if not value:
        return None
    try:
        return Path(value) if _fresh(os.lstat(value)) else None
    except OSError:
        return None


def record_launch_dir(cwd_file: Path, directory: str) -> None:
    fd = os.open(cwd_file, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not _fresh(os.fstat(fd)):
            raise OSError(f"{cwd_file} is not an empty file the rl shell function created")
        os.write(fd, os.fsencode(directory))
    finally:
        os.close(fd)
