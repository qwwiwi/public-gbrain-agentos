# Samples

This folder contains **example vault content** for a fictional team using the second-brain pattern. None of it is real. It exists so you can see what files actually look like in use, before you start writing your own.

## Why fictional

Handing your real agents someone else's notes is a bad idea. Their decisions, learnings, and pointers will leak into your agent's reasoning and bias it toward someone else's problem space. These samples are deliberately **obviously made up** so you read them, get the shape, then delete them.

## How to delete

```bash
rm -rf samples/
```

Do this before you start using the vault for real work. Or move the folder out of the agent's context root if you want to keep it as reference.

## The team

All entries here come from **Northwind Studio**, a fictional 3-person indie game studio building a turn-based strategy game codenamed **Tideborn**.

- **Alex** — lead and design. Owns product decisions, playtest feedback, publisher conversations.
- **Sam** — engineering. Owns tech stack, infra, build pipeline, CI.
- **Jordan** — art and audio. Owns asset pipeline, music, sound design.

## What each file is

- **`decisions.md`** — append-only log of meaningful calls the team made and why. Warm memory: always loaded, scanned before any related new work. Seven entries spanning 2025-08 to 2026-03.
- **`LEARNINGS.md`** — scored lessons promoted from corrections and incidents. Each entry has a confidence score and frequency count so the agent knows which rules are battle-tested.
- **`MEMORY.md`** — pointer index. Ten one-line entries that link out to longer files. This is what the agent reads first to know what exists; it fetches the full file only when the pointer is relevant.
- **`hot-recent.md`** — a single day of working journal entries. Demonstrates the silence-is-a-bug pattern: when someone gets stuck, they log it so the team can see.
- **`inbox-raw-example.md`** — what raw, unstructured input looks like when it lands in the vault. A voice-memo transcript, full of half-formed thoughts. This is realistic; raw notes are messy.
- **`inbox-curated-example.md`** — the same raw note after the compile pipeline ran. Structured, searchable, with decisions and action items extracted. Shows the raw-to-curated transformation that the inbox agent performs.

## Note on links

Some pointers in `MEMORY.md` link to files like `projects/godot-migration.md` that do not exist in this samples folder. Those are illustrative paths to show how a real vault would be cross-linked. If you want to see them resolve, create the matching empty files.
