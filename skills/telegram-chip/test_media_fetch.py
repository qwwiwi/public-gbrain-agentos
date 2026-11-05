"""Offline tests for media download / transcription (fake Telethon client, fake Groq).

No real Telegram or Groq calls. Run: .venv/bin/python -m unittest test_media_fetch -v
"""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    Document,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    MessageMediaContact,
    MessageMediaDocument,
    MessageMediaPoll,
    MessageMediaWebPage,
    Poll,
    PollResults,
    TextWithEntities,
    WebPage,
)

import api
import telegram_core as tc


def _document(attrs, mime="application/octet-stream", size=4):
    return Document(id=1, access_hash=1, file_reference=b"", date=datetime(2026, 1, 1),
                    mime_type=mime, size=size, dc_id=2, attributes=attrs)


def _msg(mid, media, text=""):
    return SimpleNamespace(id=mid, media=media, message=text, file=None)


VOICE = MessageMediaDocument(document=_document([DocumentAttributeAudio(duration=10, voice=True)], "audio/ogg"))
PDF = MessageMediaDocument(document=_document([DocumentAttributeFilename("a.pdf")], "application/pdf"))
BIG = MessageMediaDocument(document=_document([DocumentAttributeFilename("big.pdf")], "application/pdf", 60 * 1024 * 1024))
CONTACT = MessageMediaContact(phone_number="+70000000000", first_name="Ivan", last_name="", vcard="", user_id=0)
WEB_DOC = MessageMediaWebPage(webpage=WebPage(id=1, url="https://x", display_url="x", hash=0,
                                              document=_document([DocumentAttributeFilename("w.pdf")], "application/pdf")))
WEB_EMPTY = MessageMediaWebPage(webpage=WebPage(id=2, url="https://y", display_url="y", hash=0))
POLL = MessageMediaPoll(poll=Poll(id=1, question=TextWithEntities(text="q", entities=[]), answers=[], hash=0),
                        results=PollResults())


class FakeClient:
    """Mimics the Telethon calls used by fetch_media; download writes 4 bytes in chunks."""

    def __init__(self, messages):
        self.messages = messages
        self.downloads = 0
        self.file_args = []
        self.flood_once = False
        self.raise_on_entity = None

    async def get_entity(self, chat_id):
        if self.raise_on_entity:
            raise self.raise_on_entity
        return chat_id

    async def get_messages(self, entity, ids):
        return self.messages.get(ids)

    async def download_media(self, message, file):
        if self.flood_once:
            self.flood_once = False
            raise FloodWaitError(request=None, capture=5)
        self.downloads += 1
        self.file_args.append(file)
        target = file
        if file.endswith("/") or os.path.isdir(file):  # Telethon: directory -> generated name
            target = os.path.join(file, f"tg_{message.id}.bin")
        with open(target, "wb") as fh:
            for chunk in (b"a", b"b", b"c", b"d"):  # yield between chunks to expose races
                fh.write(chunk)
                fh.flush()
                await asyncio.sleep(0.01)
        return target


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tc.MEDIA_CACHE_DIR = Path(self.tmp.name) / "cache"
        self.client = FakeClient({
            1: _msg(1, VOICE), 2: _msg(2, PDF), 3: _msg(3, None, "hi"), 4: _msg(4, BIG),
            5: _msg(5, CONTACT), 6: _msg(6, WEB_DOC), 7: _msg(7, WEB_EMPTY), 8: _msg(8, POLL),
            11: _msg(11, VOICE), 12: _msg(12, VOICE), 13: _msg(13, VOICE), 14: _msg(14, VOICE),
        })
        t = tc.telegram
        t.client = self.client
        t._started = True
        t._last_request_time = None
        t._media_semaphore = asyncio.Semaphore(2)
        t._transcribe_semaphore = asyncio.Semaphore(2)
        t._media_locks = {}
        t._rate_lock = asyncio.Lock()
        self._env_key = os.environ.pop("GROQ_API_KEY", None)
        self._candidates = list(tc.GROQ_KEY_FILE_CANDIDATES)
        tc.GROQ_KEY_FILE_CANDIDATES[:] = []
        self._orig_post = httpx.AsyncClient.post
        self.api = TestClient(api.app)  # no `with` -> lifespan (real Telegram) never runs

    def tearDown(self):
        httpx.AsyncClient.post = self._orig_post
        tc.GROQ_KEY_FILE_CANDIDATES[:] = self._candidates
        os.environ.pop("GROQ_API_KEY", None)
        if self._env_key is not None:
            os.environ["GROQ_API_KEY"] = self._env_key
        self.tmp.cleanup()


class LegacyContractTests(Base):
    def test_no_media_is_200_with_error_json(self):
        r = self.api.get("/chats/42/messages/3/media")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.json()["data"]), {"success": False, "error": "Message has no media"})

    def test_unhandled_telegram_error_is_not_500(self):
        self.client.raise_on_entity = ValueError("Could not find the input entity")
        r = self.api.get("/chats/42/messages/1/media")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["success"])

    def test_success_keeps_success_and_path(self):
        data = json.loads(self.api.get("/chats/42/messages/2/media").json()["data"])
        self.assertTrue(data["success"])
        self.assertTrue(Path(data["path"]).is_file())
        self.assertEqual(data["file_name"], "a.pdf")

    def test_output_path_trailing_slash_saves_inside_new_dir(self):
        target = os.path.join(self.tmp.name, "newdir") + "/"
        data = json.loads(self.api.get("/chats/42/messages/2/media", params={"output_path": target}).json()["data"])
        self.assertTrue(data["success"])
        self.assertEqual(self.client.file_args[-1], target)  # raw string passed through
        self.assertEqual(os.path.dirname(data["path"]), target.rstrip("/"))


