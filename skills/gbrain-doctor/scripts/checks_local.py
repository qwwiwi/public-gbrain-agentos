"""Local / security / GitHub / self-install checks (G7-G10) for gbrain-doctor.

This module implements the agent-machine-local half of the doctor:

- G7 webhooks: listener healthz, auth modes, service-manager status, token
  file safety, and recent self-delivery error patterns.
- G8 topology / security: MCP URL topology classification, unauthenticated
  endpoint enforcement, raw backend port reachability, TLS validity, and the
  Cloudflare streamable-http SSE caveat.
- G9 GitHub: ``gh`` presence/auth, per-agent private repo existence/status.
- G10 self-install: skill dir, SKILL.md frontmatter, executable scripts,
  symlink visibility, bundled references.

All checks are read-only by default. Only three checks attach an ``auto_fix``
callback, all whitelisted by the core orchestrator:

- ``G9.github_repo_exists_private`` -> ``gh repo create <owner>/<repo> --private``
- ``G10.skill_scripts_executable`` -> ``chmod +x`` the entry script
- ``G10.skill_symlinked``         -> symlink the repo skill into the skills dir

Every message passes through :func:`redact.redact` before constructing a
``CheckResult``. No tokens, no webhook token-file contents, and no raw
subprocess output ever reach output un-redacted. No exception escapes
:func:`run_checks` — unexpected failures become redacted ``fail`` rows.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import redact
from result import CheckResult

if TYPE_CHECKING:  # pragma: no cover — import only for type hints
    from gbrain_doctor import DoctorContext, McpServer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Webhook listener health ports. 8091 = Hermes/Claude Code listener;
# 8089 = jarvis-channel plugin listener.
_WEBHOOK_PORTS: tuple[int, ...] = (8091, 8089)

# Raw gbrain backend MCP service ports. If reachable from the agent machine on
# the resolved *public* IP, the backend is exposed (actionable security issue).
_BACKEND_PORTS: tuple[int, ...] = (8766, 8767, 8768, 8769)

# Cloudflare IPv4 ranges — snapshot 2026-05-28. The authoritative live list is
# https://www.cloudflare.com/ips-v4 and this snapshot WILL drift over time; it
# is only used as a soft signal alongside ``server: cloudflare`` / ``cf-ray``.
_CLOUDFLARE_V4_RANGES: tuple[str, ...] = (
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "173.245.48.0/20",
    "131.0.72.0/22",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "108.162.192.0/18",
    "141.101.64.0/18",
    "162.158.0.0/15",
    "188.114.96.0/20",
    "190.93.240.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
)

# Tailscale CGNAT range (100.64.0.0/10) — plain HTTP is acceptable here.
_TAILSCALE_RANGE = "100.64.0.0/10"

# cf-ray header value shape: <hex>-<IATA airport code>.
_CF_RAY_RE = re.compile(r"^[0-9a-f]+-[A-Z]{3}$")

# Recurring webhook delivery error patterns -> operator remediation hint.
_WEBHOOK_ERROR_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"connection refused|econnrefused", re.I),
     "listener is dead — start the webhook listener service"),
    (re.compile(r"\b404\b|not found", re.I),
     "wrong path — listener route must be /webhook"),
    (re.compile(r"\b401\b|\b403\b|unauthor", re.I),
     "auth mismatch — Bearer/HMAC secret differs between worker and listener"),
    (re.compile(r"timeout|timed out", re.I),
     "inject too slow — listener handler exceeds the delivery timeout"),
    (re.compile(r"\b502\b|\b503\b|bad gateway", re.I),
     "tunnel/proxy broken — check Caddy/Cloudflare/Tailscale path"),
    (re.compile(r"name or service not known|nodename nor servname|dns", re.I),
     "DNS bad — gateway hostname does not resolve"),
)

# Hetzner gbrain backend IP — already public in repo docs, safe to show.
_KNOWN_PUBLIC_BACKEND_IP = "65.109.137.239"


# ---------------------------------------------------------------------------
# Small redacted subprocess helper
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str], timeout: float = 10.0
) -> tuple[int | None, str, str]:
    """Run a subprocess, capturing redacted stdout/stderr.

    Args:
        cmd: Argument vector. ``cmd[0]`` is the binary.
        timeout: Hard timeout in seconds.

    Returns:
        A tuple ``(returncode, stdout, stderr)``. ``returncode`` is ``None``
        when the binary is missing or the call timed out. ``stdout`` /
        ``stderr`` are already passed through :func:`redact.redact`.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return None, "", redact.redact(f"binary not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        return None, "", redact.redact(f"timeout after {timeout}s: {cmd[0]}")
    except OSError as exc:  # noqa: BLE001 — surface as redacted, never crash
        return None, "", redact.redact(f"exec error: {exc}")
    return proc.returncode, redact.redact(proc.stdout), redact.redact(proc.stderr)


def _which(binary: str) -> str | None:
    """Return the resolved path of ``binary`` or ``None`` when absent."""
    from shutil import which

    return which(binary)


# ---------------------------------------------------------------------------
# Topology helpers (G8)
# ---------------------------------------------------------------------------


def _is_ip_in_ranges(ip: str, ranges: tuple[str, ...]) -> bool:
    """Whether ``ip`` falls inside any CIDR in ``ranges`` (best-effort)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in ranges:
        try:
            if addr in ipaddress.ip_network(cidr):
                return True
        except ValueError:  # pragma: no cover — static ranges are valid
            continue
    return False


def _is_loopback_or_local(host: str) -> bool:
    """Whether ``host`` is localhost / loopback (same-host topology)."""
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_tailscale(host_or_ip: str) -> bool:
    """Whether a host/IP literal is in the Tailscale 100.64.0.0/10 range."""
    return _is_ip_in_ranges(host_or_ip, (_TAILSCALE_RANGE,))


def _resolve_host(host: str) -> str | None:
    """Resolve a hostname to a single IPv4/IPv6 address, or ``None``."""
    try:
        ipaddress.ip_address(host)
        return host  # already a literal IP
    except ValueError:
        pass
    try:
        return socket.gethostbyname(host)
    except (socket.gaierror, OSError):
        return None


def _is_raw_public_ip_host(host: str) -> bool:
    """Whether ``host`` is itself a raw, public (non-private) IP literal."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local)


def _fetch_headers(url: str, timeout: float = 8.0) -> dict[str, str]:
    """Best-effort GET of an HTTPS endpoint, returning lowercased headers.

    A non-2xx HTTP status still yields headers (read from the ``HTTPError``).
    Network failures yield an empty dict — callers treat that as inconclusive.
    """
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        try:
            return {k.lower(): v for k, v in exc.headers.items()}
        except Exception:  # noqa: BLE001 — header read best-effort
            return {}
    except (urllib.error.URLError, ssl.SSLError, OSError):
        return {}


def _detect_cloudflare(host: str, headers: dict[str, str]) -> bool:
    """Detect a Cloudflare-fronted endpoint via DNS ranges and/or headers."""
    server = headers.get("server", "").lower()
    cf_ray = headers.get("cf-ray", "")
    if "cloudflare" in server:
        return True
    if cf_ray and _CF_RAY_RE.match(cf_ray.strip()):
        return True
    resolved = _resolve_host(host)
    if resolved and _is_ip_in_ranges(resolved, _CLOUDFLARE_V4_RANGES):
        return True
    return False


def _unauth_status(url: str, timeout: float = 8.0) -> int | None:
    """Probe an MCP endpoint with NO Authorization header.

    Mirrors the streamable-http JSON-RPC shape (POST ``tools/list``) but omits
    the Bearer token. A correctly-secured endpoint returns 401/403.

    Returns:
        The HTTP status code, or ``None`` on a network/URL error (inconclusive).
    """
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, ssl.SSLError, OSError):
        return None


