"""Offline tests for the never-write-first rule on every HTTP send path (fake Telethon client).

No real Telegram calls. Run: .venv/bin/python -m unittest test_first_write -v
"""

import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient
from telethon.tl.types import Channel, ChatPhotoEmpty, User

import api
import telegram_core as tc

REFUSAL = "no dialog: contact never wrote first – sending is forbidden (ban risk)"


def _user(uid, is_self=False):
    return User(id=uid, is_self=is_self, first_name=f"u{uid}", access_hash=1)


def _channel(cid):
    return Channel(id=cid, title="g", photo=ChatPhotoEmpty(), date=None, megagroup=True, access_hash=1)


class FakeClient:
    def __init__(self, entities, history):
        self.entities = entities  # chat_id -> entity
        self.history = history  # user id -> list of messages (any order)
        self.sent = []
        self.history_reads = 0

    async def get_entity(self, cid):
        return self.entities[int(cid)]

    async def get_messages(self, entity, limit=1, offset_id=0, from_user=None):
        # Newest first, like Telethon; offset_id = exclusive upper bound.
        assert from_user is None, "from_user is filtered locally in private chats – must not be used"
        self.history_reads += 1
        msgs = sorted(self.history.get(entity.id, []), key=lambda m: -m.id)
        if offset_id:
            msgs = [m for m in msgs if m.id < offset_id]
        return msgs[:limit]

    async def send_message(self, entity, message, reply_to=None, parse_mode=None):
        self.sent.append(("msg", entity.id))
        return SimpleNamespace(id=100)

    async def send_file(self, entity, path, caption=None, reply_to=None, parse_mode=None):
        self.sent.append(("file", entity.id))
        return SimpleNamespace(id=101)

    async def forward_messages(self, to_entity, message_id, from_entity):
        self.sent.append(("fwd", to_entity.id))
        return [SimpleNamespace(id=102)]


class FirstWriteTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient(
            entities={1: _user(1), 2: _user(2), 3: _channel(3), 4: _user(4, is_self=True)},
            history={2: [SimpleNamespace(id=5, out=False)]},
        )
        t = tc.telegram
        t.client = self.client
        t._started = True
        t._last_request_time = None
        t._rate_lock = asyncio.Lock()
        t._last_message_time_per_chat = {}
        t.min_request_delay = 0.0
        self.tmp = tempfile.NamedTemporaryFile(suffix=".txt")
        self.api = TestClient(api.app)

    def tearDown(self):
        tc.telegram.min_request_delay = 0.2
        self.tmp.close()

    def _refused(self, r):
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json(), {"success": False, "data": None, "error": REFUSAL})

    def test_refused_when_contact_never_wrote(self):
        self._refused(self.api.post("/messages/send", json={"chat_id": 1, "message": "hi"}))
        self._refused(self.api.post("/messages/send-file", json={"chat_id": 1, "file_path": self.tmp.name}))
        self._refused(self.api.post("/messages/forward", json={"from_chat_id": 3, "to_chat_id": 1, "message_id": 9}))
        self.assertEqual(self.client.sent, [])

    def test_allowed_when_contact_wrote(self):
        r = self.api.post("/messages/send", json={"chat_id": 2, "message": "hi"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])
        self.api.post("/messages/send-file", json={"chat_id": 2, "file_path": self.tmp.name})
        self.api.post("/messages/forward", json={"from_chat_id": 3, "to_chat_id": 2, "message_id": 9})
        self.assertEqual(self.client.sent, [("msg", 2), ("file", 2), ("fwd", 2)])

    def test_outgoing_only_history_is_refused(self):
        self.client.history[1] = [SimpleNamespace(id=7, out=True)]
        self._refused(self.api.post("/messages/send", json={"chat_id": 1, "message": "hi"}))

    def test_latest_outgoing_does_not_hide_incoming(self):
        # Codex repro: incoming id 10, our reply id 11 -> must be allowed.
        self.client.history[1] = [SimpleNamespace(id=10, out=False), SimpleNamespace(id=11, out=True)]
        r = self.api.post("/messages/send", json={"chat_id": 1, "message": "hi"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["success"], r.text)
        self.assertEqual(self.client.sent, [("msg", 1)])

    def test_incoming_on_second_page_found(self):
        msgs = [SimpleNamespace(id=1, out=False)] + [SimpleNamespace(id=i, out=True) for i in range(2, 152)]
        self.client.history[1] = msgs
        r = self.api.post("/messages/send", json={"chat_id": 1, "message": "hi"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.history_reads, 2)

    def test_page_cap_refuses_when_nothing_incoming(self):
        self.client.history[1] = [SimpleNamespace(id=i, out=True) for i in range(1, 2500)]
        self._refused(self.api.post("/messages/send", json={"chat_id": 1, "message": "hi"}))
        self.assertEqual(self.client.history_reads, tc.FIRST_WRITE_MAX_PAGES)

    def test_groups_and_saved_messages_unaffected(self):
        for cid in (3, 4):
            r = self.api.post("/messages/send", json={"chat_id": cid, "message": "hi"})
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.history_reads, 0)
        self.assertEqual(self.client.sent, [("msg", 3), ("msg", 4)])


if __name__ == "__main__":
    unittest.main()