class V2ContractTests(Base):
    def test_status_codes(self):
        self.assertEqual(self.api.get("/chats/42/messages/3/media?v=2").status_code, 404)
        self.assertEqual(self.api.get("/chats/42/messages/4/media?v=2").status_code, 413)
        self.assertEqual(self.api.get("/chats/42/messages/8/media?v=2").status_code, 422)
        self.assertEqual(self.api.get("/chats/42/messages/7/media?v=2").status_code, 422)
        self.client.raise_on_entity = ValueError("boom")
        self.assertEqual(self.api.get("/chats/42/messages/2/media?v=2").status_code, 502)

    def test_contact_and_webpage_document_download(self):
        c = json.loads(self.api.get("/chats/42/messages/5/media?v=2").json()["data"])
        self.assertEqual(c["kind"], "contact")
        self.assertTrue(c["path"].endswith(".vcf"))
        w = json.loads(self.api.get("/chats/42/messages/6/media?v=2").json()["data"])
        self.assertEqual(w["file_name"], "w.pdf")

    def test_file_endpoint_streams_bytes(self):
        r = self.api.get("/chats/42/messages/2/media/file")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"abcd")


class CoreConcurrencyTests(Base):
    def test_concurrent_same_attachment_downloads_once(self):
        async def run():
            return await asyncio.gather(*[tc.telegram.fetch_media(42, 2) for _ in range(4)])
        results = asyncio.run(run())
        self.assertEqual(self.client.downloads, 1)
        for res in results:
            self.assertEqual(Path(res["path"]).read_bytes(), b"abcd")
        self.assertEqual(oct(Path(results[0]["path"]).stat().st_mode & 0o777), "0o600")
        leftovers = list((tc.MEDIA_CACHE_DIR / "42").glob("*.part-*"))
        self.assertEqual(leftovers, [])
        self.assertEqual(tc.telegram._media_locks, {})  # refcounted locks cleaned up

    def test_floodwait_releases_download_slot(self):
        seen = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay, *a, **k):
            if delay >= 5:  # the FloodWait backoff
                seen.append(tc.telegram._media_semaphore._value)
                return None
            return await real_sleep(delay)

        self.client.flood_once = True
        tc.asyncio.sleep = fake_sleep
        try:
            res = asyncio.run(tc.telegram.fetch_media(42, 2))
        finally:
            tc.asyncio.sleep = real_sleep
        self.assertEqual(seen, [2])  # both slots free while backing off
        self.assertEqual(Path(res["path"]).read_bytes(), b"abcd")


class RateLimiterTests(Base):
    def test_concurrent_permits_are_spaced(self):
        import time
        stamps = []

        async def acquire():
            await tc.telegram._wait_for_rate_limit()
            stamps.append(time.monotonic())

        async def run():
            await asyncio.gather(*[acquire() for _ in range(12)])

        start = time.monotonic()
        asyncio.run(run())
        elapsed = time.monotonic() - start
        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertGreaterEqual(elapsed, 2.15)  # 11 gaps * 0.2 s at 5 req/s
        self.assertGreaterEqual(min(gaps), 0.19)


class TranscribeTests(Base):
    def test_missing_key_503_and_download_still_works(self):
        self.assertEqual(self.api.get("/chats/42/messages/1/media?transcribe=1").status_code, 503)
        self.assertEqual(self.api.get("/chats/42/messages/1/media?v=2").status_code, 200)

    def test_key_redacted_on_groq_error(self):
        os.environ["GROQ_API_KEY"] = "gsk_FAKE_SECRET"

        async def bad(self_, url, **kw):
            return httpx.Response(401, text="invalid gsk_FAKE_SECRET")
        httpx.AsyncClient.post = bad
        r = self.api.get("/chats/42/messages/1/media?transcribe=1")
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("gsk_FAKE_SECRET", r.text)

    def test_streams_file_and_caps_concurrency(self):
        os.environ["GROQ_API_KEY"] = "gsk_FAKE_SECRET"
        state = {"inflight": 0, "max": 0}

        async def ok(self_, url, **kw):
            fh = kw["files"]["file"][1]
            assert hasattr(fh, "read") and not isinstance(fh, (bytes, bytearray)), "must stream a file object"
            assert kw["files"]["file"][0].endswith(".ogg")
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
            await asyncio.sleep(0.05)
            state["inflight"] -= 1
            return httpx.Response(200, text="привет\n")
        httpx.AsyncClient.post = ok

        async def run():
            return await asyncio.gather(*[tc.telegram.transcribe_media(42, m) for m in (11, 12, 13, 14)])
        results = asyncio.run(run())
        self.assertLessEqual(state["max"], 2)
        self.assertTrue(all(r["transcript"] == "привет" for r in results))
        again = asyncio.run(tc.telegram.transcribe_media(42, 11))
        self.assertTrue(again["transcript_cached"])


if __name__ == "__main__":
    unittest.main()
