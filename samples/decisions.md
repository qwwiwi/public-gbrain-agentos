# Decisions — Northwind Studio

Append-only log of decisions the team has made on the Tideborn project. Newest entries at the bottom. Every entry has a date, the context that forced the call, the decision itself, why we chose it, and how to apply it in future work.

---

## 2025-08-12 (Switch combat prototype from real-time to turn-based)

**Context.** Six weeks into prototype work, the real-time combat layer was eating most of the engineering budget. Sam kept hitting netcode complexity that we did not have headcount to solve. Alex ran two playtests with friends and noticed both players were pausing mid-fight to think — a signal that the real-time pressure was friction, not value.

**Decision.** Combat in Tideborn is turn-based. Action point economy, simultaneous resolution at end of round. No real-time mode, not even as a toggle.

**Why.** Three reasons. One: turn-based fits our team size (one engineer cannot build production-quality netcode in parallel with the rest of the game). Two: playtest evidence — testers were already playing it like a turn-based game, fighting the design. Three: it opens up tactical depth we cannot afford to design or balance under real-time constraints.

**How to apply.** Any future feature discussion that assumes real-time inputs (twitch dodging, dynamic terrain hazards mid-action) goes back to the design doc. If a feature needs real-time to work, it does not belong in Tideborn. Re-evaluate this decision only if (a) we hire a second engineer with netcode experience or (b) playtest evidence strongly contradicts the choice across three or more sessions.

---

## 2025-09-03 (Drop Unity license, migrate prototype to Godot 4)

**Context.** Unity Technologies announced pricing changes earlier in the year that affected our projected unit economics for Tideborn. Even after later walk-backs, the trust signal was bad and the licensing path remained uncertain for a small studio. We had two months invested in the Unity prototype and were about to commit to another six.

**Decision.** Migrate the prototype to Godot 4 over the next six weeks. Sam owns the port. No new Unity-specific work starts today.

**Why.** Godot is MIT-licensed, source-available, and the 4.x renderer is good enough for our visual target (stylized 2D with light 3D camera moves). The migration cost (six weeks) is recoverable; staying on a platform where licensing terms can change unilaterally is not. The turn-based combat decision from last month also reduced the engine-feature surface we depend on, which makes Godot a safer fit than it would have been for a real-time game.

**How to apply.** All new tooling and pipeline work targets Godot 4 only. Any third-party asset or plugin we buy must have a Godot export path or be data-only (textures, audio, models). Re-evaluate only if Godot 4.x drops a feature we depend on (unlikely given current roadmap) or if the migration runs past three months elapsed.

---

## 2025-10-18 (Self-host CI runner for asset import speed)

**Context.** Jordan's asset pipeline hit the wall on hosted CI: a full re-import of textures and audio was taking 35–45 minutes per pipeline run, and we run it on every art-branch push. The hosted runner had slow disk and no persistent cache between runs. Sam estimated a self-hosted runner on our existing dev VPS would cut that to under 8 minutes.

**Decision.** Set up a self-hosted CI runner on the studio VPS. Keep hosted runner as fallback for release builds and security-sensitive jobs.

**Why.** Asset import time was directly slowing iteration: Jordan was batching changes to avoid waiting for CI, which delayed feedback. Self-hosted gives us persistent disk cache, faster CPU, and no per-minute billing. Risk (runner compromise) is mitigated by isolating it from production data and only running our own repo.

**How to apply.** Asset-heavy jobs route to `self-hosted` runner label. Release builds and anything touching signing keys stays on hosted. If self-hosted goes down, asset-heavy jobs fall back to hosted automatically — slow but not blocking.

**Alternative considered.** Upgrading to the hosted CI's larger-runner tier. Rejected because the per-minute price would have exceeded the VPS cost within two months, and the persistent cache problem would have remained unsolved.

---

## 2025-11-22 (Adopt feature-flag system before public beta)

**Context.** We are targeting a closed beta in Q1 and an open beta in Q2. Without feature flags, every beta build forces us to ship every half-done system. Last week we had to revert a UI change because it was tangled into the same build as a combat balance pass we wanted to keep.

