# Hermes integration — HMAC auth for gbrain MCP

`public-gbrain-agentos` accepts **two** authentication modes on every MCP
endpoint (`memory_mcp`, `recall_mcp`, `swarm_mcp`):

* **Bearer** — existing static-token mode used by all stock agents.
* **HMAC** — Hermes-compatible per-request signature
  (`X-Hermes-Signature: sha256=<hex>` + `X-Hermes-Timestamp: <unix>`).
  The canonical signing string is `"<timestamp>.<body>"` (Hermes/Stripe
  scheme). HMAC is opt-in per agent.

> **Reality check, read before going further.** Hermes upstream
> (`tools/mcp_tool.py:1340-1480`) supports two MCP auth modes natively:
> static `headers:` passthrough (great for Bearer) and `oauth`. There is
> **no built-in per-request HMAC signer** because the signature depends
> on the body, and Hermes does not own the body until just before it
> POSTs. Sample configs in early drafts of this doc showed a
> `auth: { type: hmac, ... }` block that Hermes does **not** parse —
> Hermes would silently treat the section as unknown and send the
> request unsigned. The two viable integration paths are:
>
> 1. **Bearer** — Hermes sends a static `Authorization: Bearer ...`
>    header via its `headers:` block. Simple, works today, no sidecar
>    required.
> 2. **HMAC via a local sidecar proxy** (this repo ships one as
>    `scripts/hermes_signed_proxy.py`). Hermes points at the proxy
>    with no auth; the proxy holds the HMAC secret and signs every
>    forwarded body. This is the recommended HMAC pattern.

This document covers both paths.

---

## 1. Architecture

```
+-----------------+   HTTPS    +---------------------+    UNIX    +-----------+
|  Hermes client  | ---------> |  Caddy (TLS, :443)  | ---------> |  MCP svc  |
|  (mcp_servers:) |  per-req   |  reverse proxy      |  127.x     |  :8767    |
|                 |  signature |                     | passthr.   |  :8768    |
+-----------------+            +---------------------+            |  :8766    |
                                                                  +-----+-----+
                                                                        |
                                                                        v
                                                                +---------------+
                                                                |   Postgres    |
                                                                | agent_tokens  |
                                                                +---------------+
                                     hmac_secret_sha256 (only the digest lives here)
```

* The **raw HMAC secret never enters Postgres**. Only
  `sha256(raw_secret_string)` is stored in `agent_tokens.hmac_secret_sha256`.
* The raw secret lives only in operator-controlled env on the MCP host
  (`GBRAIN_HMAC_SECRETS_JSON='{"<agent>":"<raw>"}'`) and inside the Hermes
  client or sidecar proxy (signing key).
* On every inbound request, the ASGI middleware reads the exact body bytes,
  verifies the `sha256=<hex>` signature against every candidate row in
  constant time, and only then routes to the FastMCP tool.

### Auth priority (when both headers are present)

Bearer wins. If a request carries both `Authorization: Bearer ...` and
the Hermes HMAC headers, the server authenticates as Bearer and ignores
the HMAC headers. This protects every existing client from a stale or
malformed HMAC header breaking a valid Bearer call.

### Kill-switch

`GBRAIN_HMAC_AUTH_ENABLED=0` (env on the MCP host) **rejects** HMAC
requests with `PermissionError("HMAC auth disabled")` while leaving
Bearer working unchanged. Use this for instant rollback if HMAC starts
misfiring in production; flip Hermes back to Bearer in parallel.

---

## 2. Issue an HMAC secret

The agent row must already exist in `agent_tokens` (created by
`scripts/issue-agent-token.py`). HMAC is an additive column on that row.

```bash
# First issue — refuses to clobber an existing secret.
python scripts/issue-hmac-secret.py --agent tyrande

# Rotate — overwrites the previous secret.
python scripts/issue-hmac-secret.py --agent tyrande --rotate
```

The raw secret is printed **once** on stdout. Capture it immediately:

```bash
SECRET=$(python scripts/issue-hmac-secret.py --agent tyrande)
echo "$SECRET" >> /etc/gbrain/hmac-secrets.txt  # operator-controlled, mode 0600
```

Exit codes:

