from __future__ import annotations

import sys as _sys
from collections.abc import Callable

from ..paths import default_registry as default_registry
from . import (
    archive,
    cleaned,
    complete,
    config,
    delete,
    doctor,
    enrich,
    export,
    finish,
    harvest,
    inbox,
    keep,
    mark_harvested,
    mark_mem_reviewed,
    mark_reviewed,
    mem_cleaned,
    mem_prune,
    mem_scan,
    migrate,
    move_workspace,
    name,
    overview,
    presence,
    prune,
    relocate,
    roundup,
    setup,
    snooze,
    sweep,
    sync,
    tokens,
    triage,
    unarchive,
    usage,
    wake,
    whoami,
)
from . import harvests as harvests_cmd

# A leaf runs a command; a group is a nested dict of them. `cli.main` walks this.
CommandFn = Callable[[list[str]], int]


def _reserved(label):
    def _stub(argv):
        print(f"rl {label}: not yet implemented — see Spec 2", file=_sys.stderr)
        return 2

    return _stub


_SESSION = {
    "refresh": sync.cmd_refresh,
    "set": sync.cmd_set,
    "enrich": enrich.cmd_enrich,
    "harvest": harvest.cmd_harvest,
    "finish": finish.cmd_finish,
    "sweep": sweep.cmd_sweep,
    "archive": archive.cmd_archive,
    "unarchive": unarchive.cmd_unarchive,
    "delete": delete.cmd_delete,
    "complete": complete.cmd_complete,
    "snooze": snooze.cmd_snooze,
    "wake": wake.cmd_wake,
    "keep": keep.cmd_keep,
    "unkeep": keep.cmd_unkeep,
    "name": name.cmd_name,
    "relocate": relocate.cmd_relocate,
    "whoami": whoami.cmd_whoami,
    "mark": {
        "harvested": mark_harvested.cmd_mark_harvested,
        "reviewed": mark_reviewed.cmd_mark_reviewed,
        "cleaned": cleaned.cmd_cleaned,
    },
}
_MEM = {
    "refresh": mem_scan.cmd_mem_scan,
    "prune": mem_prune.cmd_mem_prune,
    "triage": triage.cmd_mem_triage,
    "overview": overview.cmd_mem_overview,
    "mark": {
        "cleaned": mem_cleaned.cmd_mem_cleaned,
        "reviewed": mark_mem_reviewed.cmd_mark_mem_reviewed,
    },
}
_DATA = {
    "export": export.cmd_export,
    "tokens": tokens.cmd_tokens,
    "migrate": migrate.cmd_migrate,
    "presence": {
        "install": presence.cmd_install,
        "status": presence.cmd_status,
        "uninstall": presence.cmd_uninstall,
    },
}
_WORKSPACE = {
    "move": move_workspace.cmd_move,
    "retarget": move_workspace.cmd_retarget,
}

TABLE: dict[str, CommandFn | dict] = {
    # flat daily verbs
    "overview": overview.cmd_overview,
    "triage": triage.cmd_triage,
    "prune": prune.cmd_prune,
    "roundup": roundup.cmd_roundup,
    "usage": usage.cmd_usage,
    "doctor": doctor.cmd_doctor,
    "harvests": harvests_cmd.cmd_harvests,
    "inbox": inbox.cmd_inbox,
    "install": setup.cmd_install,
    "update": setup.cmd_update,
    "uninstall": setup.cmd_uninstall,
    # namespaces
    "workspace": _WORKSPACE,
    "session": _SESSION,
    "mem": _MEM,
    "data": _DATA,
    "config": config.cmd_config,
}
