"""Offline tests for the Onliner adapter (no network): fixtures inline."""
import asyncio
import json
import unittest

from adapters.onliner import OnlinerAdapter
from config import Watch

OFFER_GPU = {
    "id": 734778,
    "user": {"type": "user", "id": 1},
    "product": {"full_name": "MSI GeForce RTX 3090 Suprim X 24GB GDDR6X", "icon": "i.jpg"},
    "location": {"address_string": "Минск"},
    "condition": 9,
    "price": {"amount": "4599.00", "currency": "BYN"},
    "delivery": {"available_for_country": True},
    "photos": [{"640x640": "https://img/1.jpg", "1280x1280": "https://img/2.jpg"}],
    "description": "24 ГБ GDDR6X",
    "created_at": "2026-09-22T03:50:19+03:00",
    "updated_at": "2026-09-29T03:50:19+03:00",
    "html_url": "https://catalog.onliner.by/videocard/msi/rtx3090suprimx/used/734778",
}
OFFER_OTHER = {**OFFER_GPU, "id": 734779,
               "product": {"full_name": "Palit GeForce RTX 4060", "icon": "i.jpg"},
               "html_url": "https://catalog.onliner.by/videocard/palit/x/used/734779"}


class FakeClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    async def get_text(self, url, tries=3):
        self.calls.append(url)
        return json.dumps(self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)])


class OnlinerTests(unittest.TestCase):
    def make_watch(self):
        return Watch(source="onliner", ref="videocard", filter=["3090", "4090", "5090"])

    def test_fetch_filters(self):
        client = FakeClient([{"offers": [OFFER_GPU, OFFER_OTHER], "total": 2,
                              "page": {"limit": 30, "items": 2, "current": 1, "last": 1}}])
        adapter = OnlinerAdapter(client)

        def run():
            return asyncio.run(adapter.fetch(self.make_watch()))

        listings = run()
        self.assertEqual(len(listings), 1)
        listing = listings[0]
        self.assertEqual(listing.external_id, "734778")
        self.assertEqual(listing.price, 4599.0)
        self.assertEqual(listing.currency, "BYN")
        self.assertEqual(listing.region, "Минск")
        self.assertEqual(listing.condition, "9")
        self.assertEqual(listing.images, ["https://img/1.jpg", "https://img/2.jpg"])
        self.assertEqual(client.calls, ["https://catalog.api.onliner.by/search/videocard/second-offers"])

    def test_fetch_follows_pages(self):
        page1 = {"offers": [OFFER_GPU] + [dict(OFFER_OTHER, id=i) for i in range(29)],
                 "total": 60, "page": {"limit": 30, "items": 30, "current": 1, "last": 2}}
        page2 = {"offers": [dict(OFFER_GPU, id=999)],
                 "total": 60, "page": {"limit": 30, "items": 1, "current": 2, "last": 2}}
        client = FakeClient([page1, page2])
        adapter = OnlinerAdapter(client)

        def run():
            return asyncio.run(adapter.fetch(self.make_watch()))

        listings = run()
        self.assertEqual(len(listings), 2)
        self.assertEqual(client.calls[1], "https://catalog.api.onliner.by/search/videocard/second-offers?page=2")

    def test_empty_page_stops(self):
        client = FakeClient([{"offers": [], "total": 0, "page": {"limit": 30, "items": 0, "current": 1, "last": 1}}])
        adapter = OnlinerAdapter(client)

        def run():
            return asyncio.run(adapter.fetch(self.make_watch()))

        self.assertEqual(run(), [])
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
