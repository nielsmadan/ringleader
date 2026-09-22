# Architecture decisions

Accepted cross-cutting choices and their tradeoffs. These records are append-only: when a
decision changes, add a new ADR and mark the earlier one superseded rather than rewriting its
history. Current behavior remains canonical in `docs/features/` and `docs/tech/`.

- [0001: Treat the registry as a cache of disk, not the master record](0001-hybrid-registry-as-cache-not-master.md)
- [0002: Build the behavior as skills first, crystallize into a CLI after](0002-skills-first-cli-later.md)
- [0003: Put scanning behind a `Source` seam](0003-scan-behind-a-source-seam.md)
- [0004: Organize the CLI around daily verbs and noun groups](0004-organize-the-cli-around-daily-verbs-and-noun-groups.md)
- [0005: Archive by moving files, not by deleting them](0005-archive-by-moving-not-deleting.md)
- [0006: Refuse to finish a session that was never harvested](0006-harvest-before-finish.md)
- [0007: Ship agent-neutral skills, not a Claude-only plugin](0007-agent-neutral-skill-packaging.md)
- [0008: Separate marking a session finished from archiving it](0008-sweep-archives-only-quiet-transcripts.md)
- [0009: Reuse the agent CLI's auth; store no secrets](0009-reuse-agent-cli-auth-and-store-no-secrets.md)
- [0010: Keep machine-local state outside the repo](0010-keep-machine-local-state-outside-the-repo.md)
- [0011: Hand off to another agent by seeding it, not by converting the session](0011-seeded-handoff-over-native-conversion.md)

These were harvested from the original `SPEC.md` and the design docs under the former
`docs/specs/`, which have been retired in favor of this tree.