def _unauth_tool_call(url: str, timeout: float = 8.0) -> tuple[int | None, bool]:
    """Probe an MCP endpoint with ``tools/call`` and NO Authorization header.

    This is the *real* auth boundary: gbrain enforces auth at ``tools/call``,
    not ``tools/list``. An unauthenticated ``tools/call`` that returns a usable
    JSON-RPC ``result`` (HTTP 200 + ``result`` present, no ``error``) is a true
    bypass. We invoke a harmless read-only tool name.

    Returns:
        A tuple ``(status, executed)`` where ``status`` is the HTTP status code
        (or ``None`` on a network error) and ``executed`` is ``True`` only when
        the call returned a real JSON-RPC ``result`` without an ``error`` — i.e.
        the tool actually ran unauthenticated.
    """
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "stats", "arguments": {}},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, False
    except (urllib.error.URLError, ssl.SSLError, OSError):
        return None, False

    # 200 alone is not a bypass — many servers 200 the JSON-RPC envelope and put
    # an auth error inside it. A bypass requires a real ``result`` and no error.
    if not (200 <= (status or 0) < 300):
        return status, False
    executed = False
    # streamable-http may answer as SSE (text/event-stream); scan data lines.
    chunks: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunks.append(line[len("data:"):].strip())
    chunks.append(body)
    for chunk in chunks:
        try:
            obj = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if not (isinstance(obj, dict) and "error" not in obj):
            continue
        result = obj.get("result")
        if not isinstance(result, dict):
            continue
        # An MCP tool-error envelope (``isError: true``, e.g. "Unknown tool",
        # "unauthorized", "permission denied") means the tool did NOT actually
        # run — that is not a bypass. Only a clean result counts as executed.
        if result.get("isError") is True:
            continue
        executed = True
        break
    return status, executed


def _port_open(ip: str, port: int, timeout: float = 3.0) -> bool:
    """Whether a TCP connect to ``ip:port`` succeeds from this machine."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((ip, port)) == 0
    except OSError:
        return False


# ---------------------------------------------------------------------------
# G7 — webhooks
# ---------------------------------------------------------------------------


def _healthz_probe(port: int, timeout: float = 4.0) -> dict[str, Any] | None:
    """GET ``http://127.0.0.1:<port>/healthz``; return parsed JSON or ``None``."""
    url = f"http://127.0.0.1:{port}/healthz"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _webhook_token_file_candidates(agent: str | None) -> list[Path]:
    """Resolve candidate Bearer token-file paths from env and conventions."""
    candidates: list[Path] = []
    for env_key in ("WEBHOOK_BEARER_FILE", "HERMES_WEBHOOK_BEARER_FILE"):
        val = os.environ.get(env_key)
        if val:
            candidates.append(Path(val).expanduser())
    if agent:
        candidates.append(
            Path.home() / ".claude-lab" / agent / "secrets" / "webhook-bearer"
        )
        candidates.append(
            Path.home() / ".secrets" / "webhook" / f"{agent}-bearer"
        )
    # De-dup while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for c in candidates:
        key = str(c)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


# Env keys that, when present in a listener service's environment, point at the
# Bearer / HMAC token file on disk.
_TOKEN_FILE_ENV_KEYS: tuple[str, ...] = (
    "WEBHOOK_BEARER_FILE",
    "HERMES_WEBHOOK_BEARER_FILE",
    "WEBHOOK_HMAC_SECRET_FILE",
)


def _parse_env_kv_lines(text: str, keys: tuple[str, ...]) -> dict[str, str]:
    """Extract ``KEY=VALUE`` pairs for ``keys`` from arbitrary KV text.

    Tolerates ``KEY=value``, ``KEY = value``, surrounding quotes, and the
    ``"KEY" = "value";`` shape that ``launchctl print`` emits inside its
    ``environment = { ... }`` block. Only the requested ``keys`` are returned.
    """
    found: dict[str, str] = {}
    for key in keys:
        # Match KEY = VALUE with optional quotes around either side and an
        # optional trailing semicolon (launchctl) — value stops at quote,
        # semicolon, or end-of-line.
        pat = re.compile(
            r'(?m)^[\s"]*' + re.escape(key) + r'"?\s*=\s*"?([^";\r\n]+)'
        )
        m = pat.search(text)
        if m:
            val = m.group(1).strip().strip('"').strip("'")
            if val:
                found[key] = val
    return found


def _launchctl_token_file_paths(unit: str) -> list[Path]:
    """Resolve token-file paths from a loaded launchd unit's environment.

    Runs ``launchctl print gui/<uid>/<unit>`` and scans its ``environment``
    block for ``WEBHOOK_BEARER_FILE`` / ``WEBHOOK_HMAC_SECRET_FILE``. Returns
    an empty list when launchctl is missing, the unit is absent, or no key is
    set. Never raises.
    """
    if _which("launchctl") is None:
        return []
    uid = os.getuid()
    rc, sout, _serr = _run(
        ["launchctl", "print", f"gui/{uid}/{unit}"], timeout=8.0
    )
    if rc != 0 or not sout:
        return []
    kv = _parse_env_kv_lines(sout, _TOKEN_FILE_ENV_KEYS)
    return [Path(v).expanduser() for v in kv.values()]


def _systemctl_token_file_paths(units: list[str]) -> list[Path]:
    """Resolve token-file paths from systemd unit Environment/EnvironmentFiles.

    For each unit runs ``systemctl show <unit> -p Environment -p
    EnvironmentFiles``; reads inline ``Environment=`` and, when needed, the
    referenced ``EnvironmentFiles=`` on disk, scanning both for the token-file
    env keys. Returns an empty list when systemctl is missing or nothing
    resolves. Never raises.
    """
    if _which("systemctl") is None:
        return []
    paths: list[Path] = []
    for unit in units:
        rc, sout, _serr = _run(
            ["systemctl", "show", unit, "-p", "Environment",
             "-p", "EnvironmentFiles"],
            timeout=8.0,
        )
        if rc is None or not sout:
            continue
        # Inline Environment=KEY=val KEY2=val2 (space-separated).
        inline = ""
        env_files: list[str] = []
        for line in sout.splitlines():
            if line.startswith("Environment="):
                inline += " " + line[len("Environment="):]
            elif line.startswith("EnvironmentFiles="):
                # Shape: EnvironmentFiles=/path (ignore_errors)
                raw = line[len("EnvironmentFiles="):].strip()
                fpath = raw.split(" ")[0].strip()
                if fpath:
                    env_files.append(fpath)
        for v in _parse_env_kv_lines(inline, _TOKEN_FILE_ENV_KEYS).values():
            paths.append(Path(v).expanduser())
        # Read referenced EnvironmentFiles for the same keys.
        for ef in env_files:
            try:
                content = Path(ef).expanduser().read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            for v in _parse_env_kv_lines(content, _TOKEN_FILE_ENV_KEYS).values():
                paths.append(Path(v).expanduser())
    return paths


