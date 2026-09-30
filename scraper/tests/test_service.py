"""Offline tests for the scrape service: alerts, delivery, summary, busy state."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from adapters.base import Listing
from metrics import Metrics
from service import Busy, Service
from store import Store


class FakeClient:
    async def close(self):
        pass


class FakeTgbot:
    def __init__(self, fail=False):
        self.sent = []
        self.user_sent = {}
        self.fail = fail

    async def notify(self, text):
        if self.fail:
            raise ConnectionError("refused")
        self.sent.append(text)

    async def notify_user(self, tg_id, text):
        if self.fail:
            raise ConnectionError("refused")
        self.user_sent.setdefault(tg_id, []).append(text)

    async def close(self):
        pass


def item_html(price_byn):
    state = json.dumps({"props": {"initialState": {"adView": {"error": None, "data": {"initial": {
        "ad_id": 42, "subject": "RTX 3090 FE", "price_byn": price_byn, "price_usd": "0",
        "ad_link": "https://www.kufar.by/item/42",
        "ad_parameters": [{"p": "region", "vl": "Минск"}]}}}}}})
    return f'<html><script id="__NEXT_DATA__" type="application/json">{state}</script></html>'


class ItemClient:
    def __init__(self, price_byn="450000"):
        self.price_byn = price_byn
        self.gone = False

    async def get_text(self, url, tries=3):
        return "<html></html>" if self.gone else item_html(self.price_byn)

    async def close(self):
        pass


class FakeAdapter:
    source = "kufar"

    def __init__(self, listings):
        self.listings = listings

    async def fetch(self, watch):
        return self.listings


def make_listing(external_id, price, title="Видеокарта MSI RTX 3090"):
    return Listing(external_id=str(external_id), title=title, price=price, currency="BYN",
                   url=f"https://kufar.by/item/{external_id}", region="Минск")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "scraper.db")
        self.tgbot = FakeTgbot()
        self.service = Service(self.store, FakeClient(), self.tgbot, Metrics())
        self.service.adapters = {"kufar": FakeAdapter([]), "onliner": FakeAdapter([])}

    def tearDown(self):
        asyncio.run(self.store.close())
        self.tmp.cleanup()

    def seed_watch(self, **overrides):
        watch = {"source": "kufar", "ref": "/l/videokarty", "label": "K", "filter": ["3090"],
                 "params": {}, "active": True, "owner": None}
        watch.update(overrides)
        return watch

    def test_first_run_raises_new_alerts_and_delivers(self):
        self.service.adapters["kufar"] = FakeAdapter([make_listing(1, 5000.0)])

        async def scenario():
            await self.store.open()
            await self.store.seed_watches([self.seed_watch()])
            summary = await self.service.scrape()
            return summary, await self.store.pending_alerts()

        summary, pending = asyncio.run(scenario())
        self.assertEqual(summary["new"], 1)
        self.assertEqual(pending, [])
        self.assertEqual(len(self.tgbot.sent), 1)
        self.assertIn("Новое объявление", self.tgbot.sent[0])
        self.assertIn("5000", self.tgbot.sent[0])

    def test_price_drop_raises_alert(self):
        self.service.adapters["kufar"] = FakeAdapter([make_listing(1, 5000.0)])

        async def scenario():
            await self.store.open()
            await self.store.seed_watches([self.seed_watch()])
            await self.service.scrape()
            self.service.adapters["kufar"] = FakeAdapter([make_listing(1, 4500.0)])
            summary = await self.service.scrape()
            return summary

        summary = asyncio.run(scenario())
        self.assertEqual(summary["price_drops"], 1)
        self.assertEqual(len(self.tgbot.sent), 2)
        self.assertIn("Снижение цены", self.tgbot.sent[1])

    def test_delivery_failure_keeps_alerts_pending(self):
        self.tgbot = FakeTgbot(fail=True)
        self.service.tgbot = self.tgbot
        self.service.adapters["kufar"] = FakeAdapter([make_listing(1, 5000.0)])

        async def scenario():
            await self.store.open()
            await self.store.seed_watches([self.seed_watch()])
            await self.service.scrape()
            return await self.store.pending_alerts()

        pending = asyncio.run(scenario())
        self.assertEqual(len(pending), 1)

    def test_flood_is_summarized(self):
        flood = [make_listing(i, 1000.0 + i, title=f"RTX 3090 #{i}") for i in range(60)]
        self.service.adapters["kufar"] = FakeAdapter(flood)

        async def scenario():
            await self.store.open()
            await self.store.seed_watches([self.seed_watch()])
            summary = await self.service.scrape()
            alerts = await self.store.alerts(limit=100)
            return summary, alerts

        summary, alerts = asyncio.run(scenario())
        self.assertEqual(summary["new"], 60)
        self.assertEqual(len(alerts), 1)  # one summary alert instead of 60
        self.assertIn("+60 новых объявлений", self.tgbot.sent[0])

    def test_busy_rejects_concurrent_scrape(self):
        async def scenario():
            await self.store.open()
            self.service.running = True
            try:
                with self.assertRaises(Busy):
                    await self.service.scrape()
            finally:
                self.service.running = False

        asyncio.run(scenario())

    def test_report_lists_cheapest(self):
        self.service.adapters["kufar"] = FakeAdapter([make_listing(1, 5000.0),
                                                      make_listing(2, 4000.0, title="RTX 3090 Ti")])

        async def scenario():
            await self.store.open()
            await self.store.seed_watches([self.seed_watch()])
            await self.service.scrape()
            return await self.service.report()

        text = asyncio.run(scenario())
        self.assertIn("Отчёт", text)
        self.assertLess(text.index("4000"), text.index("5000"))  # cheapest first

    def test_report_bolds_below_weekly_median(self):
        listings = [make_listing(i, 3000.0 * i, title=f"RTX 3090 #{i}") for i in range(1, 5)]
        self.service.adapters["kufar"] = FakeAdapter(listings)

        async def scenario():
            await self.store.open()
            await self.store.seed_watches([self.seed_watch()])
            await self.service.scrape()
            await self.service.scrape()  # second pass: dense price points, no new alerts
            return await self.service.report()

        text = asyncio.run(scenario())
        self.assertIn("медиана за 7 дней: 7500 BYN", text)  # median of 3000, 6000, 9000, 12000
        self.assertIn("<b>3000 BYN — RTX 3090 #1", text)
        self.assertIn("<b>6000 BYN — RTX 3090 #2", text)
        self.assertNotIn("<b>9000", text)
        self.assertNotIn("<b>12000", text)
        self.assertEqual(len(self.tgbot.sent), 2)  # new-ads alert + the report

    def test_report_without_watches_returns_none(self):
        async def scenario():
            await self.store.open()
            return await self.service.report()

        self.assertIsNone(asyncio.run(scenario()))

    def test_user_watch_alerts_go_to_owner_not_admins(self):
        self.service.adapters["kufar"] = FakeAdapter([make_listing(1, 5000.0)])

        async def scenario():
            await self.store.open()
            await self.store.seed_watches([self.seed_watch(owner=7)])
            await self.service.scrape()
            return await self.store.pending_alerts()

        pending = asyncio.run(scenario())
        self.assertEqual(pending, [])
        self.assertEqual(self.tgbot.sent, [])
        self.assertEqual(len(self.tgbot.user_sent[7]), 1)
        self.assertIn("Новое объявление (K)", self.tgbot.user_sent[7][0])

    def test_item_price_changes_notify_owner_both_ways(self):
        client = ItemClient("450000")
        self.service.client = client

        async def scenario():
            await self.store.open()
            await self.store.add_item(7, "https://www.kufar.by/item/42")
            await self.service.scrape()  # first check: baseline, no alert
            client.price_byn = "400000"
            await self.service.scrape()  # drop
            client.price_byn = "500000"
            await self.service.scrape()  # rise
            return await self.store.items(7), await self.store.pending_alerts()

        items, pending = asyncio.run(scenario())
        self.assertEqual(items[0]["price"], 5000.0)
        self.assertEqual(pending, [])
        self.assertEqual(self.tgbot.sent, [])
        self.assertEqual(len(self.tgbot.user_sent[7]), 2)
        self.assertIn("Снижение цены", self.tgbot.user_sent[7][0])
        self.assertIn("Повышение цены", self.tgbot.user_sent[7][1])

    def test_deleted_item_is_deactivated(self):
        client = ItemClient("450000")
        client.gone = True
        self.service.client = client

        async def scenario():
            await self.store.open()
            await self.store.add_item(7, "https://www.kufar.by/item/42")
            await self.service.scrape()
            return await self.store.items(7), await self.store.active_items()

        items, active = asyncio.run(scenario())
        self.assertFalse(items[0]["active"])
        self.assertEqual(active, [])

    def test_report_skips_user_watches(self):
        self.service.adapters["kufar"] = FakeAdapter([make_listing(1, 5000.0)])

        async def scenario():
            await self.store.open()
            await self.store.seed_watches([self.seed_watch(owner=7),
                                           self.seed_watch(ref="/l/shared", label="S")])
            await self.service.scrape()
            return await self.service.report()

        text = asyncio.run(scenario())
        self.assertIn("\nS:", text)
        self.assertNotIn("\nK:", text)


if __name__ == "__main__":
    unittest.main()
