# Memory — Northwind Studio

Pointer index. One-line entries that link to longer files. Read this first to know what exists, then fetch the specific file you need.

Some paths below (anything under `projects/`, `runbooks/`, or `playtests/`) refer to files outside this samples folder — they are illustrative, showing how a real vault cross-links.

- [Combat redesign decision](decisions.md#2025-08-12-switch-combat-prototype-from-real-time-to-turn-based) — turn-based pivot, the why and how.
- *Godot migration log* — six-week port, three blockers, what we would do again. (See decisions.md.)
- *Self-hosted CI runner setup* — install steps, monitoring, fallback procedure. (See decisions.md.)
- [Feature-flag system choice](decisions.md#2025-11-22-adopt-feature-flag-system-before-public-beta) — why we adopted flags before beta.
- *PostHog migration plan* — Firebase to self-hosted PostHog, rollback safety window. (See decisions.md.)
- [Migration FK rule](LEARNINGS.md#score-085--freq-4--migrations-drop-fks-before-truncating-recreate-after) — drop foreign keys before truncate, recreate after.
- [Async standups rule](LEARNINGS.md#score-092--freq-6--daily-standups-in-writing--sync-calls-for-async-team) — written standups beat sync calls for small teams.
- [Q2 lock decision](decisions.md#2026-03-30-lock-q2-to-combat-polish-defer-multiplayer-to-q3) — combat polish only, multiplayer deferred.
- *Playtest #14 notes* — tutorial pacing feedback from two-friend session. (See inbox-curated-example.md for a related curated note.)
- [Asset path quoting rule](LEARNINGS.md#score-070--freq-2--asset-import-failures-usually-mean-a-file-path-with-a-space-quote-variables) — always quote shell variables in import scripts.