def _listener_service_configured(ctx: "DoctorContext") -> bool:
    """Whether a webhook listener service is registered on this platform.

    macOS: launchd unit ``ai.gbrain.hermes-webhook`` is loaded (rc 0 from
    ``launchctl print``). Linux: any of ``hermes-webhook.service`` /
    ``channel-<agent>.service`` is known to systemd (load state not
    ``not-found``). Never raises; missing tooling -> ``False``.
    """
    platform = sys.platform
    if platform == "darwin":
        if _which("launchctl") is None:
            return False
        uid = os.getuid()
        rc, _sout, _serr = _run(
            ["launchctl", "print", f"gui/{uid}/ai.gbrain.hermes-webhook"],
            timeout=8.0,
        )
        return rc == 0
    if platform.startswith("linux"):
        if _which("systemctl") is None:
            return False
        units = ["hermes-webhook.service"]
        if ctx.agent:
            units.append(f"channel-{ctx.agent}.service")
        for unit in units:
            rc, sout, _serr = _run(
                ["systemctl", "show", unit, "-p", "LoadState"], timeout=8.0
            )
            if rc is None:
                continue
            state = sout.strip()
            # LoadState=loaded means the unit file exists and is known.
            if state and "LoadState=not-found" not in state:
                return True
        return False
    return False


def _service_manager_token_paths(ctx: "DoctorContext") -> list[Path]:
    """Resolve token-file paths advertised by the platform service manager."""
    platform = sys.platform
    if platform == "darwin":
        return _launchctl_token_file_paths("ai.gbrain.hermes-webhook")
    if platform.startswith("linux"):
        units = ["hermes-webhook.service"]
        if ctx.agent:
            units.append(f"channel-{ctx.agent}.service")
        return _systemctl_token_file_paths(units)
    return []


def _check_g7(ctx: "DoctorContext") -> list[CheckResult]:
    """G7 webhook checks (C035-C039)."""
    out: list[CheckResult] = []

    # Probe both known healthz ports; remember which (if any) answered.
    healthz: dict[int, dict[str, Any]] = {}
    for port in _WEBHOOK_PORTS:
        data = _healthz_probe(port)
        if data is not None:
            healthz[port] = data

    # C035 webhook_listener_healthz
    if healthz:
        live_ports = ", ".join(str(p) for p in sorted(healthz))
        out.append(CheckResult(
            name="G7.webhook_listener_healthz",
            status="pass",
            message=redact.redact(f"listener healthz ok on port(s) {live_ports}"),
        ))
    else:
        # No listener answering. This is only a warn: many agents do not run a
        # local push listener (they pull via list_my_pending).
        out.append(CheckResult(
            name="G7.webhook_listener_healthz",
            status="warn",
            message=redact.redact(
                "no webhook listener on 127.0.0.1:"
                f"{{{','.join(str(p) for p in _WEBHOOK_PORTS)}}} "
                "(ok if this agent pulls deliveries instead of receiving pushes)"
            ),
            remediation="Start Hermes/jarvis listener if push delivery is expected.",
        ))

    # C036 webhook_auth_modes_configured (only meaningful if a listener exists).
    if healthz:
        any_mode = False
        missing_field = False
        for data in healthz.values():
            modes = data.get("auth_modes")
            if modes is None:
                missing_field = True
            elif isinstance(modes, list) and modes:
                any_mode = True
        if any_mode:
            out.append(CheckResult(
                name="G7.webhook_auth_modes_configured",
                status="pass",
                message=redact.redact("listener reports Bearer/HMAC auth mode"),
            ))
        elif missing_field:
            out.append(CheckResult(
                name="G7.webhook_auth_modes_configured",
                status="warn",
                message=redact.redact("healthz lacks an auth_modes field"),
                remediation="Upgrade the listener to report auth_modes.",
            ))
        else:
            out.append(CheckResult(
                name="G7.webhook_auth_modes_configured",
                status="warn",
                message=redact.redact("listener reports NO auth mode configured"),
                remediation=(
                    "Set WEBHOOK_BEARER_FILE / WEBHOOK_BEARER / WEBHOOK_HMAC_SECRET; "
                    "unauthenticated webhooks are unsafe off-loopback."
                ),
            ))
    else:
        out.append(CheckResult(
            name="G7.webhook_auth_modes_configured",
            status="skip",
            message=redact.redact("no listener — auth-mode check not applicable"),
        ))

    # C037 webhook_service_loaded
    out.append(_check_webhook_service(ctx))

    # C038 webhook_token_file_safe
    out.append(_check_webhook_token_file(ctx))

    # C039 webhook_delivery_errors
    out.append(_check_webhook_delivery_errors(ctx))

    return out


def _check_webhook_service(ctx: "DoctorContext") -> CheckResult:
    """C037 — platform service manager knows about the listener."""
    name = "G7.webhook_service_loaded"
    platform = sys.platform

    if platform == "darwin":
        uid = os.getuid()
        rc, sout, serr = _run([
            "launchctl", "print", f"gui/{uid}/ai.gbrain.hermes-webhook",
        ])
        if rc is None:
            return CheckResult(
                name=name, status="skip",
                message=redact.redact("launchctl unavailable: " + serr),
            )
        if rc == 0:
            # Look for an active/running state in the print output.
            running = "state = running" in sout
            return CheckResult(
                name=name,
                status="pass" if running else "warn",
                message=redact.redact(
                    "launchd ai.gbrain.hermes-webhook loaded"
                    + ("" if running else " (not currently running)")
                ),
                remediation=None if running else "launchctl kickstart the webhook job.",
            )
        return CheckResult(
            name=name, status="warn",
            message=redact.redact("launchd ai.gbrain.hermes-webhook not installed"),
            remediation="Install the launchd plist if push delivery is expected.",
        )

    if platform.startswith("linux"):
        units = ["hermes-webhook.service"]
        if ctx.agent:
            units.append(f"channel-{ctx.agent}.service")
        for unit in units:
            rc, sout, _serr = _run(["systemctl", "is-active", unit])
            if rc is None:
                return CheckResult(
                    name=name, status="skip",
                    message=redact.redact("systemctl unavailable"),
                )
            state = sout.strip()
            if state == "active":
                return CheckResult(
                    name=name, status="pass",
                    message=redact.redact(f"{unit} active"),
                )
        return CheckResult(
            name=name, status="warn",
            message=redact.redact(
                "no active webhook unit ("
                + ", ".join(units) + ")"
            ),
            remediation="Start the systemd unit if push delivery is expected.",
        )

    return CheckResult(
        name=name, status="skip",
        message=redact.redact(f"unsupported platform {platform} for service check"),
    )


