"""Offline tests for the store (no network)."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from store import Store


def run(coro):
    return asyncio.run(coro)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "scraper.db")

    def tearDown(self):
        run(self.store.close())
        self.tmp.cleanup()

    def test_seed_is_idempotent(self):
        seed = [{"source": "kufar", "ref": "/l/videokarty", "label": "K", "filter": ["3090"],
                 "params": {"max_pages": 5}, "active": True}]

        async def scenario():
            await self.store.open()
            await self.store.seed_watches(seed)
            await self.store.seed_watches(seed)
            watches = await self.store.watches()
            return watches

        watches = run(scenario())
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0]["filter"], ["3090"])
        self.assertEqual(watches[0]["params"], {"max_pages": 5})
        self.assertTrue(watches[0]["active"])

    def test_upsert_new_then_price_drop(self):
        async def scenario():
            await self.store.open()
            await self.store.seed_watches([{"source": "kufar", "ref": "/l/videokarty"}])
            watch = (await self.store.watches())[0]
            record, is_new, old, new = await self.store.upsert_listing(
                source="kufar", external_id="1", watch_id=watch["id"], title="RTX 3090",
                price=5000.0, currency="BYN", region="Минск", condition="", description="",
                images=["a.jpg"], url="https://kufar.by/item/1")
            first = (is_new, old, new, record["price"])
            record, is_new, old, new = await self.store.upsert_listing(
                source="kufar", external_id="1", watch_id=watch["id"], title="RTX 3090",
                price=4500.0, currency="BYN", region="Минск", condition="", description="",
                images=["a.jpg"], url="https://kufar.by/item/1")
            second = (is_new, old, new)
            history = await self.store.listings(watch_id=watch["id"])
            return first, second, len(history)

        first, second, listing_count = run(scenario())
        self.assertEqual(first, (True, None, 5000.0, 5000.0))
        self.assertEqual(second, (False, 5000.0, 4500.0))
        self.assertEqual(listing_count, 1)

    def test_alerts_pending_and_delivery(self):
        async def scenario():
            await self.store.open()
            await self.store.upsert_listing(source="kufar", external_id="9", watch_id=None,
                                            title="RTX 4090", price=9000.0, currency="BYN",
                                            region="", condition="", description="", images=[],
                                            url="https://kufar.by/item/9")
            listings = await self.store.listings()
            await self.store.add_alert(listings[0]["id"], "new", "Новое объявление: RTX 4090")
            pending = await self.store.pending_alerts()
            await self.store.mark_delivered([pending[0]["id"]])
            return len(pending), await self.store.pending_alerts()

        pending, after = run(scenario())
        self.assertEqual(pending, 1)
        self.assertEqual(after, [])

    def test_price_medians_over_window(self):
        async def scenario():
            await self.store.open()
            await self.store.seed_watches([{"source": "kufar", "ref": "/l/videokarty"}])
            watch_id = (await self.store.watches())[0]["id"]
            await self.store.upsert_listing(source="kufar", external_id="1", watch_id=watch_id,
                                            title="a", price=3000.0, currency="BYN", region="",
                                            condition="", description="", images=[], url="")
            await self.store.upsert_listing(source="kufar", external_id="2", watch_id=watch_id,
                                            title="b", price=5000.0, currency="BYN", region="",
                                            condition="", description="", images=[], url="")
            listing2 = [r for r in await self.store.listings(watch_id=watch_id)
                        if r["external_id"] == "2"][0]
            await self.store.add_price_point(listing2["id"], 9000.0, "USD")
            now = time.time()
            return await self.store.price_medians(watch_id, now - 3600), \
                await self.store.price_medians(watch_id, now + 3600)

        recent, empty = run(scenario())
        self.assertEqual(recent, {"BYN": 4000.0, "USD": 9000.0})
        self.assertEqual(empty, {})

    def test_owner_and_subscriptions(self):
        async def scenario():
            await self.store.open()
            watch = await self.store.add_watch({"source": "kufar", "ref": "/l?query=x", "owner": 7})
            other = await self.store.add_watch({"source": "kufar", "ref": "/l/other", "label": "shared"})
            await self.store.subscribe(other["id"], 7)
            await self.store.subscribe(other["id"], 8)
            mine, mine8 = await self.store.watches_for_owner(7), await self.store.watches_for_owner(8)
            await self.store.unsubscribe(other["id"], 7)
            return watch, other, mine, mine8, await self.store.watches_for_owner(7)

        watch, other, mine, mine8, after = run(scenario())
        self.assertEqual([w["id"] for w in mine], [watch["id"], other["id"]])
        self.assertFalse(mine[0]["subscribed"])
        self.assertTrue(mine[1]["subscribed"])
        self.assertEqual([w["id"] for w in mine8], [other["id"]])
        self.assertEqual([w["id"] for w in after], [watch["id"]])

    def test_pending_alerts_recipients(self):
        async def upsert(source, external_id, watch_id, url, price):
            await self.store.upsert_listing(source=source, external_id=external_id, watch_id=watch_id,
                                            title="t", price=price, currency="BYN", region="", condition="",
                                            description="", images=[], url=url)

        async def scenario():
            await self.store.open()
            system = await self.store.add_watch({"source": "kufar", "ref": "/l/s"})
            owned = await self.store.add_watch({"source": "kufar", "ref": "/l/o", "owner": 7})
            shared = await self.store.add_watch({"source": "kufar", "ref": "/l/h", "owner": 9})
            await self.store.subscribe(shared["id"], 7)
            await upsert("kufar", "1", system["id"], "u1", 1.0)
            await upsert("kufar", "2", owned["id"], "u2", 2.0)
            await upsert("kufar", "3", shared["id"], "u3", 3.0)
            for row in await self.store.listings():
                await self.store.add_alert(row["id"], "new", "x")
            item = await self.store.add_item(5, "https://www.kufar.by/item/42")
            await upsert("item", "42", None, "u4", 4.0)
            listing4 = [r for r in await self.store.listings() if r["url"] == "u4"][0]
            await self.store.touch_item(item["id"], listing4["id"])
            await self.store.add_alert(listing4["id"], "new", "x")
            return await self.store.pending_alerts()

        by_url = {a["url"]: a["recipients"] for a in run(scenario())}
        self.assertEqual(by_url, {"u1": [None], "u2": [7], "u3": [7, 9], "u4": [5]})

    def test_items_crud(self):
        async def scenario():
            await self.store.open()
            item = await self.store.add_item(7, "https://www.kufar.by/item/42")
            mine, others = await self.store.items(owner=7), await self.store.items(owner=8)
            await self.store.set_item_active(item["id"], False)
            active = await self.store.active_items()
            await self.store.delete_item(item["id"])
            return mine, others, active, await self.store.item(item["id"])

        mine, others, active, gone = run(scenario())
        self.assertEqual(mine[0]["url"], "https://www.kufar.by/item/42")
        self.assertEqual(mine[0]["owner"], 7)
        self.assertEqual(others, [])
        self.assertEqual(active, [])
        self.assertIsNone(gone)

    def test_scrape_runs(self):
        async def scenario():
            await self.store.open()
            await self.store.record_scrape(None, 1.0, 2.0, 10, 2, ["boom"])
            return await self.store.scrapes(limit=5)

        rows = run(scenario())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seen"], 10)
        self.assertEqual(rows[0]["new"], 2)
        self.assertEqual(rows[0]["errors"], ["boom"])

    def test_counts(self):
        async def scenario():
            await self.store.open()
            await self.store.upsert_listing(source="kufar", external_id="1", watch_id=None,
                                            title="a", price=1.0, currency="BYN", region="",
                                            condition="", description="", images=[], url="")
            await self.store.upsert_listing(source="onliner", external_id="1", watch_id=None,
                                            title="b", price=2.0, currency="BYN", region="",
                                            condition="", description="", images=[], url="")
            await self.store.add_alert(1, "new", "x")
            return await self.store.counts()

        by_source, pending = run(scenario())
        self.assertEqual(by_source, {"kufar": 1, "onliner": 1})
        self.assertEqual(pending, 1)


if __name__ == "__main__":
    unittest.main()
