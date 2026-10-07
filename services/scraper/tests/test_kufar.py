"""Offline tests for the Kufar adapter (no network): fixtures inline."""
import asyncio
import json
import unittest

from adapters.kufar import KufarAdapter, card_price, item_url, parse_cards, parse_item, parse_next_data
from adapters.base import Listing
from config import Watch

AD_3090 = {
    "ad_id": 1016660668,
    "subject": "Видеокарта MSI RTX 3090 Suprim X 24GB",
    "ad_link": "https://www.kufar.by/item/1016660668",
    "price_byn": "459900", "price_usd": "148000", "currency": "BYN",
    "images": [{"path": "adim1/abc.jpg"}],
    "ad_parameters": [
        {"pl": "Регион", "vl": "Минск", "p": "region", "v": 7, "pu": "rgn"},
        {"pl": "Город / Район", "vl": "Минск", "p": "area", "v": "1", "pu": "ar"},
    ],
}
AD_OTHER = {
    "ad_id": 1016660669,
    "subject": "Корпус для компьютера",
    "ad_link": "https://www.kufar.by/item/1016660669",
    "price_byn": "0", "price_usd": "0", "currency": "BYN",
    "images": [], "ad_parameters": [],
}


def page_html(ads, total):
    state = json.dumps({"props": {"initialState": {"listing": {"ads": ads, "total": str(total)}}}})
    return f'<html><head><script id="__NEXT_DATA__" type="application/json">{state}</script></head><body></body></html>'


class FakeClient:
    def __init__(self, pages):
        self.pages = pages  # list of html strings, one per call
        self.calls = 0
        self.urls = []

    async def get_text(self, url, tries=3):
        self.urls.append(url)
        page = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return page


class KufarParseTests(unittest.TestCase):
    def test_next_data_extraction(self):
        listing = parse_next_data(page_html([AD_3090], 1))
        self.assertEqual(listing["total"], "1")
        self.assertIsNone(parse_next_data("<html></html>"))

    def test_ad_parsing(self):
        adapter = KufarAdapter(client=None)
        parsed = adapter._parse_ad(AD_3090)
        self.assertEqual(parsed.external_id, "1016660668")
        self.assertEqual(parsed.price, 4599.0)  # 459900 kopecks -> 4599.0 rubles
        self.assertEqual(parsed.currency, "BYN")
        self.assertEqual(parsed.region, "Минск")  # duplicate region/area collapsed
        self.assertEqual(parsed.images, ["https://rms.kufar.by/v1/list_thumbs_2x/adim1/abc.jpg"])

    def test_negotiable_price(self):
        adapter = KufarAdapter(client=None)
        parsed = adapter._parse_ad(AD_OTHER)
        self.assertIsNone(parsed.price)
        self.assertEqual(parsed.currency, "")

    def test_card_fallback(self):
        html = """
        <section><a class="w" data-testid="kufar-ad" href="https://kufar.by/item/42?block=1" target="_blank">
          <img src="x.jpg"/>
          <h2 class="w">Видеокарта RTX 4090</h2>
          <div class="w"><span data-testid="card-price">12 000 р.</span></div>
          <div class="styles-x__info__region">Гомель, Гомельская обл.</div>
        </section>
        <section><a data-testid="kufar-ad" href="https://kufar.by/item/43">
          <h2>Прочий товар</h2>
          <span data-testid="card-price">Договорная</span>
        </section>
        """
        cards = parse_cards(html)
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0].external_id, "42")
        self.assertEqual(cards[0].title, "Видеокарта RTX 4090")
        self.assertEqual(cards[0].price, 12000.0)
        self.assertEqual(cards[0].currency, "BYN")
        self.assertEqual(cards[0].region, "Гомель, Гомельская обл.")
        self.assertEqual(cards[1].price, None)

    def test_card_price(self):
        self.assertEqual(card_price("4 500 р."), 4500.0)
        self.assertIsNone(card_price("Договорная"))
        self.assertIsNone(card_price(""))

    def test_item_url(self):
        self.assertEqual(item_url("https://www.kufar.by/item/1085074728"), "https://www.kufar.by/item/1085074728")
        self.assertEqual(item_url("https://kufar.by/item/42"), "https://www.kufar.by/item/42")
        self.assertIsNone(item_url("https://www.kufar.by/l?query=rtx+5090"))
        self.assertIsNone(item_url("https://www.onliner.by/ru/goods/1"))
        self.assertIsNone(item_url(""))

    def test_parse_item(self):
        def page(view):
            state = json.dumps({"props": {"initialState": {"adView": view}}})
            return f'<html><script id="__NEXT_DATA__" type="application/json">{state}</script></html>'

        initial = {"ad_id": 42, "subject": "RTX 3090 FE", "price_byn": "450000", "price_usd": "0",
                   "ad_link": "https://www.kufar.by/item/42",
                   "ad_parameters": [{"p": "region", "vl": "Минск"}, {"p": "area", "vl": "Минск"}]}
        parsed = parse_item(page({"error": None, "data": {"initial": initial}}))
        self.assertEqual(parsed.external_id, "42")
        self.assertEqual(parsed.price, 4500.0)  # kopecks -> rubles
        self.assertEqual(parsed.currency, "BYN")
        self.assertEqual(parsed.region, "Минск")
        self.assertIsNone(parse_item(page({"error": {"code": 1}, "data": None})))
        self.assertIsNone(parse_item(page({"error": None, "data": {}})))
        self.assertIsNone(parse_item("<html></html>"))

    def test_exclude_drops_ads(self):
        watch = Watch(source="kufar", ref="/l?query=rtx+3090&sort=lst.d", filter=["3090"], exclude=["ноутбук"])
        self.assertTrue(watch.matches("Видеокарта RTX 3090"))
        self.assertFalse(watch.matches("Ноутбук с RTX 3090"))