def _check_webhook_token_file(ctx: "DoctorContext") -> CheckResult:
    """C038 — Bearer token file exists, mode 600, non-empty, no trailing newline.

    NEVER prints the token contents.
    """
    name = "G7.webhook_token_file_safe"

    # Discovery order: env/conventional paths first, then paths advertised by
    # the platform service manager (launchd/systemd) for the listener unit.
    candidates = list(_webhook_token_file_candidates(ctx.agent))
    try:
        svc_paths = _service_manager_token_paths(ctx)
    except Exception:  # noqa: BLE001 — service-manager probe must never crash
        svc_paths = []
    seen = {str(c) for c in candidates}
    for p in svc_paths:
        if str(p) not in seen:
            seen.add(str(p))
            candidates.append(p)

    # Is a listener service actually registered? Drives skip-vs-warn below.
    try:
        listener_configured = _listener_service_configured(ctx)
    except Exception:  # noqa: BLE001
        listener_configured = False

    if not candidates:
        if listener_configured:
            return CheckResult(
                name=name, status="warn",
                message=redact.redact(
                    "listener configured but token file path unresolved — "
                    "verify auth is set"
                ),
                remediation=(
                    "Set WEBHOOK_BEARER_FILE / WEBHOOK_HMAC_SECRET_FILE in the "
                    "listener service environment."
                ),
            )
        return CheckResult(
            name=name, status="skip",
            message=redact.redact(
                "no webhook token file configured (WEBHOOK_BEARER_FILE unset)"
            ),
        )

    found: Path | None = None
    for cand in candidates:
        if cand.is_file():
            found = cand
            break
    if found is None:
        if listener_configured:
            return CheckResult(
                name=name, status="warn",
                message=redact.redact(
                    "listener configured but token file path unresolved — "
                    "verify auth is set"
                ),
                remediation=(
                    "Ensure the configured WEBHOOK_BEARER_FILE / "
                    "WEBHOOK_HMAC_SECRET_FILE actually exists (mode 600)."
                ),
            )
        return CheckResult(
            name=name, status="skip",
            message=redact.redact(
                "no webhook token file present at configured/conventional paths"
            ),
        )

    try:
        st = found.stat()
        raw = found.read_bytes()
    except OSError as exc:
        return CheckResult(
            name=name, status="fail",
            message=redact.redact(f"cannot read token file {found.name}: {exc}"),
            remediation="Fix permissions/ownership of the token file.",
        )

    if not raw or not raw.strip():
        return CheckResult(
            name=name, status="fail",
            message=redact.redact(f"token file {found.name} is empty"),
            remediation="Write a valid Bearer token to the file (mode 600).",
        )

    mode = st.st_mode & 0o777
    if mode & 0o077:  # any group/other permission bit set
        return CheckResult(
            name=name, status="fail",
            message=redact.redact(
                f"token file {found.name} mode is {oct(mode)} (world/group readable)"
            ),
            remediation=f"chmod 600 {found}",
        )

    # Trailing newline risk: sha256 over file (with \n) != sha256 over the
    # trimmed token, which is the historical 401 mismatch source.
    if raw.endswith(b"\n") or raw.endswith(b"\r\n"):
        return CheckResult(
            name=name, status="warn",
            message=redact.redact(
                f"token file {found.name} has a trailing newline "
                "(can cause sha256 hash mismatch -> 401)"
            ),
            remediation=f"printf '%s' \"$TOKEN\" > {found}  # write without trailing newline",
        )

    return CheckResult(
        name=name, status="pass",
        message=redact.redact(f"token file {found.name} safe (mode 0600, no trailing newline)"),
    )


def _coerce_delivery_list(payload: Any) -> list[dict[str, Any]]:
    """Extract a list of delivery dicts from a tool_call payload.

    The swarm tool returns a JSON array. Depending on how it surfaces through
    the stateless MCP envelope it may arrive as the list directly, nested under
    ``content``, or under ``structuredContent``/``result``.
    """
    if isinstance(payload, list):
        return [d for d in payload if isinstance(d, dict)]
    if isinstance(payload, dict):
        for key in ("content", "result", "deliveries", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                # ``content`` may itself wrap a single nested list.
                if len(val) == 1 and isinstance(val[0], list):
                    return [d for d in val[0] if isinstance(d, dict)]
                dicts = [d for d in val if isinstance(d, dict)]
                if dicts:
                    return dicts
    return []


# Only deliveries updated within this window count toward the failure ratio.
# A ``failed`` row's cause may already be fixed (e.g. a since-corrected gateway
# route); without a window those stale rows keep the ratio red forever, because
# ``list_recent_deliveries`` returns by ``created_at`` and the denominator only
# grows as new acked rows appear. Windowing on ``updated_at`` makes the metric
# reflect *current* health.
_DELIVERY_WINDOW_HOURS = 6


def _delivery_within_window(row: dict, hours: int = _DELIVERY_WINDOW_HOURS) -> bool:
    """Whether a delivery row was last touched within ``hours``.

    Reads ``updated_at`` (falling back to ``created_at``) as an ISO-8601
    timestamp. A missing or unparseable timestamp returns ``True`` so the row
    is never silently hidden — we only ever *exclude* a row we can prove is old.
    """
    from datetime import datetime, timedelta, timezone

    ts = row.get("updated_at") or row.get("created_at")
    if not isinstance(ts, str) or not ts:
        return True
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt >= cutoff


def _check_webhook_delivery_errors(ctx: "DoctorContext") -> CheckResult:
    """C039 — recent self-addressed deliveries show no recurring webhook errors.

    Only deliveries updated within :data:`_DELIVERY_WINDOW_HOURS` count toward
    the failure ratio, so a since-fixed cause does not keep the check red.
    """
    name = "G7.webhook_delivery_errors"
    swarm = ctx.server("swarm")
    if swarm is None:
        return CheckResult(
            name=name, status="skip",
            message=redact.redact("swarm server not configured"),
        )
    if not ctx.agent:
        return CheckResult(
            name=name, status="skip",
            message=redact.redact("agent identity unknown — cannot filter to_agent"),
        )

    # Late import so the module loads even if mcp_streamable is mid-integration.
    try:
        import mcp_streamable
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name=name, status="skip",
            message=redact.redact(f"mcp_streamable unavailable: {exc}"),
        )

    try:
        payload, _latency = mcp_streamable.tool_call(
            swarm.url, swarm.token, "list_recent_deliveries", {"limit": 50}
        )
    except Exception as exc:  # noqa: BLE001 — McpError or unexpected
        return CheckResult(
            name=name, status="warn",
            message=redact.redact(f"could not list recent deliveries: {exc}"),
            remediation="Verify swarm MCP reachability (see G1).",
        )

    rows = _coerce_delivery_list(payload)
    mine_all = [r for r in rows if r.get("to_agent") == ctx.agent]
    if not mine_all:
        return CheckResult(
            name=name, status="pass",
            message=redact.redact(
                f"no recent deliveries addressed to {ctx.agent} (nothing to fault)"
            ),
        )

    # Window to current health: ignore deliveries whose cause may already be
    # fixed. Stale failures are surfaced as context, never as the verdict.
    window_h = _DELIVERY_WINDOW_HOURS
    mine = [r for r in mine_all if _delivery_within_window(r, window_h)]
    stale_failed = sum(
        1
        for r in mine_all
        if r.get("status") == "failed" and not _delivery_within_window(r, window_h)
    )
    stale_note = (
        f" ({stale_failed} older failed outside {window_h}h window ignored)"
        if stale_failed
        else ""
    )

    if not mine:
        return CheckResult(
            name=name, status="pass",
            message=redact.redact(
                f"no deliveries to {ctx.agent} in last {window_h}h"
                f"{stale_note}"
            ),
        )

    failed = [r for r in mine if r.get("status") == "failed"]
    if not failed:
        return CheckResult(
            name=name, status="pass",
            message=redact.redact(
                f"{len(mine)} deliveries to {ctx.agent} in last {window_h}h, "
                f"none failed{stale_note}"
            ),
        )

    # Map the first recognizable error pattern for a remediation hint.
    hint = "Inspect swarm worker logs; map ConnectionRefused/401/404/Timeout/502/DNS."
    matched: str | None = None
    for row in failed:
        blob = redact.redact(
            " ".join(
                str(row.get(k, ""))
                for k in ("last_error", "status", "payload_json")
            )
        )
        for pattern, remediation in _WEBHOOK_ERROR_HINTS:
            if pattern.search(blob):
                matched = remediation
                break
        if matched:
            break
    if matched:
        hint = matched

    ratio = len(failed) / len(mine)
    status = "fail" if ratio > 0.20 else "warn"
    return CheckResult(
        name=name, status=status,
        message=redact.redact(
            f"{len(failed)}/{len(mine)} deliveries to {ctx.agent} in last "
            f"{window_h}h failed ({ratio:.0%}){stale_note}"
        ),
        remediation=hint,
    )


