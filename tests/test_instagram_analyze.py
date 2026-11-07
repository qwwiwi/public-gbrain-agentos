"""Tests for skills/instagram-superpower/scripts/analyze.sh (Loore REST)."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "skills" / "instagram-superpower" / "scripts" / "analyze.sh"
TEST_KEY = "test-key-not-real"


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class _Loore(BaseHTTPRequestHandler):
    """Minimal Loore REST double that records every request."""

    calls: list[tuple[str, str | None, dict]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        type(self).calls.append((self.path, self.headers.get("Authorization"), body))
        if self.path == "/v1/ig-profile":
            result = {"profile": {
                "id": "42", "username": "someuser", "fullName": "Some User",
                "followers": 1000, "following": 10, "posts": 5,
                "isVerified": False, "category": "Blogger", "biography": "bio",
            }}
        elif self.path == "/v1/ig-user-reels":
            result = {"items": [
                {"code": "AAA", "url": "https://www.instagram.com/reel/AAA/",
                 "takenAt": _iso(1), "views": 100, "likes": 5, "comments": 1,
                 "caption": "recent low"},
                {"code": "BBB", "url": "https://www.instagram.com/reel/BBB/",
                 "takenAt": _iso(2), "views": 900, "likes": 50, "comments": 3,
                 "caption": "recent top"},
                {"code": "OLD", "url": "https://www.instagram.com/reel/OLD/",
                 "takenAt": _iso(90), "views": 99999, "likes": 1, "comments": 0,
                 "caption": "too old"},
            ], "hasMore": False}
        elif self.path == "/v1/billing-balance":
            result = {"creditsRemaining": 12}
        else:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps({"status": "ok", "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        return


@pytest.fixture
def loore_server():
    _Loore.calls = []
    server = HTTPServer(("127.0.0.1", 0), _Loore)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def _run(base: str, home: Path, key: str | None = TEST_KEY) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "LOORE_API_BASE": base}
    if key:
        env["LOORE_API_KEY"] = key
    return subprocess.run(
        ["bash", str(SCRIPT), "someuser", "1", "14"],
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_analyze_uses_loore_rest(loore_server: str, tmp_path: Path) -> None:
    proc = _run(loore_server, tmp_path)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    paths = [c[0] for c in _Loore.calls]
    assert paths[:2] == ["/v1/ig-profile", "/v1/ig-user-reels"]
    assert all(c[1] == f"Bearer {TEST_KEY}" for c in _Loore.calls)
    assert _Loore.calls[0][2] == {"username": "someuser"}
    # Top-1 by engagement inside the window: BBB, never the old reel.
    assert "BBB" in proc.stdout
    assert "OLD" not in proc.stdout
    assert TEST_KEY not in proc.stdout + proc.stderr


def test_analyze_requires_key(loore_server: str, tmp_path: Path) -> None:
    proc = _run(loore_server, tmp_path, key=None)
    assert proc.returncode != 0
    assert "LOORE_API_KEY" in proc.stdout + proc.stderr
    assert _Loore.calls == []


def test_repo_has_no_hikerapi() -> None:
    needle = "hiker"
    hits = [
        str(p.relative_to(REPO_ROOT))
        for p in REPO_ROOT.rglob("*")
        if p.is_file()
        and not any(part.startswith(".") for part in p.relative_to(REPO_ROOT).parts)
        and p.suffix not in {".pyc"}
        and p != Path(__file__).resolve()
        and needle in p.read_text(errors="ignore").lower()
    ]
    assert hits == []
