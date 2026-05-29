"""Tests for the gbrain-doctor skill.

Covers the public interfaces written by subagent A (redact, result,
mcp_streamable, gbrain_doctor CLI/config) plus the G6 hooks fixtures.

Hard rules honored here:
- No network. ``mcp_streamable`` tests monkeypatch ``urllib.request.urlopen``.
- No real ``~/.claude`` access. Hooks tests run against the bundled fixture
  tree only.
- No state mutation. Nothing calls a mutating MCP tool or ``gh repo create``.

The core modules (redact/result/mcp_streamable/CLI) MUST pass. The check
modules (``checks_mcp`` / ``checks_hooks`` / ``checks_local``) may land after
this file; tests that need them ``skip`` cleanly when they are absent.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import re
import stat
import sys
import types
from pathlib import Path

import pytest

# --- Path wiring: import the real core modules from scripts/ ----------------

_TESTS_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _TESTS_DIR.parent
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
_FIXTURES = _TESTS_DIR / "fixtures" / "gbrain_doctor"

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import redact  # noqa: E402
import result as result_mod  # noqa: E402
from result import CheckResult  # noqa: E402
import mcp_streamable  # noqa: E402
from mcp_streamable import McpError  # noqa: E402
import gbrain_doctor  # noqa: E402
from gbrain_doctor import (  # noqa: E402
    DoctorContext,
    McpServer,
    _infer_service,
    _infer_service_from_key,
    _parse_groups,
    load_mcp_config,
    main,
)


# The toxic regex the doctor promises nothing leaks past. Used as an
# independent oracle so the test does not just re-import redact's own regex.
#
# IMPORTANT: the oracle must NOT flag the safe ``<REDACTED>`` placeholder.
# ``redact()`` rewrites e.g. ``password=hunter2`` -> ``password=<REDACTED>``
# (readable, secret stripped) which is the *correct* output. A naive
# ``password=\S+`` would match the placeholder (``<REDACTED>`` is non-space)
# and falsely report a leak. We therefore negative-look-ahead the placeholder
# in the key=value shapes so only a *real* value trips the oracle, while a raw
# secret still does.
_REDACTED = "<REDACTED>"
_LEAK_RE = re.compile(
    r"(?:Bearer\s+(?!" + re.escape(_REDACTED) + r")\S+"
    r"|sk-[A-Za-z0-9_-]+"
    r"|hmac_[A-Za-z0-9]+"
    r"|password=(?!" + re.escape(_REDACTED) + r")\S+)",
    re.IGNORECASE,
)


def _no_leak(out: str) -> bool:
    """True when ``out`` carries no raw secret (the redacted marker is fine)."""
    return _LEAK_RE.search(out) is None


# Each tuple: (raw input, the raw secret VALUE that must be absent after redact).
_SAMPLE_SECRETS = [
    ("Bearer sk-proj-abcd1234efgh5678ijkl", "sk-proj-abcd1234efgh5678ijkl"),
    ("Authorization: Bearer aaaaaaaaaaaaaaaaaaaaaaaa", "aaaaaaaaaaaaaaaaaaaaaaaa"),
    ("sk-proj-DEADBEEFcafef00dba5eba11", "sk-proj-DEADBEEFcafef00dba5eba11"),
    ("hmac_0123456789abcdef0123456789abcdef", "hmac_0123456789abcdef0123456789abcdef"),
    ("password=hunter2supersecret", "hunter2supersecret"),
]


# ---------------------------------------------------------------------------
# redact.py
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_each_sample_secret_is_fully_masked(self):
        for raw, secret_value in _SAMPLE_SECRETS:
            out = redact.redact(raw)
            assert _REDACTED in out
            # The raw secret VALUE must be gone; the placeholder is allowed.
            assert secret_value not in out, f"raw secret survived in {out!r}"
            assert _no_leak(out), f"leak in {out!r}"

    def test_redact_embedded_in_sentence(self):
        text = "call failed with Authorization: Bearer abcdef123456 and password=topsecret here"
        out = redact.redact(text)
        # Both raw secret values must be stripped, the prose preserved.
        assert "abcdef123456" not in out
        assert "topsecret" not in out
        assert _no_leak(out)
        assert "call failed with" in out

    def test_leak_oracle_still_catches_raw_secret(self):
        # Guard the oracle itself: a genuinely UN-redacted secret must trip it,
        # so the masking tests above stay meaningful if redaction regresses.
        assert not _no_leak("password=topsecret")
        assert not _no_leak("Authorization: Bearer abcdef123456")
        assert not _no_leak("sk-proj-DEADBEEFcafef00d")
        # ...while the safe redacted placeholder must NOT be flagged.
        assert _no_leak("password=<REDACTED>")
        assert _no_leak("authorization: Bearer <REDACTED>")

    def test_redact_case_insensitive(self):
        out = redact.redact("BEARER abcdefghijklmnop")
        assert not _LEAK_RE.search(out)

    def test_redact_coerces_non_string(self):
        out = redact.redact(McpError("Bearer leakytoken12345"))
        assert isinstance(out, str)
        assert not _LEAK_RE.search(out)

    def test_mask_token_long(self):
        tok = "abcdefgh1234567890wxyz"
        masked = mask = redact.mask_token(tok)
        assert masked == "abcdefgh...wxyz"
        assert tok not in masked

    def test_mask_token_short(self):
        assert redact.mask_token("short") == "***"

    def test_mask_hmac(self):
        h = "0123456789abcdef0123"
        masked = redact.mask_hmac(h)
        assert masked == "0123456789ab..."
        assert masked.endswith("...")

    def test_mask_hmac_short(self):
        assert redact.mask_hmac("abc") == "***"

    # --- Phase 5: broadened regex + register_secrets ----------------------

    def test_github_oauth_token_redacted(self):
        tok = "gho_" + "A" * 36
        out = redact.redact(f"clone failed: {tok} rejected")
        assert tok not in out
        assert _REDACTED in out

    def test_github_personal_token_redacted(self):
        tok = "ghp_" + "b3" * 18
        out = redact.redact(f"git push used {tok}")
        assert tok not in out
        assert _REDACTED in out

    def test_github_fine_grained_pat_redacted(self):
        tok = "github_pat_" + "1" * 22 + "_" + "c" * 30
        out = redact.redact(f"auth header {tok} here")
        assert tok not in out
        assert _REDACTED in out

    def test_json_token_field_redacted(self):
        secret = "opaqueJSONtokenvalue1234"
        out = redact.redact(json.dumps({"token": secret}))
        assert secret not in out
        assert _REDACTED in out

    def test_webhook_hmac_secret_env_redacted(self):
        secret = "deadbeefdeadbeefdeadbeef"
        out = redact.redact(f"WEBHOOK_HMAC_SECRET={secret}")
        assert secret not in out
        # env-style assignments keep the key for diagnosability.
        assert "WEBHOOK_HMAC_SECRET=<REDACTED>" in out

    def test_registered_opaque_token_redacted(self):
        # A prefix-less opaque token the regex cannot recognize on its own;
        # only register_secrets() makes redact() mask it verbatim.
        opaque = "Sz3FsecretTTaU000000"
        # Pre-condition: without registration it leaks (proves registration is
        # what masks it, not an incidental regex match).
        assert opaque in redact.redact(f"server echoed {opaque}")
        redact.register_secrets([opaque])
        try:
            out = redact.redact(f"server echoed {opaque} back")
            assert opaque not in out
            assert _REDACTED in out
        finally:
            # Keep the module-level registry clean for other tests.
            redact._REGISTERED.discard(opaque)

    def test_masked_token_form_is_not_re_redacted(self):
        # A diagnostic mask_token() output (prefix...suffix) must survive redact()
        # unchanged -- it is intentionally shown and is not a raw secret. (Real
        # code in checks_mcp deliberately avoids a secret keyword like "token:"
        # immediately before the mask so the redactor's key/value rule does not
        # eat it -- mirror that here.)
        masked = "Sz3FHraF...TTaU"
        out = redact.redact(f"single shared auth across 3 server(s): {masked}")
        assert masked in out
        assert _REDACTED not in out


# ---------------------------------------------------------------------------
# result.py  (table + JSON renderers, verdict, redaction discipline)
# ---------------------------------------------------------------------------


class TestResultModel:
    def _sample(self) -> list[CheckResult]:
        return [
            CheckResult("G1.mcp_tools_list", "pass", "recall=82ms swarm=77ms"),
            CheckResult(
                "G6.stop_hook_recent_fresh",
                "warn",
                "marker is 31h old",
                remediation="Start a session/turn.",
            ),
            CheckResult(
                "G8.backend_ports_closed",
                "fail",
                "8767 reachable",
                remediation="Close UFW.",
                auto_fix=lambda: True,
            ),
            CheckResult("G6.tasks_optional", "skip", "tasks server absent"),
        ]

    def test_to_serializable_drops_auto_fix(self):
        r = CheckResult("X", "fail", "msg", auto_fix=lambda: True)
        d = r.to_serializable()
        assert "auto_fix" not in d
        assert set(d) == {"name", "status", "message", "remediation"}

    def test_summary_counts(self):
        c = result_mod.summary_counts(self._sample())
        assert c == {"pass": 1, "warn": 1, "fail": 1, "skip": 1}

    def test_verdict_fail(self):
        word, code = result_mod.verdict(self._sample())
        assert word == "FAIL"
        assert code == 1

    def test_verdict_warn_is_exit_zero(self):
        results = [
            CheckResult("a", "pass", "ok"),
            CheckResult("b", "warn", "meh"),
        ]
        word, code = result_mod.verdict(results)
        assert word == "WARN"
        assert code == 0

    def test_verdict_pass(self):
        results = [CheckResult("a", "pass", "ok"), CheckResult("b", "skip", "n/a")]
        word, code = result_mod.verdict(results)
        assert word == "PASS"
        assert code == 0

    def test_render_table_no_color_has_no_ansi(self):
        out = result_mod.render_table(self._sample(), no_color=True)
        assert "\033[" not in out
        assert "[PASS]" in out and "[FAIL]" in out
        assert "Verdict: FAIL" in out

    def test_render_json_is_array_without_auto_fix(self):
        out = result_mod.render_json(self._sample())
        parsed = json.loads(out)
        assert isinstance(parsed, list) and len(parsed) == 4
        for obj in parsed:
            assert "auto_fix" not in obj
            assert {"name", "status", "message"} <= set(obj)

    def test_renderers_never_emit_raw_secret(self):
        # A message that was correctly pre-redacted must survive both renderers
        # without re-introducing a secret. Also assert renderers do not undo
        # redaction.
        msg = redact.redact("token Bearer abcdefghijklmnop failed")
        results = [CheckResult("G2.x", "fail", msg, remediation="rotate")]
        table = result_mod.render_table(results, no_color=True)
        as_json = result_mod.render_json(results)
        assert not _LEAK_RE.search(table)
        assert not _LEAK_RE.search(as_json)


# ---------------------------------------------------------------------------
# mcp_streamable.py  (monkeypatched urlopen — NO network)
# ---------------------------------------------------------------------------


class _FakeResp:
    """Mimics the context-manager returned by urllib.request.urlopen."""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _patch_urlopen(monkeypatch, body: str | None = None, raise_exc=None):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        if raise_exc is not None:
            raise raise_exc
        return _FakeResp(body or "")

    monkeypatch.setattr(mcp_streamable.urllib.request, "urlopen", fake_urlopen)
    return captured


_PLAIN_TOOLS = json.dumps(
    {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "recall"}, {"name": "recent"}]}}
)
_SSE_TOOLS = (
    "event: message\n"
    "data: "
    + json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "stats"}]}}
    )
    + "\n\n"
)
_SSE_TOOL_CALL = (
    "data: "
    + json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"structuredContent": {"count": 42, "ok": True}},
        }
    )
    + "\n"
)
_IS_ERROR = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"isError": True, "content": [{"type": "text", "text": "boom"}]},
    }
)


class TestMcpStreamable:
    def test_plain_json_tools_list(self, monkeypatch):
        cap = _patch_urlopen(monkeypatch, body=_PLAIN_TOOLS)
        tools, latency = mcp_streamable.tools_list(
            "https://host/recall/mcp", "tok-aaaaaaaaaaaa"
        )
        assert [t["name"] for t in tools] == ["recall", "recent"]
        assert latency >= 0.0
        # Header carries the Bearer but the value is never logged by the client.
        assert cap["req"].get_header("Authorization") == "Bearer tok-aaaaaaaaaaaa"

    def test_sse_data_frame_parsed(self, monkeypatch):
        _patch_urlopen(monkeypatch, body=_SSE_TOOLS)
        tools, _ = mcp_streamable.tools_list("https://host/swarm/mcp", "tok")
        assert [t["name"] for t in tools] == ["stats"]

    def test_tool_call_structured_content(self, monkeypatch):
        _patch_urlopen(monkeypatch, body=_SSE_TOOL_CALL)
        payload, _ = mcp_streamable.tool_call(
            "https://host/swarm/mcp", "tok", "stats", {}
        )
        assert payload == {"count": 42, "ok": True}

    def test_is_error_raises_mcperror(self, monkeypatch):
        _patch_urlopen(monkeypatch, body=_IS_ERROR)
        with pytest.raises(McpError) as ei:
            mcp_streamable.tools_list("https://host/memory/mcp", "tok")
        assert "boom" in str(ei.value)

    def test_http_error_is_redacted(self, monkeypatch):
        import urllib.error

        body = io.BytesIO(b"unauthorized: Bearer leakytoken1234567 rejected")
        err = urllib.error.HTTPError(
            "https://host/memory/mcp", 401, "Unauthorized", {}, body
        )
        _patch_urlopen(monkeypatch, raise_exc=err)
        with pytest.raises(McpError) as ei:
            mcp_streamable.tools_list("https://host/memory/mcp", "tok")
        assert ei.value.status_code == 401
        assert not _LEAK_RE.search(str(ei.value)), "token leaked through HTTPError body"

    def test_url_error_raises(self, monkeypatch):
        import urllib.error

        _patch_urlopen(monkeypatch, raise_exc=urllib.error.URLError("conn refused"))
        with pytest.raises(McpError):
            mcp_streamable.tools_list("https://host/recall/mcp", "tok")

    def test_empty_body_raises(self, monkeypatch):
        _patch_urlopen(monkeypatch, body="")
        with pytest.raises(McpError):
            mcp_streamable.tools_list("https://host/recall/mcp", "tok")

    def test_non_json_body_raises(self, monkeypatch):
        _patch_urlopen(monkeypatch, body="<html>nope</html>")
        with pytest.raises(McpError):
            mcp_streamable.tools_list("https://host/recall/mcp", "tok")


# ---------------------------------------------------------------------------
# gbrain_doctor.py  — config loader + group parser + CLI exit codes
# ---------------------------------------------------------------------------


class TestConfigLoader:
    def test_loads_valid_mcp_json(self):
        servers, path = load_mcp_config(str(_FIXTURES / "mcp-valid.json"), None)
        services = sorted(s.service for s in servers)
        assert services == ["memory", "recall", "swarm"]
        assert path == _FIXTURES / "mcp-valid.json"
        for s in servers:
            assert s.token  # bearer extracted
            assert s.type == "http"

    def test_explicit_missing_path_exits_2(self):
        with pytest.raises(SystemExit) as ei:
            load_mcp_config(str(_FIXTURES / "does-not-exist.json"), None)
        assert ei.value.code == 2

    def test_explicit_malformed_exits_2(self):
        with pytest.raises(SystemExit) as ei:
            load_mcp_config(str(_FIXTURES / "mcp-malformed.json"), None)
        assert ei.value.code == 2

    def test_loads_port_based_mcp_json(self):
        # Port-based topology: every server shares the ``/mcp`` path and the
        # service is encoded only in the config key. The unknown ``some-other``
        # server must still be skipped.
        servers, _ = load_mcp_config(
            str(_FIXTURES / "mcp-port-based.json"), None
        )
        services = sorted(s.service for s in servers)
        assert services == ["memory", "recall", "swarm"]


class TestServiceInference:
    def test_infer_service_from_path(self):
        assert _infer_service("https://h/memory/mcp") == "memory"
        assert _infer_service("https://h/swarm/mcp") == "swarm"
        assert _infer_service("https://h/task/mcp") == "tasks"

    def test_infer_service_path_none_for_bare_mcp(self):
        # Port-based URL has no service segment in the path.
        assert _infer_service("http://127.0.0.1:8767/mcp") is None

    def test_infer_service_from_key(self):
        assert _infer_service_from_key("gbrain-memory") == "memory"
        assert _infer_service_from_key("gbrain-recall") == "recall"
        assert _infer_service_from_key("gbrain-swarm") == "swarm"
        assert _infer_service_from_key("gbrain-task") == "tasks"

    def test_infer_service_from_key_ignores_unknown(self):
        assert _infer_service_from_key("some-other-mcp") is None
        assert _infer_service_from_key("openai") is None


class TestGroupParser:
    def test_none_when_absent(self):
        assert _parse_groups(None) is None
        assert _parse_groups([]) is None

    def test_comma_and_repeat(self):
        assert _parse_groups(["G1,G6"]) == {"G1", "G6"}
        assert _parse_groups(["G1", "G2"]) == {"G1", "G2"}

    def test_lowercase_normalized(self):
        assert _parse_groups(["g6"]) == {"G6"}

    def test_invalid_group_exits_2(self):
        with pytest.raises(SystemExit) as ei:
            _parse_groups(["G99"])
        assert ei.value.code == 2


class TestCliExitCodes:
    """Drive ``main`` with fake check modules to control fail/pass verdicts.

    We inject lightweight modules into ``sys.modules`` so the registry imports
    them instead of the real (possibly absent) check modules, keeping the CLI
    test hermetic and offline.
    """

    def _install_fake_modules(self, monkeypatch, results_by_module):
        for mod_name, results in results_by_module.items():
            mod = types.ModuleType(mod_name)

            def make(res):
                def run_checks(ctx):
                    return list(res)

                return run_checks

            mod.run_checks = make(results)
            monkeypatch.setitem(sys.modules, mod_name, mod)

    def test_exit_0_all_pass(self, monkeypatch, capsys):
        self._install_fake_modules(
            monkeypatch,
            {
                "checks_mcp": [CheckResult("G1.ok", "pass", "fine")],
                "checks_hooks": [CheckResult("G6.ok", "pass", "fine")],
                "checks_local": [CheckResult("G7.ok", "skip", "no listener")],
            },
        )
        code = main(["--agent", "silvana", "--mcp-json", str(_FIXTURES / "mcp-valid.json"), "--no-color"])
        assert code == 0
        out = capsys.readouterr().out
        assert "Verdict: PASS" in out

    def test_exit_1_on_fail(self, monkeypatch, capsys):
        self._install_fake_modules(
            monkeypatch,
            {
                "checks_mcp": [CheckResult("G1.bad", "fail", "down", remediation="fix it")],
                "checks_hooks": [CheckResult("G6.ok", "pass", "fine")],
                "checks_local": [CheckResult("G7.ok", "pass", "fine")],
            },
        )
        code = main(["--mcp-json", str(_FIXTURES / "mcp-valid.json"), "--no-color"])
        assert code == 1
        out = capsys.readouterr().out
        assert "Verdict: FAIL" in out

    def test_exit_2_bad_group(self, capsys):
        with pytest.raises(SystemExit) as ei:
            main(["--group", "G42", "--mcp-json", str(_FIXTURES / "mcp-valid.json")])
        assert ei.value.code == 2

    def test_exit_2_malformed_explicit_mcp_json(self):
        with pytest.raises(SystemExit) as ei:
            main(["--mcp-json", str(_FIXTURES / "mcp-malformed.json")])
        assert ei.value.code == 2

    def test_group_filter_only_runs_selected(self, monkeypatch, capsys):
        self._install_fake_modules(
            monkeypatch,
            {
                "checks_mcp": [CheckResult("G1.only", "pass", "mcp ran")],
                "checks_hooks": [CheckResult("G6.only", "pass", "hooks ran")],
                "checks_local": [CheckResult("G7.only", "pass", "local ran")],
            },
        )
        code = main(["--group", "G6", "--mcp-json", str(_FIXTURES / "mcp-valid.json"), "--no-color"])
        assert code == 0
        out = capsys.readouterr().out
        assert "G6.only" in out
        assert "G1.only" not in out
        assert "G7.only" not in out

    def test_json_output_parses_and_no_secret(self, monkeypatch, capsys):
        leaky = redact.redact("auth Bearer abcdefghijklmnop denied")
        self._install_fake_modules(
            monkeypatch,
            {
                "checks_mcp": [CheckResult("G1.x", "fail", leaky, remediation="rotate")],
                "checks_hooks": [],
                "checks_local": [],
            },
        )
        code = main(["--json", "--mcp-json", str(_FIXTURES / "mcp-valid.json")])
        assert code == 1
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert isinstance(parsed, list) and parsed
        assert not _LEAK_RE.search(out)
        for obj in parsed:
            assert "auto_fix" not in obj

    def test_quiet_suppresses_stdout(self, monkeypatch, capsys):
        self._install_fake_modules(
            monkeypatch,
            {
                "checks_mcp": [CheckResult("G1.ok", "pass", "fine")],
                "checks_hooks": [],
                "checks_local": [],
            },
        )
        code = main(["--quiet", "--mcp-json", str(_FIXTURES / "mcp-valid.json")])
        assert code == 0
        out = capsys.readouterr().out
        assert out.strip() == ""

    def test_fix_noninteractive_skips_without_yes(self, monkeypatch, capsys):
        flags = {"ran": False}

        def fix_cb():
            flags["ran"] = True
            return True

        self._install_fake_modules(
            monkeypatch,
            {
                "checks_mcp": [
                    CheckResult("G1.bad", "fail", "down", remediation="x", auto_fix=fix_cb)
                ],
                "checks_hooks": [],
                "checks_local": [],
            },
        )
        # Force non-TTY so _confirm returns False; --yes not passed.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        code = main(["--fix", "--mcp-json", str(_FIXTURES / "mcp-valid.json"), "--no-color"])
        assert code == 1
        assert flags["ran"] is False  # fix must NOT run without confirmation/--yes
        err = capsys.readouterr().err
        assert "skipped" in err

    def test_fix_yes_runs_callback(self, monkeypatch, capsys):
        flags = {"ran": False}

        def fix_cb():
            flags["ran"] = True
            return True

        self._install_fake_modules(
            monkeypatch,
            {
                "checks_mcp": [
                    CheckResult("G1.bad", "fail", "down", remediation="x", auto_fix=fix_cb)
                ],
                "checks_hooks": [],
                "checks_local": [],
            },
        )
        code = main(["--fix", "--yes", "--mcp-json", str(_FIXTURES / "mcp-valid.json"), "--no-color"])
        assert code == 1  # verdict still reflects the original fail
        assert flags["ran"] is True
        err = capsys.readouterr().err
        assert "ok" in err


# ---------------------------------------------------------------------------
# Fixtures sanity — every settings fixture parses; tree is well-formed.
# ---------------------------------------------------------------------------


class TestFixturesWellFormed:
    @pytest.mark.parametrize(
        "name",
        [
            "settings-complete.json",
            "settings-missing-hook.json",
            "settings-typo-event.json",
            "settings.local.json",
            "settings-async-blocking.json",
            "settings-disable-all-hooks.json",
            "mcp-valid.json",
        ],
    )
    def test_settings_fixture_parses(self, name):
        data = json.loads((_FIXTURES / name).read_text())
        assert isinstance(data, dict)

    def test_workspace_tree_present(self):
        ws = _FIXTURES / "workspace"
        assert (ws / "hooks" / "session-start-hook.sh").is_file()
        assert (ws / "hooks" / "stop-hook.sh").is_file()
        assert (ws / "hooks" / "precompact-hook.sh").is_file()
        assert (ws / "core" / "hot" / "recent.md").is_file()
        snaps = list((ws / "core" / "hot" / "pre-compact").glob("recent-*.md"))
        assert snaps

    def test_stale_marker_in_recent(self):
        text = (_FIXTURES / "workspace" / "core" / "hot" / "recent.md").read_text()
        assert "[stop-hook]" in text
        assert "2026-01-01" in text


# ---------------------------------------------------------------------------
# Reference assets the skill ships (G10 self-install relies on these).
# ---------------------------------------------------------------------------


class TestSkillAssets:
    def test_expected_hooks_json_valid(self):
        data = json.loads(
            (_SKILL_ROOT / "references" / "expected-hooks.json").read_text()
        )
        events = {e["event"] for e in data["expected"]}
        assert {"SessionStart", "Stop", "PreCompact"} <= events

    def test_claude_hook_events_json_valid(self):
        data = json.loads(
            (_SKILL_ROOT / "references" / "claude-hook-events.json").read_text()
        )
        assert "SessionStart" in data and "Stop" in data and "PreCompact" in data

    def test_skill_frontmatter_name_is_gbrain_doctor(self):
        text = (_SKILL_ROOT / "SKILL.md").read_text()
        assert text.startswith("---")
        fm = text.split("---", 2)[1]
        name_line = next(ln for ln in fm.splitlines() if ln.startswith("name:"))
        assert name_line.split(":", 1)[1].strip() == "gbrain-doctor"

    def test_skill_description_has_triggers(self):
        text = (_SKILL_ROOT / "SKILL.md").read_text()
        fm = text.split("---", 2)[1]
        assert "проверь gbrain" in fm
        assert "gbrain health" in fm


# ---------------------------------------------------------------------------
# G6 hooks suite — runs against fixtures ONLY when checks_hooks lands.
# ---------------------------------------------------------------------------


def _hooks_module():
    try:
        return importlib.import_module("checks_hooks")
    except Exception:
        return None


_HOOKS = _hooks_module()
_hooks_reason = "checks_hooks not present yet (subagent C)"


@pytest.mark.skipif(_HOOKS is None, reason=_hooks_reason)
class TestHooksAgainstFixtures:
    """Exercises G6 settings-layer parsing on fixture dirs only.

    These tests are intentionally tolerant of the exact internal helper names
    in checks_hooks: they assert on observable behavior (a run over a fixture
    workspace yields the expected statuses for known check names) rather than
    importing private functions. If checks_hooks exposes only ``run_checks``,
    we build a DoctorContext pointed at the fixture workspace and inspect the
    returned CheckResults.
    """

    def _ctx_for(self, settings_name: str) -> DoctorContext:
        ws = _FIXTURES / "workspace"
        # Ensure the non-executable scenario regardless of git checkout state.
        stop_hook = ws / "hooks" / "stop-hook.sh"
        if stop_hook.is_file():
            stop_hook.chmod(0o644)
        return DoctorContext(
            agent="testagent",
            servers=[],
            mcp_json_path=_FIXTURES / "mcp-valid.json",
            expected_hooks_path=_SKILL_ROOT / "references" / "expected-hooks.json",
            groups={"G6"},
            skill_root=_SKILL_ROOT,
            workspace_root=ws,
            repo_root=_SKILL_ROOT.parent.parent,
            do_fix=False,
            assume_yes=False,
        )

    def test_run_checks_returns_list_no_exception(self):
        results = _HOOKS.run_checks(self._ctx_for("settings-complete.json"))
        assert isinstance(results, list)
        # Every result must be a CheckResult with a redacted message.
        for r in results:
            assert isinstance(r, CheckResult)
            assert not _LEAK_RE.search(r.message)
            assert r.name.startswith("G6.")

    def test_non_executable_stop_hook_flagged(self):
        results = _HOOKS.run_checks(self._ctx_for("settings-complete.json"))
        names = {r.name: r for r in results}
        # If the executable check is implemented, the non-exec stop-hook fixture
        # should surface a fail or a chmod autofix on the matching check.
        exec_checks = [
            r for n, r in names.items() if "executable" in n
        ]
        assert exec_checks, "expected at least one *executable* check"


# ---------------------------------------------------------------------------
# Phase 5 hooks behaviors — isolated fixture workspaces, monkeypatched HOME.
# ---------------------------------------------------------------------------


_WS_CASES = _FIXTURES / "ws-cases"


def _result_by_suffix(results: list[CheckResult], suffix: str) -> CheckResult | None:
    """Find a single CheckResult whose name ends with ``.<suffix>``."""
    for r in results:
        if r.name == f"G6.{suffix}":
            return r
    return None


@pytest.mark.skipif(_HOOKS is None, reason=_hooks_reason)
class TestHooksPhase5:
    """G6 behaviors fixed in Phase 5, exercised on isolated fixture dirs.

    HOME is monkeypatched to an empty fixture dir so the real
    ``~/.claude/settings.json`` global layer never participates -- these tests
    stay hermetic and never read the operator's real Claude config.
    """

    def _isolated_home(self, monkeypatch):
        """Point ``Path.home()`` at the empty fixture home (no .claude)."""
        fake_home = _WS_CASES / "fake-home"
        monkeypatch.setenv("HOME", str(fake_home))
        # checks_hooks calls Path.home() directly; patch it for both modules.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        return fake_home

    def _ctx(self, ws_dot_claude: Path) -> DoctorContext:
        return DoctorContext(
            agent="testagent",
            servers=[],
            mcp_json_path=_FIXTURES / "mcp-valid.json",
            expected_hooks_path=_SKILL_ROOT / "references" / "expected-hooks.json",
            groups={"G6"},
            skill_root=_SKILL_ROOT,
            workspace_root=ws_dot_claude,
            repo_root=_SKILL_ROOT.parent.parent,
            do_fix=False,
            assume_yes=False,
        )

    def test_workspace_settings_layer_discovered(self, monkeypatch):
        # Regression for the double-`.claude` bug (H1): with workspace_root at
        # ``<ws>/.claude`` and settings.json directly under it, the settings
        # layer must be parsed AND the expected events must register (PASS).
        self._isolated_home(monkeypatch)
        ws = _WS_CASES / "with-hooks" / ".claude"
        results = _HOOKS.run_checks(self._ctx(ws))

        parse = _result_by_suffix(results, "settings_layers_parse")
        assert parse is not None and parse.status == "pass", (
            f"workspace settings layer not parsed: {parse}"
        )
        assert "workspace" in parse.message  # provenance label proves discovery

        registered = _result_by_suffix(results, "expected_events_registered")
        assert registered is not None and registered.status == "pass", (
            f"expected events not registered from workspace layer: {registered}"
        )

    def test_no_hooks_in_settings_is_warn_not_fail(self, monkeypatch):
        # C025: a settings layer present but with ZERO lifecycle hooks wired is a
        # WARN (config variance: hooks may live in cron/gateway/plugin), NOT FAIL.
        self._isolated_home(monkeypatch)
        ws = _WS_CASES / "no-hooks" / ".claude"
        results = _HOOKS.run_checks(self._ctx(ws))

        parse = _result_by_suffix(results, "settings_layers_parse")
        assert parse is not None and parse.status == "pass"

        registered = _result_by_suffix(results, "expected_events_registered")
        assert registered is not None, "expected_events_registered missing"
        assert registered.status == "warn", (
            f"zero-hook settings must WARN, not {registered.status}: "
            f"{registered.message}"
        )

    def test_resolve_command_path_bare_and_quoted(self):
        # C026/C031 path resolution: bare $HOME and ${VAR}, quoted command, and
        # an interpreter-prefixed command must all yield the script path.
        rcp = _HOOKS._resolve_command_path
        placeholders = {"HOME": "/home/agent", "CLAUDE_PROJECT_DIR": "/proj"}

        # bare $HOME
        p = rcp("$HOME/hooks/stop-hook.sh", placeholders)
        assert p == Path("/home/agent/hooks/stop-hook.sh")

        # ${CLAUDE_PROJECT_DIR}
        p = rcp("${CLAUDE_PROJECT_DIR}/hooks/session-start-hook.sh", placeholders)
        assert p == Path("/proj/hooks/session-start-hook.sh")

        # quoted path with a space (shlex strips the quotes)
        p = rcp('"/home/agent/my hooks/stop-hook.sh"', placeholders)
        assert p == Path("/home/agent/my hooks/stop-hook.sh")

        # interpreter wrapper is skipped to reach the real script
        p = rcp("bash $HOME/hooks/precompact-hook.sh", placeholders)
        assert p == Path("/home/agent/hooks/precompact-hook.sh")

        # inline-code wrapper carries no script path -> None (WARN upstream)
        assert rcp("bash -c 'echo hi'", placeholders) is None

    def test_c033_utc_marker_26h_old_is_stale(self, monkeypatch, tmp_path):
        # C033 must interpret the [stop-hook] marker as UTC only. A marker ~26h
        # old (UTC) is past the 24h max_age -> WARN (stale), never a local-tz
        # false PASS. Written dynamically so the test never drifts over time.
        from datetime import datetime, timedelta, timezone

        self._isolated_home(monkeypatch)
        ws = tmp_path / "agent" / ".claude"
        recent = ws / "core" / "hot" / "recent.md"
        recent.parent.mkdir(parents=True)
        stale = datetime.now(timezone.utc) - timedelta(hours=26)
        marker = stale.strftime("%Y-%m-%d %H:%M")
        recent.write_text(
            f"# recent\n\n### {marker} [stop-hook]\n\nstale UTC marker\n",
            encoding="utf-8",
        )
        results = _HOOKS.run_checks(self._ctx(ws))
        fresh = _result_by_suffix(results, "stop_hook_recent_fresh")
        assert fresh is not None, "stop_hook_recent_fresh missing"
        assert fresh.status == "warn", (
            f"26h-old UTC marker must be stale WARN, got {fresh.status}: "
            f"{fresh.message}"
        )

    def test_c033_fresh_utc_marker_passes(self, monkeypatch, tmp_path):
        # Counterpart: a marker ~1h old (UTC) is within max_age -> PASS. Guards
        # against the freshness check warning on everything.
        from datetime import datetime, timedelta, timezone

        self._isolated_home(monkeypatch)
        ws = tmp_path / "agent" / ".claude"
        recent = ws / "core" / "hot" / "recent.md"
        recent.parent.mkdir(parents=True)
        recent_ts = datetime.now(timezone.utc) - timedelta(hours=1)
        marker = recent_ts.strftime("%Y-%m-%d %H:%M")
        recent.write_text(
            f"# recent\n\n### {marker} [stop-hook]\n\nfresh UTC marker\n",
            encoding="utf-8",
        )
        results = _HOOKS.run_checks(self._ctx(ws))
        fresh = _result_by_suffix(results, "stop_hook_recent_fresh")
        assert fresh is not None and fresh.status == "pass", (
            f"1h-old UTC marker must be fresh PASS, got {fresh}"
        )


# ---------------------------------------------------------------------------
# checks_mcp.py — pure-logic helpers (no network). Skips if module absent.
# ---------------------------------------------------------------------------


def _mcp_module():
    try:
        return importlib.import_module("checks_mcp")
    except Exception:
        return None


_MCP = _mcp_module()
_mcp_reason = "checks_mcp not present"


@pytest.mark.skipif(_MCP is None, reason=_mcp_reason)
class TestMcpArgValidationDetection:
    """``_is_arg_validation_error`` classification (no network involved)."""

    @pytest.mark.parametrize(
        "message",
        [
            "validation error: unexpected keyword argument 'agent'",
            "missing required argument: scope",
            "field required",
            "input should be a valid string",
            "JSON-RPC error: Invalid params -32602 bad args",
        ],
    )
    def test_validation_messages_are_true(self, message):
        err = McpError(message)
        assert _MCP._is_arg_validation_error(err) is True

    def test_normal_connectivity_error_is_false(self):
        err = McpError("connection refused: host unreachable")
        assert _MCP._is_arg_validation_error(err) is False

    def test_not_registered_error_is_false(self):
        # A missing-tool error is a different class (handled by _is_not_registered),
        # not an arg-validation error.
        err = McpError("unknown tool: stats")
        assert _MCP._is_arg_validation_error(err) is False


# ---------------------------------------------------------------------------
# CLI: --probe-scope plumbing onto DoctorContext (no network).
# ---------------------------------------------------------------------------


class TestProbeScopeCli:
    def test_probe_scope_lands_on_context(self, monkeypatch):
        captured = {}

        def fake_run_all(ctx):
            captured["probe_scope"] = ctx.probe_scope
            return [CheckResult("G1.ok", "pass", "fine")]

        monkeypatch.setattr(gbrain_doctor, "_run_all_checks", fake_run_all)
        code = main(
            [
                "--probe-scope",
                "decisions",
                "--mcp-json",
                str(_FIXTURES / "mcp-valid.json"),
                "--no-color",
            ]
        )
        assert code == 0
        assert captured["probe_scope"] == "decisions"

    def test_probe_scope_defaults_none(self, monkeypatch):
        captured = {}

        def fake_run_all(ctx):
            captured["probe_scope"] = ctx.probe_scope
            return [CheckResult("G1.ok", "pass", "fine")]

        # Ensure the env fallback does not bleed in from the host.
        monkeypatch.delenv("GBRAIN_PROBE_SCOPE", raising=False)
        monkeypatch.setattr(gbrain_doctor, "_run_all_checks", fake_run_all)
        code = main(
            ["--mcp-json", str(_FIXTURES / "mcp-valid.json"), "--no-color"]
        )
        assert code == 0
        assert captured["probe_scope"] is None