# ---------------------------------------------------------------------------
# G8 — topology / security
# ---------------------------------------------------------------------------


def _primary_public_server(ctx: "DoctorContext") -> "McpServer | None":
    """Pick a representative non-local server for topology checks."""
    for svc in ("memory", "recall", "swarm", "tasks"):
        srv = ctx.server(svc)
        if srv and srv.url:
            return srv
    return ctx.servers[0] if ctx.servers else None


def _classify_topology(
    url: str,
) -> tuple[str, str]:
    """Classify an MCP URL into a topology label + scheme/host description.

    Returns:
        A tuple ``(label, detail)`` where ``label`` is one of ``"local"``,
        ``"tailscale"``, ``"raw_public_ip"``, ``"domain_tls"``,
        ``"private_unknown"``, or ``"unknown"``.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    scheme = parsed.scheme

    if _is_loopback_or_local(host):
        return "local", f"{scheme}://{host} (loopback)"
    if _is_tailscale(host):
        return "tailscale", f"{scheme}://{host} (Tailscale 100.64/10)"
    if _is_raw_public_ip_host(host):
        return "raw_public_ip", f"{scheme}://{host} (raw public IP)"
    # Hostname (not a literal IP).
    try:
        ipaddress.ip_address(host)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False
    if not is_literal_ip and scheme == "https":
        return "domain_tls", f"https://{host} (TLS domain)"
    if is_literal_ip:
        return "private_unknown", f"{scheme}://{host} (private/LAN IP)"
    return "unknown", f"{scheme}://{host}"


def _check_g8(ctx: "DoctorContext") -> list[CheckResult]:
    """G8 topology/security checks (C040-C044)."""
    out: list[CheckResult] = []
    srv = _primary_public_server(ctx)
    if srv is None:
        for cid in (
            "mcp_url_topology_detected",
            "public_endpoint_auth_enforced",
            "backend_ports_closed",
            "tls_certificate_valid",
            "cloudflare_sse_caveat",
        ):
            out.append(CheckResult(
                name=f"G8.{cid}", status="skip",
                message=redact.redact("no MCP server configured"),
            ))
        return out

    label, detail = _classify_topology(srv.url)
    parsed = urlparse(srv.url)
    host = parsed.hostname or ""
    headers = _fetch_headers(f"https://{host}/") if parsed.scheme == "https" else {}
    is_cloudflare = bool(host) and parsed.scheme == "https" and _detect_cloudflare(host, headers)

    out.append(_c040_topology(label, detail, is_cloudflare))
    out.append(_c041_public_auth(srv, label))
    out.append(_c042_backend_ports(srv, label, is_cloudflare))
    out.append(_c043_tls(srv, label))
    out.append(_c044_cloudflare_caveat(srv, label, is_cloudflare))
    return out


def _c040_topology(label: str, detail: str, is_cloudflare: bool) -> CheckResult:
    """C040 — MCP URL is in a recognized secure topology."""
    name = "G8.mcp_url_topology_detected"
    if is_cloudflare:
        return CheckResult(
            name=name, status="pass",
            message=redact.redact(f"Cloudflare-fronted topology ({detail})"),
        )
    if label == "local":
        return CheckResult(
            name=name, status="pass",
            message=redact.redact(f"local-only topology ({detail})"),
        )
    if label == "tailscale":
        return CheckResult(
            name=name, status="pass",
            message=redact.redact(f"Tailscale topology ({detail})"),
        )
    if label == "domain_tls":
        return CheckResult(
            name=name, status="pass",
            message=redact.redact(f"Caddy/TLS domain topology ({detail})"),
        )
    if label == "raw_public_ip":
        return CheckResult(
            name=name, status="fail",
            message=redact.redact(f"raw public IP MCP URL ({detail})"),
            remediation="Move to Caddy+TLS domain, Tailscale IP, or Cloudflare Tunnel.",
        )
    return CheckResult(
        name=name, status="warn",
        message=redact.redact(f"unrecognized/private topology ({detail})"),
        remediation="Confirm this is a tailnet/LAN path with Bearer enforced.",
    )


def _c041_public_auth(srv: "McpServer", label: str) -> CheckResult:
    """C041 — public MCP endpoint enforces auth at the real boundary.

    gbrain doctrine: the auth boundary is ``tools/call``, not ``tools/list``.
    An unauthenticated ``tools/list`` returning 200 only exposes the tool schema
    and is therefore a WARN, not a hard FAIL. A hard FAIL is reserved for a real
    bypass: an unauthenticated ``tools/call`` that actually executes (returns a
    real JSON-RPC result). See REVIEW L2 / C041 alignment with checks_mcp C007.
    """
    name = "G8.public_endpoint_auth_enforced"
    if label in ("local", "tailscale"):
        return CheckResult(
            name=name, status="skip",
            message=redact.redact(
                f"{label} topology — public no-auth probe not applicable"
            ),
        )

    # 1) Real-bypass probe: unauthenticated tools/call execution.
    call_status, executed = _unauth_tool_call(srv.url)
    if executed:
        return CheckResult(
            name=name, status="fail",
            message=redact.redact(
                "unauthenticated tools/call EXECUTED "
                f"(HTTP {call_status}) — real auth bypass"
            ),
            remediation=(
                "Fix AuthCaptureMiddleware/proxy — tool execution must require a "
                "Bearer token."
            ),
        )

    # 2) tools/call was rejected at the boundary -> auth enforced where it counts.
    if call_status in (401, 403):
        return CheckResult(
            name=name, status="pass",
            message=redact.redact(
                f"unauthenticated tools/call denied (HTTP {call_status})"
            ),
        )

    # 3) tools/call inconclusive (network error). Fall back to the schema probe.
    list_status = _unauth_status(srv.url)
    if call_status is None and list_status is None:
        return CheckResult(
            name=name, status="warn",
            message=redact.redact(
                "inconclusive: unauth probes had a network error"
            ),
            remediation="Re-run from a network path that can reach the endpoint.",
        )
    if list_status in (401, 403):
        return CheckResult(
            name=name, status="pass",
            message=redact.redact(
                f"unauthenticated tools/list denied (HTTP {list_status})"
            ),
        )
    if list_status == 200:
        return CheckResult(
            name=name, status="warn",
            message=redact.redact(
                "unauthenticated tools/list exposes schema only; tool execution "
                "is auth-enforced"
            ),
            remediation=(
                "No unauthenticated tool executed; optionally also gate "
                "tools/list to hide the schema."
            ),
        )

    # 4) Any other status from the schema probe — not a clean denial.
    shown = list_status if list_status is not None else call_status
    return CheckResult(
        name=name, status="warn",
        message=redact.redact(f"unexpected unauth status HTTP {shown}"),
        remediation="Investigate proxy/auth behavior.",
    )


def _c042_backend_ports(
    srv: "McpServer", label: str, is_cloudflare: bool
) -> CheckResult:
    """C042 — raw backend MCP ports not reachable from this machine."""
    name = "G8.backend_ports_closed"
    if label in ("local", "tailscale"):
        return CheckResult(
            name=name, status="skip",
            message=redact.redact(f"{label} topology — backend port scan not applicable"),
        )

    # Determine the IP to scan: explicit env override, raw-IP host, or resolved
    # hostname. For Cloudflare-fronted domains the true origin is hidden.
    backend_ip = os.environ.get("GBRAIN_BACKEND_IP")
    host = urlparse(srv.url).hostname or ""

    if not backend_ip:
        if label == "raw_public_ip":
            backend_ip = host
        elif is_cloudflare:
            return CheckResult(
                name=name, status="warn",
                message=redact.redact(
                    "Cloudflare-fronted: origin IP hidden, cannot prove backend ports closed "
                    "(set GBRAIN_BACKEND_IP to verify origin)"
                ),
                remediation="Provide GBRAIN_BACKEND_IP to scan the true origin host.",
            )
        else:
            resolved = _resolve_host(host)
            backend_ip = resolved

    if not backend_ip:
        return CheckResult(
            name=name, status="warn",
            message=redact.redact("could not resolve a backend IP to scan"),
            remediation="Set GBRAIN_BACKEND_IP to the origin host IP.",
        )

    open_ports = [p for p in _BACKEND_PORTS if _port_open(backend_ip, p)]
    shown_ip = backend_ip  # Hetzner public IP is already in repo docs; ok to show.
    if open_ports:
        return CheckResult(
            name=name, status="fail",
            message=redact.redact(
                f"backend MCP port(s) {open_ports} OPEN on {shown_ip} "
                "(reachable from this machine — actionable exposure)"
            ),
            remediation="Close UFW/security group; bind MCP to localhost or tailnet.",
        )
    return CheckResult(
        name=name, status="pass",
        message=redact.redact(
            f"backend ports {list(_BACKEND_PORTS)} closed/filtered from here on {shown_ip} "
            "(note: 'closed from here' != 'closed from the whole internet')"
        ),
    )


def _c043_tls(srv: "McpServer", label: str) -> CheckResult:
    """C043 — public-domain topology has valid TLS."""
    name = "G8.tls_certificate_valid"
    parsed = urlparse(srv.url)
    if label in ("local", "tailscale") or parsed.scheme != "https":
        return CheckResult(
            name=name, status="skip",
            message=redact.redact(f"{label}/non-HTTPS topology — TLS check not applicable"),
        )
    host = parsed.hostname or ""
    port = parsed.port or 443
    ctx_ssl = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=8.0) as sock:
            with ctx_ssl.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
    except ssl.SSLCertVerificationError as exc:
        return CheckResult(
            name=name, status="fail",
            message=redact.redact(f"TLS certificate verification failed: {exc.verify_message}"),
            remediation="Fix Caddy/ACME/DNS so the cert is valid for this host.",
        )
    except (ssl.SSLError, socket.timeout, OSError) as exc:
        return CheckResult(
            name=name, status="warn",
            message=redact.redact(f"TLS handshake inconclusive: {exc}"),
            remediation="Re-run from a network path that can reach :443.",
        )
    issuer = ""
    if cert:
        issuer_parts = []
        for rdn in cert.get("issuer", ()):  # tuple of tuples
            for k, v in rdn:
                if k in ("organizationName", "commonName"):
                    issuer_parts.append(v)
        issuer = ", ".join(issuer_parts)
    return CheckResult(
        name=name, status="pass",
        message=redact.redact(
            f"valid TLS for {host}" + (f" (issuer: {issuer})" if issuer else "")
        ),
    )


def _c044_cloudflare_caveat(
    srv: "McpServer", label: str, is_cloudflare: bool
) -> CheckResult:
    """C044 — Cloudflare-fronted endpoints called out without mandating CF."""
    name = "G8.cloudflare_sse_caveat"
    if not is_cloudflare:
        return CheckResult(
            name=name, status="skip",
            message=redact.redact("not Cloudflare-fronted — caveat not applicable"),
        )

    # CF detected: confirm streamable-http actually works via a real tools/list.
    try:
        import mcp_streamable
        mcp_streamable.tools_list(srv.url, srv.token)
        works = True
        err = ""
    except Exception as exc:  # noqa: BLE001 — McpError or unexpected
        works = False
        err = redact.redact(str(exc))

    if works:
        return CheckResult(
            name=name, status="warn",
            message=redact.redact(
                "Cloudflare-fronted and streamable-http works now — but proxied DNS "
                "can buffer SSE and break streaming later (see topology-caveats.md)"
            ),
            remediation="Prefer DNS-only/Tunnel; avoid orange-cloud proxied DNS for MCP.",
        )
    return CheckResult(
        name=name, status="fail",
        message=redact.redact(
            f"Cloudflare-fronted and streamable-http MCP call FAILED: {err}"
        ),
        remediation="Cloudflare proxy is likely buffering SSE — switch to DNS-only/Tunnel.",
    )


# ---------------------------------------------------------------------------
# G9 — GitHub
# ---------------------------------------------------------------------------


def _sanitize_repo_segment(value: str) -> str:
    """Sanitize an agent id into a safe GitHub repo-name segment."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned or "agent"