| Code | Meaning                                                |
|------|--------------------------------------------------------|
| `0`  | success — raw secret on stdout                         |
| `1`  | conflict — secret already present or concurrent issuer |
| `2`  | agent row not found (run `issue-agent-token.py` first) |
| `3`  | DB / I/O error during commit                           |

The issuer uses a single conditional `UPDATE ... RETURNING` so two
concurrent runs cannot both print a "successful" secret pointing at
different stored hashes.

---

## 3. Mount the secret on the MCP host

Add to `/etc/gbrain/secrets.env` (read by the systemd units, mode `0600`):

```
GBRAIN_HMAC_SECRETS_JSON={"tyrande":"<raw_secret_from_step_2>"}
HMAC_TIMESTAMP_TOLERANCE_SECONDS=300
GBRAIN_HMAC_AUTH_ENABLED=1
```

Then restart the three MCP units so they pick up the new env:

```bash
sudo systemctl restart gbrain-memory-mcp gbrain-recall-mcp gbrain-swarm-mcp
```

Verify with the doctor:

```bash
python scripts/gbrain_doctor.py
# expect: hmac_secret_health [PASS] 1 HMAC agent(s) healthy: ['tyrande']
```

---

## 4. Configure Hermes — Bearer path (recommended for simplicity)

Hermes can carry a static Bearer token via the `headers:` passthrough.
Issue a Bearer for the agent with `scripts/issue-agent-token.py` and
mount it in the Hermes env, then:

```yaml
mcp_servers:
  gbrain_memory:
    url: https://mcp.example.com/memory/mcp
    headers:
      Authorization: "Bearer ${TYRANDE_BEARER}"

  gbrain_recall:
    url: https://mcp.example.com/recall/mcp
    headers:
      Authorization: "Bearer ${TYRANDE_BEARER}"

  gbrain_swarm:
    url: https://mcp.example.com/swarm/mcp
    headers:
      Authorization: "Bearer ${TYRANDE_BEARER}"
```

This is the **default-recommended** Hermes integration. It does not
require a sidecar and is functionally identical from the gbrain side
to any other Bearer agent.

---

## 5. Configure Hermes — HMAC via local sidecar proxy

If HMAC is mandatory (e.g. operator policy forbids long-lived bearer
tokens), run the bundled sidecar proxy. The proxy receives Hermes' raw
JSON-RPC body, signs it with the configured secret, and forwards the
byte-identical body to the gbrain MCP.

Start the proxy (one per MCP target, or one with separate path-based
fan-out):

```bash
export GBRAIN_PROXY_HMAC_SECRET='<raw secret from step 2>'
python scripts/hermes_signed_proxy.py \
    --target https://mcp.example.com/memory/mcp \
    --secret-env GBRAIN_PROXY_HMAC_SECRET \
    --host 127.0.0.1 \
    --port 8767
```

Point Hermes at the proxy — **no auth block needed**:

```yaml
mcp_servers:
  gbrain_memory:
    url: http://127.0.0.1:8767/
    # No auth here — the proxy signs every request.
```

Run a second instance on a different port for each MCP service
(recall :8768, swarm :8766) or use `systemd` templates.

Health check:

```bash
curl -sS http://127.0.0.1:8767/healthz
# {"status":"ok","target":"https://mcp.example.com/memory/mcp"}
```

The proxy:

* Reads the secret from `GBRAIN_PROXY_HMAC_SECRET` (configurable via
  `--secret-env`) and **never logs it**.
* Refuses to start if the secret env var is missing or empty.
* Never logs request bodies (only sizes and status codes).
* Listens on `127.0.0.1` by default — keep it localhost-only unless you
  fully understand the implications.

Source: `scripts/hermes_signed_proxy.py`. Tests:
`tests/test_hermes_signed_proxy.py` (asserts signature parity with the
gbrain verifier).

---

## 6. Manual signing — Python SDK / curl recipes

Use these when integrating a programmatic client that signs per request
(custom Python tooling, integration tests, ad-hoc shell scripts). The
canonical signing string is `"<timestamp>.<body>"`.

### Python

