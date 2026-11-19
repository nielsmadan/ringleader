from __future__ import annotations

from .mem_output import MemoryParser

_GUIDANCE = (
    "Live memory pruning has been retired. Review entries with `rl mem triage` and use "
    "`rl mem delete ID` for an explicitly approved discard. Use `rl mem recovery prune` "
    "to preview expired recovery payloads."
)


def cmd_mem_prune(argv: list[str]) -> int:
    parser = MemoryParser("prune", argv, _GUIDANCE)
    parser.add_argument("--project", action="append", help="retired project filter")
    parser.add_argument("--delete", action="store_true", help="retired live memory deletion flag")
    parser.parse_args(argv)
    return parser.fail(_GUIDANCE, code=2)