def _resolve_github_target(ctx: "DoctorContext") -> tuple[str | None, str | None]:
    """Resolve ``(owner, repo)`` for the per-agent GitHub repo.

    Owner: ``GBRAIN_GITHUB_OWNER`` env, else the authenticated ``gh api user``
    login (resolved by the caller). Repo: ``GBRAIN_GITHUB_REPO`` env, else
    ``<agent>-agent`` sanitized. The convention is provisional pending prince
    confirmation.
    """
    owner = os.environ.get("GBRAIN_GITHUB_OWNER")
    repo = os.environ.get("GBRAIN_GITHUB_REPO")
    if not repo and ctx.agent:
        repo = f"{_sanitize_repo_segment(ctx.agent)}-agent"
    return owner, repo


def _gh_authed_login() -> str | None:
    """Return the authenticated gh login via ``gh api user``, or ``None``."""
    rc, sout, _serr = _run(["gh", "api", "user", "--jq", ".login"])
    if rc == 0 and sout.strip():
        return sout.strip()
    return None


def _check_g9(ctx: "DoctorContext") -> list[CheckResult]:
    """G9 GitHub checks (C045-C047)."""
    out: list[CheckResult] = []

    if _which("gh") is None:
        out.append(CheckResult(
            name="G9.gh_cli_present_and_authed", status="skip",
            message=redact.redact("gh CLI not installed"),
            remediation="Install GitHub CLI to enable per-agent repo checks.",
        ))
        for cid in ("github_repo_exists_private", "github_repo_status"):
            out.append(CheckResult(
                name=f"G9.{cid}", status="skip",
                message=redact.redact("gh CLI not installed"),
            ))
        return out

    # C045 gh_cli_present_and_authed
    rc, _sout, serr = _run(["gh", "auth", "status"])
    login = _gh_authed_login() if rc == 0 else None
    if rc != 0 or not login:
        out.append(CheckResult(
            name="G9.gh_cli_present_and_authed", status="fail",
            message=redact.redact("gh present but not authenticated: " + (serr or "")),
            remediation="Run `gh auth login` outside the doctor.",
        ))
        for cid in ("github_repo_exists_private", "github_repo_status"):
            out.append(CheckResult(
                name=f"G9.{cid}", status="skip",
                message=redact.redact("gh not authenticated"),
            ))
        return out

    out.append(CheckResult(
        name="G9.gh_cli_present_and_authed", status="pass",
        message=redact.redact(f"gh authenticated as {login}"),
    ))

    # Resolve target owner/repo (owner falls back to the authed login).
    owner, repo = _resolve_github_target(ctx)
    owner = owner or login
    if not repo:
        out.append(CheckResult(
            name="G9.github_repo_exists_private", status="skip",
            message=redact.redact(
                "no agent id / GBRAIN_GITHUB_REPO — cannot derive repo name"
            ),
            remediation="Pass --agent or set GBRAIN_GITHUB_REPO.",
        ))
        out.append(CheckResult(
            name="G9.github_repo_status", status="skip",
            message=redact.redact("repo target unresolved"),
        ))
        return out

    full = f"{owner}/{repo}"
    rc, sout, _serr = _run([
        "gh", "repo", "view", full, "--json",
        "name,visibility,url,defaultBranchRef,pushedAt",
    ])

    if rc != 0:
        # Repo missing (or no access). Offer the confirmed create autofix.
        def _create_repo(target: str = full) -> bool:
            crc, _o, _e = _run(["gh", "repo", "create", target, "--private"])
            return crc == 0

        out.append(CheckResult(
            name="G9.github_repo_exists_private", status="fail",
            message=redact.redact(
                f"repo {full} not found/accessible "
                "(naming convention <agent>-agent is provisional, pending prince OK)"
            ),
            remediation=f"gh repo create {full} --private",
            auto_fix=_create_repo,
        ))
        out.append(CheckResult(
            name="G9.github_repo_status", status="skip",
            message=redact.redact("repo absent — status not available"),
        ))
        return out

    try:
        meta = json.loads(sout)
    except json.JSONDecodeError as exc:
        out.append(CheckResult(
            name="G9.github_repo_exists_private", status="warn",
            message=redact.redact(f"could not parse gh repo view output: {exc}"),
        ))
        out.append(CheckResult(
            name="G9.github_repo_status", status="skip",
            message=redact.redact("repo metadata unparseable"),
        ))
        return out

    visibility = str(meta.get("visibility", "")).lower()
    if visibility == "private":
        out.append(CheckResult(
            name="G9.github_repo_exists_private", status="pass",
            message=redact.redact(f"repo {full} exists and is private"),
        ))
    else:
        out.append(CheckResult(
            name="G9.github_repo_exists_private", status="fail",
            message=redact.redact(
                f"repo {full} exists but visibility is '{visibility}' (expected private)"
            ),
            remediation=f"gh repo edit {full} --visibility private",
        ))

    # C047 status
    url = meta.get("url") or ""
    default_branch = (meta.get("defaultBranchRef") or {})
    branch_name = default_branch.get("name") if isinstance(default_branch, dict) else None
    pushed_at = meta.get("pushedAt")
    if url and branch_name and pushed_at:
        out.append(CheckResult(
            name="G9.github_repo_status", status="pass",
            message=redact.redact(
                f"{full}: branch={branch_name} last_push={pushed_at} {url}"
            ),
        ))
    else:
        out.append(CheckResult(
            name="G9.github_repo_status", status="warn",
            message=redact.redact(
                f"{full}: missing default branch or never pushed (branch={branch_name})"
            ),
            remediation="Push an initial private branch and set the default branch.",
        ))
    return out