```python
import hmac, hashlib, json, time, urllib.request

SECRET = b"<raw_secret>"
body = json.dumps({
    "jsonrpc": "2.0", "id": 1,
    "method": "tools/list", "params": {},
}).encode("utf-8")
ts = str(int(time.time()))
# Canonical Hermes/Stripe scheme: HMAC over "<ts>.<body>" bytes.
message = ts.encode("ascii") + b"." + body
sig = "sha256=" + hmac.new(SECRET, message, hashlib.sha256).hexdigest()

req = urllib.request.Request(
    "https://mcp.example.com/memory/mcp",
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-Hermes-Signature": sig,
        "X-Hermes-Timestamp": ts,
    },
    method="POST",
)
print(urllib.request.urlopen(req).read())
```

### `curl` + `openssl`

```bash
SECRET='<raw_secret>'
BODY='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
TS=$(date +%s)
# Sign "<TS>.<BODY>" — timestamp prefix is mandatory.
SIG="sha256=$(printf '%s.%s' "$TS" "$BODY" \
    | openssl dgst -sha256 -hmac "$SECRET" -hex \
    | awk '{print $NF}')"

curl -sS https://mcp.example.com/memory/mcp \
    -H "Content-Type: application/json" \
    -H "X-Hermes-Signature: $SIG" \
    -H "X-Hermes-Timestamp: $TS" \
    --data-raw "$BODY"
```

Notes:

* Sign the **exact body bytes** you put on the wire. Re-serializing JSON
  on the server will reorder keys and break the signature.
* `X-Hermes-Timestamp` must be within `HMAC_TIMESTAMP_TOLERANCE_SECONDS`
  of the server clock (default 300s).

---

## 7. Outbound HMAC (swarm worker)

The swarm worker can sign outbound webhooks with the same scheme. Configure
per-agent gateway auth via `AGENT_GATEWAY_AUTH` (JSON map):

```
AGENT_GATEWAYS={"tyrande":"http://gateway.example/agent","claude":"http://127.0.0.1:8089"}
AGENT_GATEWAY_AUTH={"tyrande":"hmac:env:TYRANDE_WEBHOOK_HMAC","claude":"bearer:env:GATEWAY_WEBHOOK_TOKEN"}
```

The first segment selects the mode (`hmac` / `bearer` / `none`); the
`env:NAME` indirection tells the worker which env var holds the raw value
so the secret never appears in the parsed JSON map.

Disable outbound HMAC globally with `GBRAIN_HMAC_OUTBOUND_ENABLED=0`.

---

## 8. Rotation

Rotation is staged so the running Hermes client keeps working until the
new secret is in place.

1. **Issue a new secret** on the MCP host:

   ```bash
   python scripts/issue-hmac-secret.py --agent tyrande --rotate
   ```

   The old `hmac_secret_sha256` is overwritten in place; the previous
   secret immediately stops working.

2. **Update the Hermes client config or the sidecar proxy env** with the
   new raw secret and reload Hermes (or restart the proxy with the new
   `GBRAIN_PROXY_HMAC_SECRET`).

3. **Update `GBRAIN_HMAC_SECRETS_JSON`** on the MCP host with the new
   raw value, then restart the three MCP units. The doctor will move
   from `warn` (env missing) → `pass` once both sides are in sync.

4. **Verify**:

   ```bash
   python scripts/gbrain_doctor.py | grep hmac_secret_health
   ```

If rotation must happen with **zero downtime**, fall back to Bearer
for the agent during the swap window, then re-enable HMAC after
verification.

---

## 9. Security notes

### Replay window

`HMAC_TIMESTAMP_TOLERANCE_SECONDS` (default 300s) controls how stale a
timestamp may be. The server rejects timestamps outside `abs(now - ts)
> tolerance`.

**What this protects against:** network propagation delay and clock skew
between the client and the MCP host. The signature itself is bound to
the timestamp (canonical signing string is `"<ts>.<body>"`), so an
attacker who captured a signed request cannot replay it under a different
timestamp — the signature would no longer verify.

**What this does NOT protect against:** in-window replay. An attacker
who captured a valid signed request can re-POST it byte-identically
within the tolerance window and the server will accept it. There is no
server-side nonce cache by design (operationally simpler, multi-instance
safe).

**Operator recipe for tighter windows on write-heavy tools:** set
`HMAC_TIMESTAMP_TOLERANCE_SECONDS=60` (or lower) on the MCP host and
restart the units. The doctor will not warn — confirm by issuing a
deliberately stale request and observing `401`.

### Kill-switch verification

