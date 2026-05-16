# Learnings — Northwind Studio

Scored lessons promoted from incidents and corrections. Each entry shows the confidence score, how many times the pattern has recurred, the rule itself, the incident that surfaced it, and when to apply it.

Score scale: 0.0–1.0. Anything above 0.80 is treated as a hard rule. Frequency count tracks how many distinct incidents have reinforced the lesson — high frequency raises trust even if individual scores are lower.

---

## Score 0.85 / freq 4 — Migrations: drop FKs before truncating, recreate after.

Sam ran a schema migration on the dev Postgres that needed to truncate three event tables and reload them with corrected types. First attempt failed with foreign-key violations because related tables held references. Second attempt dropped the FKs first, truncated, reloaded, recreated FKs — clean run. We have now hit this pattern on PostHog event-schema changes, on a player-progress migration, on the audio-metadata refactor, and once during a backup-restore drill.

**Apply when:** writing any migration that truncates or rebuilds a table with incoming FK references. Always check `pg_constraint` for referencing tables before the destructive step; drop those FKs in the same transaction; recreate them after the reload completes.

---

## Score 0.78 / freq 3 — Always test feature flags with the flag DISABLED first.

We shipped the new tutorial UI behind a flag, defaulted on for internal builds. A week later a beta tester reported the old tutorial was completely broken — turned out our flag's "off" branch had never been exercised because everyone tested with it on. Three separate incidents have followed the same shape: a flag that only worked in its "new code path" state because nobody verified the fallback still ran.

**Apply when:** adding any feature flag. The first test pass runs the code with the flag explicitly off, confirming the old behaviour still works. Only then test with the flag on. CI should run both branches when feasible; the "flag off" state is the safety net and deserves equal coverage.

---

## Score 0.92 / freq 6 — Daily standups in writing > sync calls for async team.

For the first two months of the project we did 15-minute video standups three times a week. They cost an hour per person per week and the notes were terrible. We switched to async written standups in our team Discord — one short message per person per day, format "yesterday / today / blocked." Within two weeks we had higher signal, searchable history, and reclaimed roughly two hours per person per week. The pattern has reasserted itself every time we have tried to add a sync meeting back: each one has been replaceable by a structured async message. Six clear data points across the project so far.

**Apply when:** any recurring status meeting is proposed for a team smaller than five people. Default to async written form first; only schedule a sync meeting when the topic genuinely requires interactive discussion (design debates, retros, conflict resolution). Status updates, blockers, and progress reports stay written.

---

## Score 0.70 / freq 2 — Asset import failures usually mean a file path with a space; quote variables.

Jordan's asset import script broke twice in three months. Both times the cause was a directory name containing a space (`Tideborn Audio Drafts/` the first time, `Battle Loops v2/` the second), and the script was passing the path unquoted to a downstream tool. Shell expansion treated it as multiple arguments and the import silently skipped files. Score is lower than the others because we have only two incidents, but the pattern is well-understood and the fix is mechanical.

**Apply when:** writing or reviewing any shell script that handles user-supplied or asset-supplied paths. Always quote variables — `"$VAR"`, not `$VAR`. When debugging an "asset not found" or "file count mismatch" error in an import pipeline, the path-with-space hypothesis is the first thing to check.