# ---------------------------------------------------------------------------
# G10 — skill self-install
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse a minimal ``--- ... ---`` YAML frontmatter block (key: value).

    Only top-level ``key: value`` scalar pairs are extracted (sufficient for
    ``name`` and ``description``). Values may be quoted.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        fields[key] = val
    return fields


def _check_g10(ctx: "DoctorContext") -> list[CheckResult]:
    """G10 skill self-install checks (C048-C052)."""
    out: list[CheckResult] = []
    skill_root = ctx.skill_root

    # C048 skill_dir_present
    if not skill_root.is_dir():
        out.append(CheckResult(
            name="G10.skill_dir_present", status="fail",
            message=redact.redact(f"skill dir missing: {skill_root}"),
            remediation="Install the gbrain-doctor skill directory in the repo.",
        ))
        # Without the dir, downstream checks are moot.
        for cid in (
            "skill_frontmatter_valid", "skill_scripts_executable",
            "skill_symlinked", "skill_references_present",
        ):
            out.append(CheckResult(
                name=f"G10.{cid}", status="skip",
                message=redact.redact("skill dir absent"),
            ))
        return out
    out.append(CheckResult(
        name="G10.skill_dir_present", status="pass",
        message=redact.redact(f"skill dir present at {skill_root}"),
    ))

    # C049 skill_frontmatter_valid
    out.append(_c049_frontmatter(skill_root))

    # C050 skill_scripts_executable
    out.append(_c050_scripts_executable(skill_root))

    # C051 skill_symlinked
    out.append(_c051_symlinked(ctx, skill_root))

    # C052 skill_references_present
    out.append(_c052_references(skill_root))

    return out