After flipping `GBRAIN_HMAC_AUTH_ENABLED=0` and restarting the units,
confirm by:

1. Sending a signed HMAC request (e.g. the curl recipe in §6). Expect
   `401` with reason `HMAC auth disabled` in the server log.
2. Sending a Bearer request to the same endpoint. Expect `200`.
3. Tailing `audit_log` (Postgres) for the period: there should be no
   new rows from the HMAC agent, and the server logs should carry
   `HMAC authentication attempted with kill-switch disabled` warnings.

```sql
-- Recent failed HMAC attempts since the kill-switch flipped:
SELECT created_at, error
  FROM audit_log
 WHERE error ILIKE '%hmac auth disabled%'
 ORDER BY created_at DESC
 LIMIT 20;
```

(Failed-auth audit rows appear when the calling tool wraps the auth call
in its error path. If your tools don't audit auth failures yet, rely on
the server log.)

---

## 10. Troubleshooting

| Symptom                                       | Likely cause                                                                                                                                  |
|-----------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------|
| `401` on every signed request                 | Timestamp outside tolerance — check clock skew (`ntpdate`, `chronyc tracking`). Raise `HMAC_TIMESTAMP_TOLERANCE_SECONDS` only as last resort. |
| `401` only on some requests                   | Body re-serialization by an HTTP middleware between Hermes and Caddy. Sign and post identical bytes; do not let proxies rewrite the body.    |
| `401` immediately after rotation              | Env not reloaded — restart `gbrain-{memory,recall,swarm}-mcp`.                                                                                |
| Doctor reports `hmac_secret_health [FAIL]`    | DB and env disagree on the secret hash — re-run `issue-hmac-secret.py --rotate` and update env, or restore the previous env value.           |
| Doctor reports `[WARN] N agent(s) missing`    | Agent has HMAC in DB but no entry in `GBRAIN_HMAC_SECRETS_JSON`. Add the entry or revoke the HMAC column.                                     |
| Doctor reports `[WARN] N agent(s) ... no DB row` | Env carries an HMAC agent name that does not exist in `agent_tokens` — typo in the env JSON, or you forgot `issue-hmac-secret.py`.        |
| Doctor reports `[FAIL] parse error`           | `GBRAIN_HMAC_SECRETS_JSON` is malformed — fix the JSON and restart the units.                                                                 |
| `unknown agent` in logs                       | The signature did not match any active `hmac_secret_sha256`. Verify the raw secret matches the one printed by `issue-hmac-secret.py`.        |
| Bearer client suddenly breaks                 | Unlikely — Bearer wins over HMAC. Check `agent_tokens.revoked_at` and rerun `scripts/issue-agent-token.py` if the token was rotated.         |
| HMAC silently rejected with `HMAC auth disabled` | `GBRAIN_HMAC_AUTH_ENABLED=0` is set on the MCP host. Toggle back to `1` and restart the units when you're ready to re-enable HMAC.       |
| Sidecar proxy returns `502`                   | Upstream MCP unreachable from the proxy host. Check the `--target` URL, DNS, firewall, and the upstream service health.                       |

The doctor never prints raw secrets — its output is safe to paste into a
ticket.

---

## 11. Cross-references

* Env vars: `.env.example` — `HMAC_TIMESTAMP_TOLERANCE_SECONDS`,
  `GBRAIN_HMAC_AUTH_ENABLED`, `GBRAIN_HMAC_SECRETS_JSON`,
  `GBRAIN_HMAC_OUTBOUND_ENABLED`, `AGENT_GATEWAY_AUTH`,
  `GBRAIN_PROXY_HMAC_SECRET`.
* Doctor check: `hmac_secret_health` (11th check, between
  `bearer_mapping` and `embedding_queue_depth`).
* Migration: `migrations/004_hmac_secrets.sql` — nullable additive columns,
  idempotent, validates column types on re-apply.
* Server middleware: `services/shared/asgi_auth.py::HermesAwareAuthMiddleware`.
* Shared resolver: `services/shared/auth.py::resolve_request_identity`.
* Issuer script: `scripts/issue-hmac-secret.py`.
* Sidecar proxy: `scripts/hermes_signed_proxy.py`.

This change is **public-distro-only**. The production Hermes deployment
(Tyranda) is not touched by this repo.
