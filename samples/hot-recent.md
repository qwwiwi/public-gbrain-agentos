# Hot — Recent

Working journal. One day at a time. Newest entries at the bottom of each day. The rule: if you are stuck, log it. Silence is a bug — the rest of the team should be able to see when someone is blocked without having to ask.

---

## 2026-03-15

### 09:14 — Alex

Opened the day by re-reading the combat playtest notes from Thursday. Two clear themes: enemy AI feels too predictable in early encounters, and the targeting cursor is hard to read on busy backgrounds. Focusing today on enemy AI — Sam already has a draft of a perception system, I want to spec the behaviour layer on top of it so we can wire them together this week. Cursor visibility is a Jordan task for Monday.

### 11:30 — Sam

Hit a wall on the Godot navmesh. Agent radius parameter is being ignored when I rebake the mesh — agents are clipping through walls that should be navigable around. Spent an hour on it, tried three things from the issue tracker, no luck. Logged as `error-pattern/godot-navmesh-radius-2026-03-15.md` so it does not get lost. Flagging here because silence is a bug: I am stuck and the workaround might cost the rest of the morning. If anyone has hit this on a recent Godot 4.x version, ping me before lunch.

### 13:45 — Jordan

Finished `tideborn-music-loop-v3`. Exported the stems, ran the import script (with proper path quoting this time, lesson learned), dropped the WAVs and the metadata yaml into `50-knowledge/audio/loops/`. Loop point is at bar 16, tested in-engine — clean loop, no click. Sam's audio bus changes from yesterday play nicely with the new mix. Next: ambient layers for the coastal biome, starting Tuesday.

### 15:20 — Alex

Playtest with two friends, ran for 90 minutes. Useful raw notes captured in `90-inbox/2026-03-15-playtest-session.md` — voice-memo style, will compile later. Headline: tutorial pacing is still too slow in the first ten minutes. Both testers correctly described the action point economy by minute five but the tutorial keeps over-explaining it until minute twelve. Cut the redundant prompts. Also one of them found a soft-lock in the second encounter — Sam, will write it up properly after I make tea.

### 17:00 — Sam

End-of-day. Navmesh bug: fixed via workaround. Turned out to be a known issue with concave collision shapes and a specific build setting; documented in the error-pattern note and added the build-setting check to the asset-import checklist. Real fix is upstream and not ours to wait on. Feature-flag system is roughly 60% done — flag registry and runtime evaluation work, but I have not picked the persistence layer yet, deciding between a simple Postgres table and a config-file approach. Will sleep on it. Blocked on nothing for tomorrow.