**Decision.** Implement a lightweight feature-flag system before any public-facing beta build ships. Sam to evaluate three options and pick one by end of November.

**Why.** Beta builds need to be a moving target controlled per-player, not per-build. Without flags, we either ship everything or revert everything; that is too coarse for live testing. Going in early (now, while the codebase is small) is cheaper than retrofitting flags around shipped systems later.

**How to apply.** Any new gameplay system that lands after the flag system is in place ships behind a flag, default off, until it passes internal playtest. Flags get reviewed monthly: anything stable for 30 days either flips to default-on or gets removed. Re-evaluate the choice of flag library only if it fails us in two distinct incidents.

---

## 2026-01-09 (Move analytics from Firebase to PostHog self-hosted)

**Context.** Firebase analytics was free and easy at the prototype stage, but we are about to start collecting beta telemetry — session lengths, combat outcomes, churn signals. Firebase's data export model is awkward for the kind of slicing we want to do, and we are uncomfortable with the long-term data ownership story. Sam ran a one-week test of PostHog on our VPS and found it more than adequate.

**Decision.** All Tideborn analytics now flow to a self-hosted PostHog instance on the studio VPS. Firebase analytics gets turned off after a one-week dual-write overlap for safety.

**Why.** Self-hosted means we own the data, can query it with SQL, and can set retention policy ourselves. PostHog's product-analytics primitives (cohorts, funnels, session replay) are well-suited to a game in beta. Cost is server-only (we already have the VPS). Privacy story is simpler to communicate to beta testers: data stays on our box.

**How to apply.** All new event tracking goes through the PostHog SDK; no new Firebase events. Any analytics consumer (dashboards, exports, the weekly playtest summary) reads from PostHog. Re-evaluate only if PostHog's resource use grows past what our VPS can serve, in which case we look at the managed cloud tier rather than switching products.

---

## 2026-02-14 (Set 90-day data retention for raw analytics)

**Context.** PostHog has been collecting beta telemetry for a month. The raw event table is already at 2.4 GB and growing at roughly 1 GB per week. We do not need raw events long-term — we need rolled-up daily aggregates and the ability to drill in for the last quarter.

**Decision.** Raw event retention is 90 days. Daily aggregates retained indefinitely. Session replays retained 30 days.

**Why.** 90 days covers two release cycles plus a safety margin for post-mortem analysis. Aggregates are small (kilobytes per day per metric) and cheap to keep forever. Session replays are the largest data class and the least often re-watched after the first week, so 30 days is the right cutoff.

**How to apply.** Any new event type gets reviewed for retention class at design time, not later. If a future feature requires longer raw retention (e.g. fraud investigation), the request comes with a justification and a storage-cost estimate.

**Alternative considered.** Keeping raw events for 12 months "just in case". Rejected because storage growth at 1 GB/week would have outpaced the VPS disk budget inside a year, with no concrete query we could not answer from aggregates plus 90-day raw.

---

## 2026-03-30 (Lock Q2 to combat polish, defer multiplayer to Q3)

**Context.** End-of-Q1 retro. The combat system has been the highest-impact lever in every playtest, and tester feedback is consistent: combat is interesting but rough. Meanwhile, multiplayer keeps coming up in roadmap discussions, and Sam has a working asynchronous-turn prototype. The temptation is to ship multiplayer in Q2 because it is the most exciting feature to talk about.

**Decision.** Q2 is locked to combat polish and tutorial work. Multiplayer is deferred to Q3 earliest. No multiplayer feature work merges to main during Q2.

**Why.** The single-player combat loop is the foundation; if it is not satisfying, multiplayer does not save us. Polishing combat in Q2 raises the floor for every later mode, multiplayer included. Deferring multiplayer also lets the async-turn prototype keep maturing in a branch without pressure.

**How to apply.** All Q2 planning meetings filter every proposed task through one question: "does this make single-player combat better?" If no, it goes to Q3 backlog. Multiplayer prototype work can continue in `feature/mp-async` branch but does not block any Q2 milestone. Re-evaluate the multiplayer timing at the Q2 retro based on combat-polish progress.
