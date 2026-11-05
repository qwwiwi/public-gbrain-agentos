"""Offline tests for POST /supergroups, POST /chats/{id}/history-visible and the create_group fix.

Fake Telethon client, no real Telegram calls. Run: .venv/bin/python -m unittest test_supergroup -v
"""

import asyncio
import json
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from telethon.errors import (
    ChannelsTooMuchError,
    ChatNotModifiedError,
    FloodWaitError,
    RpcCallFailError,
    TimedOutError,
    UserBotError,
)
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    InviteToChannelRequest,
    TogglePreHistoryHiddenRequest,
)
from telethon.tl.functions.messages import CreateChatRequest
from telethon.tl.types import Channel, Chat, ChatPhotoEmpty, MissingInvitee, Updates, User
from telethon.tl.types.messages import InvitedUsers

import api
import telegram_core as tc

CHANNEL_ID = 2_000_000_123


def _user(uid, bot=False):
    return User(id=uid, first_name=f"u{uid}", access_hash=1, bot=bot)


def _channel(cid=CHANNEL_ID):
    return Channel(id=cid, title="Client | Support", photo=ChatPhotoEmpty(), date=None,
                   megagroup=True, access_hash=1)


def _basic_chat(cid):
    return Chat(id=cid, title="basic", photo=ChatPhotoEmpty(), participants_count=1, date=None, version=1)


def _updates(chats):
    return Updates(updates=[], users=[], chats=chats, date=None, seq=0)


class FakeClient:
    """Records every request; behaviour per request type is set by the test."""

    def __init__(self, entities):
        self.entities = entities
        self.calls = []
        self.invite_errors = {}  # user id -> exception
        self.invite_missing = set()  # user ids reported as missing_invitees
        self.create_chat_result = None
        self.create_channel_error = None
        self.toggle_error = None
        # Real Telethon client attributes (defaults of TelegramBaseClient).
        self.flood_sleep_threshold = 60
        self._request_retries = 5
        self.slept = []  # FloodWaits Telethon would have slept on (and resent the request)

    async def get_entity(self, cid):
        if isinstance(cid, str) and not cid.lstrip("-").isdigit():
            return self.entities[cid]
        return self.entities[int(cid)]

    async def __call__(self, request, ordered=False):
        self.calls.append((request, {
            "threshold": self.flood_sleep_threshold,
            "retries": self._request_retries,
            "rate_lock_held": tc.telegram._rate_lock.locked(),
        }))
        try:
            return self._dispatch(request)
        except FloodWaitError as e:
            # Telethon 1.43 users._call: sleeps and resends while seconds <= flood_sleep_threshold.
            if e.seconds <= self.flood_sleep_threshold:
                self.slept.append(e.seconds)
                return _updates([_channel()])
            raise

    def _dispatch(self, request):
        if isinstance(request, CreateChannelRequest):
            if self.create_channel_error:
                raise self.create_channel_error
            return _updates([_channel()])
        if isinstance(request, InviteToChannelRequest):
            uid = request.users[0].id
            if uid in self.invite_errors:
                raise self.invite_errors[uid]
            missing = [MissingInvitee(user_id=uid)] if uid in self.invite_missing else []
            return InvitedUsers(updates=_updates([_channel()]), missing_invitees=missing)
        if isinstance(request, TogglePreHistoryHiddenRequest):
            if self.toggle_error:
                raise self.toggle_error
            return _updates([])
        if isinstance(request, CreateChatRequest):
            return self.create_chat_result
        raise AssertionError(f"unexpected request {type(request).__name__}")

    def of_type(self, cls):
        return [(r, snap) for r, snap in self.calls if isinstance(r, cls)]


class SupergroupTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient({
            11: _user(11, bot=True), 12: _user(12, bot=True), 13: _user(13),
            "@helper_bot": _user(11, bot=True),
            int(f"-100{CHANNEL_ID}"): _channel(), CHANNEL_ID: _channel(), 77: _basic_chat(77), -77: _basic_chat(77),
        })
        t = tc.telegram
        t.client = self.client
        t._started = True
        t._last_request_time = None
        t._rate_lock = asyncio.Lock()
        t.min_request_delay = 0.0
        self.api = TestClient(api.app)

    def tearDown(self):
        tc.telegram.min_request_delay = 0.2

    def _ok(self, r):
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["success"], body)
        return json.loads(body["data"])

    def test_create_supergroup_returns_marked_id_and_makes_history_visible(self):
        data = self._ok(self.api.post("/supergroups", json={
            "title": "Client | Support", "about": "x", "users": [11, "@helper_bot"]}))
        self.assertEqual(data["chat_id"], int(f"-100{CHANNEL_ID}"))
        self.assertTrue(data["created"])
        self.assertEqual([i["status"] for i in data["invites"]], ["invited", "invited"])
        self.assertEqual(data["history"], {"history_visible": True, "changed": True})
        (create, snap), = self.client.of_type(CreateChannelRequest)
        self.assertTrue(create.megagroup)
        self.assertEqual(create.about, "x")
        self.assertEqual(snap, {"threshold": 0, "retries": 0, "rate_lock_held": True},
                         "create: one attempt (retry_range(0) = 1 send), FloodWait raised, lock held")
        for _, snap in self.client.of_type(InviteToChannelRequest) + self.client.of_type(TogglePreHistoryHiddenRequest):
            self.assertEqual(snap["threshold"], 0)
            self.assertEqual(snap["retries"], 5, "only the create is limited to a single attempt")
            self.assertTrue(snap["rate_lock_held"])
        self.assertEqual((self.client.flood_sleep_threshold, self.client._request_retries), (60, 5),
                         "client values restored after the call")
        (toggle, _), = self.client.of_type(TogglePreHistoryHiddenRequest)
        self.assertFalse(toggle.enabled)

    def test_invite_failure_is_reported_not_raised(self):
        self.client.invite_errors[12] = UserBotError(request=None)
        self.client.invite_missing.add(13)
        data = self._ok(self.api.post("/supergroups", json={"title": "T", "users": [11, 12, 13]}))
        by_user = {i["user"]: i for i in data["invites"]}
        self.assertEqual(by_user[11]["status"], "invited")
        self.assertEqual(by_user[12]["status"], "error")
        self.assertIn("UserBotError", by_user[12]["error"])
        self.assertEqual(by_user[13]["status"], "not_invited")
        self.assertEqual(data["chat_id"], int(f"-100{CHANNEL_ID}"))
        self.assertEqual(len(self.client.of_type(CreateChannelRequest)), 1)

    def test_floodwait_on_invite_skips_rest(self):
        self.client.invite_errors[11] = FloodWaitError(request=None, capture=30)
        data = self._ok(self.api.post("/supergroups", json={"title": "T", "users": [11, 12]}))
        self.assertEqual([i["status"] for i in data["invites"]], ["error", "skipped"])
        self.assertNotIn("flood_wait", data["invites"][0])

    def test_client_values_restored_after_create_error(self):
        self.client.create_channel_error = ChannelsTooMuchError(request=None)
        body = self.api.post("/supergroups", json={"title": "T"}).json()
        self.assertFalse(body["success"])
        self.assertIn("nothing was created", body["error"])
        self.assertEqual((self.client.flood_sleep_threshold, self.client._request_retries), (60, 5))
        self.assertFalse(tc.telegram._rate_lock.locked())

    def test_floodwait_raises_instead_of_sleeping(self):
        # 30s is below the default threshold (60): without the override Telethon would sleep + resend.
        self.client.create_channel_error = FloodWaitError(request=None, capture=30)
        body = self.api.post("/supergroups", json={"title": "T"}).json()
        self.assertFalse(body["success"])
        self.assertIn("FloodWait 30s", body["error"])
        self.assertEqual(self.client.slept, [])
        self.client.invite_errors[11] = FloodWaitError(request=None, capture=5)
        self.client.create_channel_error = None
        data = self._ok(self.api.post("/supergroups", json={"title": "T", "users": [11]}))
        self.assertEqual(data["invites"][0]["error"], "FloodWait 5s")
        self.assertEqual(self.client.slept, [])

    def test_ambiguous_create_failure_is_outcome_unknown(self):
        for err in (TimedOutError(request=None, message="Timeout", code=-503), RpcCallFailError(request=None),
                    ValueError("Request was unsuccessful 1 time(s)"), ConnectionError("reset")):
            self.client.create_channel_error = err
            body = self.api.post("/supergroups", json={"title": "Client | Support"}).json()
            self.assertFalse(body["success"], err)
            self.assertIn("outcome unknown", body["error"])
            self.assertIn("do NOT retry", body["error"])
            self.assertIn("/chats/list", body["error"])

    def test_exception_after_create_rpc_reports_created(self):
        with mock.patch.object(tc.telegram, "_finish_supergroup", side_effect=RuntimeError("boom")):
            data = self._ok(self.api.post("/supergroups", json={"title": "T", "users": [11]}))
        self.assertTrue(data["created"])
        self.assertEqual(data["chat_id"], int(f"-100{CHANNEL_ID}"))
        self.assertIn("do NOT retry", data["warning"])
        self.assertEqual(len(self.client.of_type(CreateChannelRequest)), 1)

    def test_history_already_visible_is_not_an_error(self):
        self.client.toggle_error = ChatNotModifiedError(request=None)
        data = self._ok(self.api.post("/supergroups", json={"title": "T"}))
        self.assertEqual(data["history"], {"history_visible": True, "changed": False})

    def test_create_floodwait_is_an_error_without_retry(self):
        self.client.create_channel_error = FloodWaitError(request=None, capture=120)
        r = self.api.post("/supergroups", json={"title": "T", "users": [11]})
        self.assertFalse(r.json()["success"])
        self.assertIn("FloodWait 120s", r.json()["error"])
        self.assertEqual(len(self.client.of_type(CreateChannelRequest)), 1)
        self.assertEqual(self.client.of_type(InviteToChannelRequest), [])

    def test_create_group_handles_invited_users_wrapper(self):
        self.client.create_chat_result = InvitedUsers(
            updates=_updates([_basic_chat(555)]), missing_invitees=[MissingInvitee(user_id=13)])
        r = self.api.post("/groups", json={"title": "G", "users": [11, 13]})
        body = r.json()
        self.assertTrue(body["success"], body)
        self.assertIn("Chat ID: 555", body["data"])
        self.assertIn("[13]", body["data"])

    def test_create_group_plain_updates_still_works(self):
        self.client.create_chat_result = _updates([_basic_chat(556)])
        body = self.api.post("/groups", json={"title": "G", "users": [11]}).json()
        self.assertEqual(body, {"success": True, "data": "Group 'G' created successfully. Chat ID: 556", "error": None})

    def test_history_visible_on_basic_group_gives_clear_error(self):
        body = self.api.post("/chats/-77/history-visible", json={"visible": True}).json()
        self.assertFalse(body["success"])
        self.assertIn("basic group", body["error"])
        self.assertIn("/supergroups", body["error"])
        self.assertEqual(self.client.of_type(TogglePreHistoryHiddenRequest), [])

    def test_history_visible_on_supergroup_default_body(self):
        body = self.api.post(f"/chats/-100{CHANNEL_ID}/history-visible").json()
        self.assertTrue(body["success"], body)
        self.assertEqual(json.loads(body["data"])["chat_id"], int(f"-100{CHANNEL_ID}"))
        (toggle, _), = self.client.of_type(TogglePreHistoryHiddenRequest)
        self.assertFalse(toggle.enabled)

    def test_history_hidden(self):
        self.api.post(f"/chats/-100{CHANNEL_ID}/history-visible", json={"visible": False})
        (toggle, _), = self.client.of_type(TogglePreHistoryHiddenRequest)
        self.assertTrue(toggle.enabled)


if __name__ == "__main__":
    unittest.main()
