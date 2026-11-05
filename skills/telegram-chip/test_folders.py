"""Offline tests for chat folders (GET /folders, POST /folders/upsert) with a fake Telethon client.

No real Telegram calls. Run: .venv/bin/python -m unittest test_folders -v
"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest
from telethon.tl.types import (
    DialogFilter,
    DialogFilterDefault,
    InputPeerChannel,
    InputPeerUser,
    TextWithEntities,
)
from telethon.tl.types.messages import DialogFilters

import api
import telegram_core as tc


def _user(uid):
    return InputPeerUser(user_id=uid, access_hash=uid * 7)


def _title(text):
    return TextWithEntities(text=text, entities=[])


class FakeClient:
    """Minimal Telethon surface used by the folder code."""

    def __init__(self, filters, known, cold=()):
        self.filters = filters
        self.known = known  # chat_id -> InputPeer
        self.cold = set(cold)  # ids resolvable only after get_dialogs()
        self.updates = []
        self.dialogs_calls = 0
        self.flood_on_update = False
        self.dialogs = []
        self.dialog_offsets = []
        self.dialog_pages_served = 0

    async def get_input_entity(self, cid):
        if cid in self.cold:
            raise ValueError(f"Could not find the input entity for {cid}")
        if cid not in self.known:
            raise ValueError(f"Could not find the input entity for {cid}")
        return self.known[cid]

    async def get_dialogs(self, limit=None, offset_date=None, offset_id=0, offset_peer=None):
        assert limit is not None and limit <= 100, "must page manually"
        self.dialogs_calls += 1
        self.dialog_offsets.append(offset_id)
        start = self.dialog_pages_served * limit
        self.dialog_pages_served += 1
        page = self.dialogs[start:start + limit]
        if start + limit >= len(self.dialogs):
            self.cold.clear()
        return page

    async def get_entity(self, peer):
        raise ValueError("not needed")

    async def __call__(self, request):
        await asyncio.sleep(0)  # yield like a real RPC so concurrent callers interleave
        if isinstance(request, GetDialogFiltersRequest):
            return DialogFilters(filters=list(self.filters))
        if isinstance(request, UpdateDialogFilterRequest):
            if self.flood_on_update:
                raise FloodWaitError(request=None, capture=17)
            self.updates.append(request)
            self.filters = [f for f in self.filters if getattr(f, "id", None) != request.id] + [request.filter]
            return True
        raise AssertionError(f"unexpected request {request!r}")


OTHER = DialogFilter(id=2, title=_title("Work"), pinned_peers=[], include_peers=[_user(900)],
                     exclude_peers=[], color=3, emoticon="💼")


class FolderTests(unittest.TestCase):
    def setUp(self):
        known = {i: _user(i) for i in range(1, 400)}
        known[-1001234] = InputPeerChannel(channel_id=1234, access_hash=5)
        self.client = FakeClient([DialogFilterDefault(), OTHER], known)
        t = tc.telegram
        t.client = self.client
        t._started = True
        t._last_request_time = None
        t._rate_lock = asyncio.Lock()
        t._folder_lock = asyncio.Lock()
        t.min_request_delay = 0.0
        self.api = TestClient(api.app)  # no `with` -> lifespan (real Telegram) never runs

    def tearDown(self):
        tc.telegram.min_request_delay = 0.2

    def upsert(self, **body):
        return self.api.post("/folders/upsert", json=body)

    def folder(self, title):
        for f in self.client.filters:
            if isinstance(f, DialogFilter) and tc._folder_title_text(f.title) == title:
                return f
        return None

    def assert_other_untouched(self):
        self.assertIs(self.folder("Work"), OTHER)
        self.assertTrue(all(u.id != 2 for u in self.client.updates))

    def test_create(self):
        r = self.upsert(title="Leads hot", color=1, emoticon="🔥", include_chat_ids=[1, 2, "-1001234"])
        self.assertEqual(r.status_code, 200, r.text)
        data = json.loads(r.json()["data"])
        self.assertEqual(data["action"], "created")
        self.assertEqual(data["id"], 3)  # 2 is taken by "Work"
        f = self.folder("Leads hot")
        self.assertIsInstance(f.title, TextWithEntities)
        self.assertEqual(len(f.include_peers), 3)
        self.assertEqual((f.color, f.emoticon), (1, "🔥"))
        self.assert_other_untouched()

    def test_add_remove_replace(self):
        self.upsert(title="Leads", include_chat_ids=[1, 2])
        r = self.upsert(title="Leads", include_chat_ids=[2, 3], mode="add")
        d = json.loads(r.json()["data"])
        self.assertEqual((d["action"], d["include_count"], d["added"]), ("updated", 3, ["3"]))
        r = self.upsert(title="Leads", include_chat_ids=[1], mode="remove")
        d = json.loads(r.json()["data"])
        self.assertEqual((d["include_count"], d["removed"]), (2, ["1"]))
        r = self.upsert(title="Leads", include_chat_ids=[5], mode="replace")
        d = json.loads(r.json()["data"])
        self.assertEqual(d["include_peer_ids"], ["5"])
        self.assertEqual(sorted(d["removed"]), ["2", "3"])
        self.assertEqual(len(self.client.filters), 3)  # default + Work + Leads, same id reused
        self.assertEqual(self.folder("Leads").id, 3)
        self.assert_other_untouched()

    def test_unchanged_skips_write_and_keeps_color(self):
        self.upsert(title="Leads", color=4, include_chat_ids=[1])
        n = len(self.client.updates)
        d = json.loads(self.upsert(title="Leads", include_chat_ids=[1], mode="add").json()["data"])
        self.assertEqual(d["action"], "unchanged")
        self.assertEqual(len(self.client.updates), n)
        self.upsert(title="Leads", include_chat_ids=[2], mode="add")  # color omitted -> kept
        self.assertEqual(self.folder("Leads").color, 4)
        self.upsert(title="Leads", color=None, include_chat_ids=[], mode="add")  # explicit null clears
        self.assertIsNone(self.folder("Leads").color)

    def test_existing_folder_title_preserved_and_others_untouched(self):
        r = self.upsert(title="Work", include_chat_ids=[7], mode="add")
        self.assertEqual(r.status_code, 200)
        f = self.folder("Work")
        self.assertEqual(f.id, 2)
        self.assertEqual((f.color, f.emoticon), (3, "💼"))
        self.assertEqual(len(f.include_peers), 2)

    def test_title_as_plain_str_layer(self):
        old = DialogFilter(id=2, title=_title("x"), pinned_peers=[], include_peers=[], exclude_peers=[])
        old.title = "Legacy"  # simulate a pre-190 layer object
        self.client.filters = [DialogFilterDefault(), old]
        r = self.upsert(title="Legacy", include_chat_ids=[1], mode="add")
        self.assertEqual(json.loads(r.json()["data"])["id"], 2)
        self.assertEqual(self.client.updates[-1].filter.title, "Legacy")
        # Constructor detection: a str-typed signature yields a plain str title.
        with mock.patch("inspect.signature") as sig:
            sig.return_value.parameters = {"title": mock.Mock(annotation="str")}
            self.assertEqual(tc._make_folder_title("New"), "New")
        self.assertIsInstance(tc._make_folder_title("New"), TextWithEntities)

    def test_limit_422(self):
        r = self.upsert(title="Big", include_chat_ids=list(range(1, 202)))
        self.assertEqual(r.status_code, 422)
        self.assertIn("limit is 200", r.json()["detail"])
        self.assertEqual(self.client.updates, [])
        self.assertEqual(self.upsert(title="Big", include_chat_ids=list(range(1, 201))).status_code, 200)

    def test_validation_and_unresolved(self):
        self.assertEqual(self.upsert(title="X", color=9, include_chat_ids=[1]).status_code, 422)
        self.assertEqual(self.upsert(title="X", include_chat_ids=[1], mode="drop").status_code, 422)
        self.assertEqual(self.upsert(title="ThirteenChars", include_chat_ids=[1]).status_code, 422)
        r = self.upsert(title="X", include_chat_ids=[1, 99999])
        self.assertEqual(r.status_code, 422)
        self.assertIn("99999", r.json()["detail"])
        self.assertEqual(self.upsert(title="Nope", include_chat_ids=[1], mode="remove").status_code, 422)
        self.upsert(title="One", include_chat_ids=[1])
        self.assertEqual(self.upsert(title="One", include_chat_ids=[1], mode="remove").status_code, 422)
        self.assertEqual(self.client.updates[-1].filter.title.text, "One")  # empty-folder write blocked

    def test_cache_warm_on_missing_entity(self):
        self.client.cold = {5, 6}
        r = self.upsert(title="Warm", include_chat_ids=[5, 6])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.dialogs_calls, 1)

    def test_warmup_pages_dialogs_one_permit_per_page(self):
        from datetime import datetime
        self.client.dialogs = [
            SimpleNamespace(message=SimpleNamespace(id=1000 - i, date=datetime(2026, 1, 1)), input_entity=_user(i + 1))
            for i in range(250)
        ]
        self.client.cold = {5}
        permits = []
        orig = tc.telegram._wait_for_rate_limit

        async def counting():
            permits.append(1)
            await orig()
        tc.telegram._wait_for_rate_limit = counting
        try:
            r = self.upsert(title="Warm", include_chat_ids=[5])
        finally:
            del tc.telegram._wait_for_rate_limit
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.dialogs_calls, 3)  # 100 + 100 + 50
        self.assertEqual(self.client.dialog_offsets, [0, 901, 801])
        # 2 failed/ok resolves + 3 dialog pages + retry resolve + get filters + update
        self.assertGreaterEqual(len(permits), 3 + 4)

    def test_concurrent_upserts_serialized(self):
        async def run():
            return await asyncio.gather(
                tc.telegram.upsert_folder("Alpha", [1]),
                tc.telegram.upsert_folder("Beta", [2]),
                tc.telegram.upsert_folder("Alpha", [3], mode="add"),
                tc.telegram.upsert_folder("Alpha", [4], mode="add"),
            )
        res = asyncio.run(run())
        self.assertEqual(sorted({r["id"] for r in res}), [3, 4])
        self.assertEqual(len(self.folder("Alpha").include_peers), 3)
        self.assertEqual(len(self.folder("Beta").include_peers), 1)
        self.assert_other_untouched()

    def test_title_with_spaces_rejected_exact_match(self):
        r = self.upsert(title=" Work ", include_chat_ids=[7], mode="add")
        self.assertEqual(r.status_code, 422)
        self.assertIs(self.folder("Work"), OTHER)
        self.assertEqual(self.client.updates, [])

    def test_floodwait_429(self):
        self.client.flood_on_update = True
        r = self.upsert(title="Flood", include_chat_ids=[1])
        self.assertEqual(r.status_code, 429)

    def test_get_folders_shape(self):
        self.upsert(title="Leads", include_chat_ids=[1, 2])
        r = self.api.get("/folders", params={"include_chats": "false"})
        folders = json.loads(r.json()["data"])
        leads = next(f for f in folders if f["title"] == "Leads")
        self.assertEqual(leads["include_peer_ids"], ["1", "2"])
        self.assertEqual((leads["include_count"], leads["exclude_count"], leads["pinned_count"]), (2, 0, 0))
        work = next(f for f in folders if f["title"] == "Work")
        self.assertEqual((work["color"], work["emoticon"]), (3, "💼"))
        self.assertEqual(folders[0]["type"], "default")


if __name__ == "__main__":
    unittest.main()