class KufarFetchTests(unittest.TestCase):
    def make_watch(self):
        return Watch(source="kufar", ref="/l/videokarty", filter=["3090"],
                     params={"max_pages": 40})

    def test_fetch_filters_and_stops_on_total(self):
        client = FakeClient([page_html([AD_3090, AD_OTHER], 2)])
        adapter = KufarAdapter(client)

        def run():
            return asyncio.run(adapter.fetch(self.make_watch()))

        listings = run()
        self.assertEqual(len(listings), 1)
        self.assertIn("3090", listings[0].title)
        self.assertEqual(client.calls, 1)  # total=2 fits in one page

    def test_fetch_follows_pages(self):
        first = page_html([AD_3090] + [dict(AD_OTHER, ad_id=i, subject=f"Прочее {i}") for i in range(29)], 60)
        second = page_html([dict(AD_3090, ad_id=999, subject="RTX 3090 Ti")], 60)
        client = FakeClient([first, second])
        adapter = KufarAdapter(client)

        def run():
            return asyncio.run(adapter.fetch(self.make_watch()))

        listings = run()
        self.assertEqual(len(listings), 2)  # two 3090s, 30 fillers filtered out
        self.assertEqual(client.calls, 2)

    def make_search_watch(self):
        return Watch(source="kufar", ref="/l?query=rtx+3090&sort=lst.d", filter=["3090"],
                     exclude=["ноутбук"], params={"max_pages": 5})

    def test_fetch_search_follows_cursor_tokens(self):
        def search_page(ads, token):
            pagination = [{"label": "next", "num": 2, "token": token}] if token else []
            listing = {"ads": ads, "total": "99", "pagination": pagination}
            state = json.dumps({"props": {"initialState": {"listing": listing}}})
            return f'<html><script id="__NEXT_DATA__" type="application/json">{state}</script></html>'

        laptop = dict(AD_OTHER, ad_id=500, subject="Ноутбук с RTX 3090")
        client = FakeClient([
            search_page([AD_3090, laptop], "tok1"),
            search_page([dict(AD_3090, ad_id=999, subject="RTX 3090 Ti")], "tok2"),
            search_page([dict(AD_3090, ad_id=998, subject="RTX 3090 FE")], None),
        ])
        adapter = KufarAdapter(client)

        def run():
            return asyncio.run(adapter.fetch(self.make_search_watch()))

        listings = run()
        self.assertEqual([l.external_id for l in listings], ["1016660668", "999", "998"])
        self.assertEqual(client.calls, 3)
        self.assertIn("cursor=tok1", client.urls[1])
        self.assertIn("cursor=tok2", client.urls[2])

    def test_card_fallback_used_when_next_data_missing(self):
        html = ('<html><body><section><a data-testid="kufar-ad" href="https://kufar.by/item/7">'
                '<h2>RTX 3090</h2><span data-testid="card-price">4500 р.</span>'
                '<div class="a__info__region">Минск</div></section></body></html>')
        client = FakeClient([html])
        adapter = KufarAdapter(client)
        watch = Watch(source="kufar", ref="/l/videokarty", filter=["3090"])

        def run():
            return asyncio.run(adapter.fetch(watch))

        listings = run()
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].external_id, "7")
        self.assertEqual(listings[0].title, "RTX 3090")
        self.assertEqual(listings[0].price, 4500.0)
        self.assertIsInstance(listings[0], Listing)


if __name__ == "__main__":
    unittest.main()
