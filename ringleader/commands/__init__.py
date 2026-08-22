from __future__ import annotations

import sys as _sys

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
    name,
    overview,
    prune,
    relocate,
    roundup,
    skills,
    snooze,
    sync,
    tokens,
    triage,
    unarchive,
    usage,
    wake,
)
from . import harvests as harvests_cmd


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
}
_SKILLS = {
    "install": skills.cmd_install,
    "status": skills.cmd_status,
    "update": skills.cmd_update,
    "uninstall": skills.cmd_uninstall,
}

TABLE = {
    # flat daily verbs
    "overview": overview.cmd_overview,
    "triage": triage.cmd_triage,
    "prune": prune.cmd_prune,
    "roundup": roundup.cmd_roundup,
    "usage": usage.cmd_usage,
    "doctor": doctor.cmd_doctor,
    "harvests": harvests_cmd.cmd_harvests,
    "inbox": inbox.cmd_inbox,
    # namespaces
    "session": _SESSION,
    "mem": _MEM,
    "data": _DATA,
    "skills": _SKILLS,
    "config": config.cmd_config,
}