def _c049_frontmatter(skill_root: Path) -> CheckResult:
    """C049 — SKILL.md frontmatter has name==gbrain-doctor and rich description."""
    name = "G10.skill_frontmatter_valid"
    skill_md = skill_root / "SKILL.md"
    if not skill_md.is_file():
        return CheckResult(
            name=name, status="fail",
            message=redact.redact("SKILL.md missing"),
            remediation="Create SKILL.md with valid frontmatter.",
        )
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            name=name, status="fail",
            message=redact.redact(f"cannot read SKILL.md: {exc}"),
        )
    fm = _parse_frontmatter(text)
    fm_name = fm.get("name", "")
    desc = fm.get("description", "")
    if fm_name != "gbrain-doctor":
        return CheckResult(
            name=name, status="fail",
            message=redact.redact(
                f"frontmatter name is {fm_name!r}, expected 'gbrain-doctor'"
            ),
            remediation="Set `name: gbrain-doctor` in SKILL.md frontmatter.",
        )
    # Trigger-rich = non-empty and reasonably descriptive.
    if not desc or len(desc) < 40:
        return CheckResult(
            name=name, status="fail",
            message=redact.redact("frontmatter description missing or too thin"),
            remediation="Add a trigger-rich description with invocation keywords.",
        )
    return CheckResult(
        name=name, status="pass",
        message=redact.redact("SKILL.md frontmatter valid (name + trigger-rich description)"),
    )


def _c050_scripts_executable(skill_root: Path) -> CheckResult:
    """C050 — the entry CLI script is executable (chmod +x autofix)."""
    name = "G10.skill_scripts_executable"
    entry = skill_root / "scripts" / "gbrain_doctor.py"
    if not entry.is_file():
        return CheckResult(
            name=name, status="fail",
            message=redact.redact("entry script scripts/gbrain_doctor.py missing"),
            remediation="Restore the entry script.",
        )
    if os.access(entry, os.X_OK):
        return CheckResult(
            name=name, status="pass",
            message=redact.redact("entry script is executable"),
        )

    def _chmod(path: Path = entry) -> bool:
        try:
            mode = path.stat().st_mode
            path.chmod(mode | 0o111)
            return os.access(path, os.X_OK)
        except OSError:
            return False

    return CheckResult(
        name=name, status="fail",
        message=redact.redact("entry script scripts/gbrain_doctor.py is not executable"),
        remediation=f"chmod +x {entry}",
        auto_fix=_chmod,
    )


def _c051_symlinked(ctx: "DoctorContext", skill_root: Path) -> CheckResult:
    """C051 — skill is visible to Claude Code globally or per-agent (symlink autofix)."""
    name = "G10.skill_symlinked"
    targets: list[Path] = [Path.home() / ".claude" / "skills" / "gbrain-doctor"]
    if ctx.agent:
        targets.append(
            Path.home() / ".claude-lab" / ctx.agent / ".claude" / "skills" / "gbrain-doctor"
        )

    resolved_skill = skill_root.resolve()
    for tgt in targets:
        if tgt.is_symlink():
            try:
                if tgt.resolve() == resolved_skill:
                    return CheckResult(
                        name=name, status="pass",
                        message=redact.redact(f"skill symlinked at {tgt}"),
                    )
            except OSError:
                pass
            # Symlink exists but points elsewhere -> warn (possibly stale).
            return CheckResult(
                name=name, status="warn",
                message=redact.redact(
                    f"symlink {tgt} does not point at this skill checkout"
                ),
                remediation=f"Re-point {tgt} -> {resolved_skill}",
            )
        if tgt.exists():
            # A real dir/copy (not a symlink) — may be a stale copy.
            return CheckResult(
                name=name, status="warn",
                message=redact.redact(
                    f"{tgt} exists as a copy, not a symlink (may be stale)"
                ),
                remediation=f"Replace with a symlink to {resolved_skill}",
            )

    # No symlink and no copy at any target -> fail with symlink autofix.
    primary = targets[-1]  # prefer per-agent dir when --agent given

    def _symlink(target: Path = primary, src: Path = resolved_skill) -> bool:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                return False  # never overwrite an existing entry
            target.symlink_to(src, target_is_directory=True)
            return target.is_symlink()
        except OSError:
            return False

    return CheckResult(
        name=name, status="fail",
        message=redact.redact(
            "skill not symlinked into ~/.claude/skills or the agent skills dir"
        ),
        remediation=f"ln -s {resolved_skill} {primary}",
        auto_fix=_symlink,
    )


def _c052_references(skill_root: Path) -> CheckResult:
    """C052 — bundled references required by the doctor exist and parse."""
    name = "G10.skill_references_present"
    refs_dir = skill_root / "references"
    required_json = ["expected-hooks.json", "claude-hook-events.json"]
    missing: list[str] = []
    malformed: list[str] = []
    for fname in required_json:
        path = refs_dir / fname
        if not path.is_file():
            missing.append(fname)
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            malformed.append(fname)
    if missing or malformed:
        parts = []
        if missing:
            parts.append("missing: " + ", ".join(missing))
        if malformed:
            parts.append("malformed: " + ", ".join(malformed))
        return CheckResult(
            name=name, status="fail",
            message=redact.redact("reference issue — " + "; ".join(parts)),
            remediation="Restore references/ from the repo.",
        )
    return CheckResult(
        name=name, status="pass",
        message=redact.redact("required references present and parseable"),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_checks(ctx: "DoctorContext") -> list[CheckResult]:
    """Run all G7-G10 local/security/GitHub/self-install checks.

    Respects ``ctx.want("G7".."G10")`` so a ``--group`` filter short-circuits
    whole groups. No exception escapes this function — any unexpected failure
    is converted into a redacted ``fail`` row so the CLI never crashes.

    Args:
        ctx: The shared :class:`DoctorContext`.

    Returns:
        An ordered list of :class:`CheckResult` for the requested groups.
    """
    results: list[CheckResult] = []
    groups: list[tuple[str, Any]] = [
        ("G7", _check_g7),
        ("G8", _check_g8),
        ("G9", _check_g9),
        ("G10", _check_g10),
    ]
    for group_id, func in groups:
        if not ctx.want(group_id):
            continue
        try:
            results.extend(func(ctx))
        except Exception as exc:  # noqa: BLE001 — isolate unexpected failures
            results.append(CheckResult(
                name=f"{group_id}.unexpected_error",
                status="fail",
                message=redact.redact(f"{group_id} checks raised: {exc}"),
                remediation="Inspect checks_local.py; this is a doctor bug.",
            ))
    return results
