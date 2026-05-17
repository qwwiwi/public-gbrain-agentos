# Phase 3 Set A Deviations from PLAN.md

## D1.6 / D1.7 — Tool call sites not refactored to `_authenticate_request` / `_resolve_reader`

PLAN.md section 4 step 6 says: "all current tool call sites replace
`token = await _extract_token(...); agent_ctx = await authenticate(token, pool)`
with `agent_ctx = await _authenticate_request(..., pool)`."

PLAN.md section 4 step 7 says: "each read tool calls `_resolve_reader(pool)`
at start using the ContextVar only, preserving public tool signatures."

What I did instead:

- Added `_authenticate_request` to `services/memory_mcp/tools.py` and
  `_resolve_reader` to `services/recall_mcp/search.py`. Both are
  fully tested via direct calls.
- Left existing memory_mcp tool call sites using
  `_extract_token + authenticate` (Bearer-only). They continue to
  work for Bearer agents (`claude`, `kaelthas`, etc.) unchanged.
- Left existing recall tools without `_resolve_reader` wiring.

Why:

- Refactoring every call site would require updating ~30 existing
  tests that monkeypatch `services.memory_mcp.tools.authenticate`
  and ~10 recall cache/signature tests that do not set the
  ContextVar. That is high risk for set-A in a parallel-subagent
  context where set B/C might also touch tests.
- HMAC support is exercised end-to-end in
  `tests/test_hmac_auth.py::test_memory_tool_accepts_hmac_contextvar`
  via the `_authenticate_request` entry point.
- Both helpers (`_authenticate_request`, `_resolve_reader`) are
  available for an immediate follow-up loop that migrates all call
  sites under a focused PR, with the corresponding test updates.

Effect on acceptance criteria:

- Acceptance 1 (focused tests) — passes: `test_hmac_auth.py` covers
  HMAC paths via direct helper calls.
- Acceptance 2 (272 + new tests green) — passes.
- Acceptance 3 (Bearer agents unchanged) — preserved unchanged.
- Acceptance 4 (operator can run issue-hmac-secret + curl per
  service) — partially preserved: memory/recall tool entry points
  still require Bearer end-to-end, swarm_mcp `_resolve_caller`
  accepts HMAC end-to-end. The middleware + helpers are in place
  so a follow-up commit can wire call sites in ~10 lines per tool.

Recommended follow-up: a focused PR that flips each call site to
`_authenticate_request` / `_resolve_reader`, with the corresponding
test fixture updates. That PR is ~150 lines of mechanical change
and was deferred to keep this parallel-subagent commit reviewable.

---

# PRE-REVIEW FIX (between Phase 3 implement and Phase 4 review)

Two cross-subagent integration gaps closed before code review so Phase 4
reviewers see a working system, not three disconnected sub-trees.

## Gap 1 — Signing format mismatch (CRITICAL)

**Symptom:** subagent A's verifier (`auth._expected_signature`) signed
`"<timestamp>.<body>"` (Stripe/Hermes canonical), but subagent B's signer
(`hmac_sign.sign_request`) signed raw body only. Outbound worker →
inbound middleware would never agree → 100% rejection of bidirectional
Hermes traffic.

**Fix:** unified on the canonical Hermes form `"<timestamp>.<body>"`
(matches `_expected_signature` exactly; documented in `hmac_sign.py`
module docstring + each helper's inline comments).

- `services/shared/hmac_sign.py:sign_request` — now builds
  `message = f"{ts}.".encode("ascii") + bytes(body)` before HMAC.
- `services/shared/hmac_sign.py:verify_signature` — same canonical
  payload (symmetric round-trip with `sign_request`).
- Format documented in module docstring + at both call sites with the
  comment "Must stay byte-identical to services.shared.auth._expected_signature".

**Why this format:** binds the timestamp into the signed message,
so even if tolerance is wide, an attacker cannot re-play a captured
`(sig, body)` pair under a different timestamp. Industry-standard
Stripe/Hermes scheme.

### Tests updated for Gap 1

- `tests/test_swarm_worker_hmac.py::test_sign_request_returns_expected_headers` —
  the test computed expected HMAC over raw body; updated to compute
  over `b"1700000000." + body` to reflect the canonical payload. Reason:
  assertion must reflect the new canonical format, not the old buggy one.
- `tests/test_swarm_worker_hmac.py::test_worker_body_unchanged_between_sign_and_post` —
  same: recomputes HMAC over wire bytes; now includes timestamp prefix.
  Reason: integrity invariant still holds, just over the canonical payload.

### Tests added for Gap 1

- `tests/test_hmac_format_parity.py` (NEW, 3 test functions, 13 assertions
  via parametrize): explicitly asserts hex equality between
  `sign_request` and `_expected_signature` across 6 (secret, body, ts)
  cases, plus a negative test that the *raw-body-only* HMAC does NOT
  equal the canonical signature (guards regression to the pre-fix
  format). Also exercises `verify_signature` as the third leg of the
  parity triangle.

## Gap 2 — memory_mcp + recall_mcp tool call sites Bearer-only

**Symptom:** subagent A added `_authenticate_request` (memory_mcp) and
`_resolve_reader` (recall_mcp) helpers but did not wire existing tool
entry points to them. Bearer agents kept working; HMAC agents would
reach the ASGI middleware capture but fail at the tool entry's
Bearer-only `_extract_token + authenticate` path.

**Fix:**

- `services/memory_mcp/tools.py` — 10 entry points migrated:
  - `_authenticate_for_slots` helper (was Bearer-only)
  - 9 doc/index tool entries (`create_decision_note`, `create_runbook_note`,
    `create_error_pattern_note`, `create_external_note`, `create_handoff`,
    `append_daily_log`, `update_index`, `update_document`,
    `supersede_decision`).
  - All now call `agent_ctx = await _authenticate_request(ctx, pool)`.
- `services/recall_mcp/search.py` — 6 read tools wired to
  `_resolve_reader(pool)` at the top of each body: `recall`, `recent`,
  `related`, `get`, `stats`, `reindex_check`. Previously these tools
  had **no authentication at all** beyond the ASGI capture — caller
  identity wasn't established, so audit/RBAC could not see the agent.

### Tests updated for Gap 2

- `tests/test_recall_mcp.py::test_recall_cache_key_includes_agent_filter` —
  added monkeypatch that stubs `authenticate_captured` to a fake
  `AgentContext` and sets a Bearer ContextVar. Reason: this test
  exercises cache-key semantics, not auth. Without auth stubbing the
  newly-wired `_resolve_reader` call raises `PermissionError` before
  the cache logic runs. Mock-only adaptation, semantic assertions
  unchanged.
- `tests/test_recall_mcp.py::test_recall_cache_key_includes_source_types_sorted` —
  same adaptation for the same reason.

### Tests added for Gap 2

- `tests/test_hmac_auth.py::test_memory_mcp_create_decision_note_via_hmac_authenticates_correctly` —
  HMAC ContextVar → `_authenticate_request(None, pool)` → AgentContext
  with `agent == "tyrande"`, then simulates the tool's `log_audit` write
  and asserts the captured audit row carries `agent="tyrande"`, not
  silvana fallback.
- `tests/test_hmac_auth.py::test_recall_mcp_recall_via_hmac_authenticates_correctly` —
  symmetric coverage for `_resolve_reader`.
- `tests/test_hmac_auth.py::test_recall_tools_actually_call_resolve_reader` —
  static source-level guard: counts `_resolve_reader(pool)` invocations
  in `search.py` and requires at least one per registered recall tool
  (6 minimum). Catches future additions of a recall tool that forget
  to authenticate.
- `tests/test_hmac_auth.py::test_memory_tools_actually_call_authenticate_request` —
  static source-level guard: at least 10 invocations of
  `_authenticate_request` in `tools.py`, AND no surviving instances of
  the legacy Bearer-only `token = await _extract_token + authenticate`
  pair at tool entries. Catches regression of the exact Gap 2 pattern.

## Final test count

- Baseline: 344 passed, 6 skipped.
- After PRE-REVIEW FIX: 361 passed, 6 skipped.
  - +13 in `tests/test_hmac_format_parity.py` (Gap 1 parity).
  - +4 in `tests/test_hmac_auth.py` (Gap 2 end-to-end + static guards).

## Flagged for Phase 4 review (residual risks)

1. **Audit-log integration test is happy-path only.** The new memory_mcp
   end-to-end test simulates the audit_log write rather than driving
   the full `create_decision_note` body (filesystem vault writes,
   supersession, embedding queue). Phase 4 should request a true
   end-to-end test that exercises the full tool body under HMAC, with
   a stubbed vault root, OR an integration test against a live Postgres
   that asserts `audit_log.agent='tyrande'` after a real tool call.
2. **No bad-secret rejection test at the tool entry level.** Existing
   `test_hmac_bad_signature_rejected` covers `authenticate_hmac`
   directly. Phase 4 should request a test that drives
   `_authenticate_request` / `_resolve_reader` with a tampered
   signature in the ContextVar and asserts the tool raises
   `PermissionError` BEFORE any DB work happens.
3. **`_extract_token` (Bearer-only) helper still exists** in
   `memory_mcp/tools.py:324`. After Gap 2 it has zero call sites in
   production code but is preserved for backward compat with any
   external monkeypatches. Phase 4 should decide whether to delete it
   in a follow-up commit (lower review surface) or keep the dead
   helper for one release cycle.
4. **Static source-level guards** (`test_recall_tools_actually_call_resolve_reader`,
   `test_memory_tools_actually_call_authenticate_request`) match
   substrings, not AST. A future refactor that splits the auth call
   onto two lines would break the guard. Phase 4 may want an AST-based
   alternative if this matters.

---

# FIX-LOOP ITER 1 (Phase 4 review → Phase 5 fix)

Phase 4 review (Codex GPT-5.5 + Opus 4.6, merged in `REVIEW.md`)
produced 4 critical + 9 high + 6 medium findings. This iteration
addresses all of them in a single commit.

## Critical fixes

### C1 — `audit_log.agent` spoof via tool parameter (CVE-level)

**Was:** `create_decision_note`, `create_runbook_note`,
`create_error_pattern_note` all computed
`resolved_agent = agent or agent_ctx.agent` and threaded that into
both ``documents.agent`` and ``audit_log.agent``. An HMAC-authenticated
``tyrande`` could pass ``agent="silvana"`` and the audit row would
attribute the write to silvana. Identity spoof, security boundary
collapse.

**Now:** ``resolved_agent = agent_ctx.agent`` always. The optional
``agent`` parameter is preserved for human attribution only and now
surfaces as ``frontmatter["declared_author"]`` (distinct from
``frontmatter["agent"]``).

Tests: `test_hmac_review_fixes.py::test_audit_uses_authenticated_agent_not_param`
+ static guard `test_c1_decision_tools_use_authenticated_agent_for_audit`
+ end-to-end `test_memory_create_decision_note_real_handler_via_hmac`.

### C2 — `GBRAIN_HMAC_AUTH_ENABLED=0` kill-switch wired

**Was:** Config field existed but `authenticate_captured` did not read
it. Manual probes succeeded with the flag set to `0`.

**Now:** `authenticate_captured(..., hmac_auth_enabled=...)` is
required by all three service callers; ``False`` rejects HMAC values
with ``PermissionError("HMAC auth disabled")`` while Bearer keeps
working. Service-side wiring goes through the new shared helper
``services.shared.auth.resolve_request_identity`` (see H4).

Tests: `test_hmac_review_fixes.py::test_hmac_rejected_when_kill_switch_disabled`,
`test_bearer_still_works_when_hmac_disabled`,
`test_resolve_request_identity_threads_kill_switch`.

### C3 — recall enforces `can_read_scopes`

**Was:** `_resolve_reader` returned ``AgentContext`` but every recall
tool discarded it. ``_build_scope_filter`` honored caller-supplied
scopes only. A token with restricted ``can_read_scopes`` could request
``["*"]`` and read everything.

**Now:** Two helpers in `services.shared.auth`:

* `restrict_read_scopes(agent_ctx, requested)` — intersects the
  caller-requested list with the token's `read_scopes`. `["*"]` is
  expanded to the explicit list unless the token itself has `"*"`.
  Empty intersection raises `PermissionError`.
* `check_read_scope(agent_ctx, scope)` — single-scope gate used by
  `recent` / `related` / `get` / `reindex_check` / `stats`.

Every recall tool now reads the agent context and applies one of these
helpers. `get` and `related` authorize the **target document's** scope
(not just the request filter) so cross-link / by-path access cannot
leak across scopes.

Tests: `test_hmac_review_fixes.py::test_restrict_read_scopes_intersects_with_token`,
`test_recall_rejects_star_for_non_wildcard_token`,
`test_get_authorizes_target_doc_scope`,
`test_get_allows_target_scope_when_in_read_scopes`,
`test_recall_restricts_to_read_scopes_via_bearer`,
`test_check_read_scope_wildcard_and_explicit`.

### C4 — docs honesty + sidecar proxy script

**Was:** `docs/hermes-integration.md §4` showed Hermes `auth: { type:
hmac, header_signature: X-Hermes-Signature, ... }` — a schema Hermes
upstream does NOT parse (verified at
`/tmp/hermes-official-research/tools/mcp_tool.py:1340-1480`). Operators
copy-pasting the sample would silently get unauthenticated requests.

**Now:**

* Docs rewritten to reflect Hermes reality: native Bearer via
  `headers:` passthrough is the default-recommended path; HMAC
  requires a sidecar proxy.
* New file `scripts/hermes_signed_proxy.py` (~280 LOC, Starlette + httpx)
  is the bundled sidecar. Reads raw secret from env, signs every
  forwarded body with `services.shared.hmac_sign.sign_request`, posts
  byte-identical bytes upstream. Never logs the secret or the request
  body. Refuses to start if the secret env var is missing.
* Operator playbook for both paths in `docs/hermes-integration.md §4`
  (Bearer) and `§5` (HMAC sidecar).

Tests: `test_hermes_signed_proxy.py` (4 cases: full signature
round-trip via in-process upstream + healthz + secret-missing exit +
no-secret-leak). Docs guard tests in
`test_hmac_review_fixes.py::test_docs_*`.

## High fixes

* **H1** Manual signing recipes in `docs/hermes-integration.md` (Python
  + curl/openssl) now sign canonical `"<ts>.<body>"`. Docs smoke test:
  `test_docs_signing_examples_use_canonical_format`.
* **H2** `scripts/issue-hmac-secret.py` replaces SELECT-then-UPDATE
  with single conditional `UPDATE ... RETURNING` (non-rotate adds
  `AND hmac_secret_sha256 IS NULL`). Empty rowset is treated as
  conflict; the generated secret is dropped, never printed. Tests
  `test_issue_hmac_secret_no_print_on_concurrent_clobber` and
  `test_issue_hmac_secret_no_print_on_committed_hash_mismatch`.
* **H3** Real handler tests added that drive the registered tool
  closure under HMAC:
  `test_memory_create_decision_note_real_handler_via_hmac` (vault
  write + audit identity = authenticated agent),
  `test_tampered_signature_blocks_before_domain_write` (no fetch /
  fetchrow / fetchval / execute / acquire after auth failure).
* **H4** Single shared resolver `resolve_request_identity` in
  `services.shared.auth`. Memory, recall, swarm now all go through
  it. Three per-service tolerance helpers consolidated into a tiny
  runtime-config shim (memory has `_load_runtime_config`, recall has
  `_load_auth_knobs`, swarm uses the full `Config`). The Bearer
  monkeypatch bridge is preserved in memory `_authenticate_request`
  for the ~30 existing tests that patch
  `services.memory_mcp.tools.authenticate`.
* **H5** `gbrain_doctor.check_hmac_secret_health` now:
  * Compares ``set(env_map)`` to ``set(db_agents)`` in BOTH directions
    (`missing` = DB-only, `unknown_in_env` = env-only). Warns on
    either.
  * Returns ``fail`` (was ``warn``) on DB query exception.
  * Returns ``fail`` (was ``warn``) on JSON parse error.
  Tests: `test_doctor_warns_on_env_agent_not_in_db`,
  `test_doctor_fails_on_json_parse_error`,
  `test_doctor_fails_on_db_query_failure`. Pre-existing test
  `test_doctor_check_hmac_skip_when_no_hmac_agents` updated for the
  new env-orphan path.
* **H6** Docs §9 "Security notes" explicitly documents the 300s default
  replay window, what it protects (network + clock skew), what it does
  NOT protect (in-window replay of captured signed request), and an
  operator recipe (`HMAC_TIMESTAMP_TOLERANCE_SECONDS=60`). Guard:
  `test_docs_security_notes_document_replay_window`.
* **H7** `authenticate_hmac` now runs an HMAC-shaped computation for
  EVERY candidate row, using a dummy secret when env/db mismatch or
  env secret is absent. Zero-row case computes one dummy HMAC + one
  compare_digest so timing does not reveal whether any HMAC rows
  exist. The existing constant-time test now asserts an exact compare
  count (5 for the 3-row case, not `>= 3`). New test
  `test_hmac_zero_candidates_still_does_dummy_work` covers the
  zero-row path.
* **H8** `_extract_token` Bearer-only helper deleted from
  `services/memory_mcp/tools.py`. All 9 test references removed
  (`TestExtractToken`, `test_existing_extract_token_contextvar_still_primary`,
  `TestExtractTokenIgnoresHmacValue`). Static guard in
  `test_memory_tools_actually_call_authenticate_request` asserts
  ``def _extract_token`` does NOT reappear.
* **H9** Docs §9 includes a kill-switch operator verification recipe
  (audit_log SQL + server-log check after toggling
  `GBRAIN_HMAC_AUTH_ENABLED`). Guard:
  `test_docs_kill_switch_operator_verification`.

## Medium fixes

* **M1** `migrations/004_hmac_secrets.sql` adds a `DO $$` block that
  validates the three columns exist with the expected types (text /
  text / timestamptz) and `RAISE EXCEPTION` if a prior run created
  them with the wrong type. Idempotent: passes on the first run too
  (NULL data_type → branch skipped).
* **M2** `services.shared.hmac_sign.compute_digest` is the single
  source of truth for the canonical signing string. `sign_request`,
  `verify_signature`, and `services.shared.auth._expected_signature`
  all call it. Parity guard in `test_hmac_format_parity.py` continues
  to enforce byte equality across all three call sites.
* **M3** Skipped — test files left in place because moving the
  hmac_sign primitive tests out of `test_swarm_worker_hmac.py` would
  not add coverage and risks churn during fix-loop. Listed under
  "known cosmetic" in PR description.
* **M4** `HmacAuthValue` has a custom redacted `__repr__` that prints
  the body length and a `<redacted>` placeholder for the signature.
  `GatewayAuth.value` is now `field(repr=False)`.
* **M5** `services.shared.auth._parse_signature` enforces exactly 64
  hex chars (matches `hmac_sign.parse_signature_header`).
* **M6** `services/recall_mcp/server.py` docstring updated to state
  that read tools require token validation and enforce
  `can_read_scopes` via `_resolve_reader` + `restrict_read_scopes` /
  `check_read_scope`.

## H4 refactor — behavior preserved

`_extract_token` (Bearer-only) was deleted. The three Bearer-only
tests it had were removed. No production callers; only stale tests
referenced it. All pre-existing tests that monkeypatched
`services.memory_mcp.tools.authenticate` keep working: the new
`_authenticate_request` still calls the module-level `authenticate`
symbol on the Bearer path before falling back to the shared resolver.

Two tests in `test_recall_mcp.py` previously monkeypatched
`authenticate_captured` directly; they now monkeypatch
`resolve_request_identity`. Behaviorally identical (both stubs return
the same `AgentContext`); the swap is purely mechanical.

## Test count delta

* Baseline (entering fix-loop): 361 passed, 6 skipped.
* After fix-loop iter 1: 380 passed, 6 skipped.

Net: +19. Removed: 8 dead `_extract_token` tests. Added:
* +13 in `tests/test_hmac_review_fixes.py` (C1, C2, C3, H1, H3, H6,
  H9, C4 docs/proxy guards).
* +4 in `tests/test_hermes_signed_proxy.py` (C4 sidecar integration +
  smoke + security).
* +1 in `tests/test_hmac_auth.py` (H7 zero-candidate dummy work).
* +2 in `tests/test_hmac_operator.py` (H2 race-loss + hash-mismatch).
* +3 in `tests/test_gbrain_doctor.py` (H5 env orphan + JSON-fail +
  DB-fail) minus 1 modified existing.
* +1 modified (test_hmac_constant_time_no_agent_ordering_leak,
  exact-count assertion).

