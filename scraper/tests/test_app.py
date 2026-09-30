"""Offline tests for the HTTP API: health, auth, watch CRUD, manual scrape and report."""
import json
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from adapters.base import Listing
from app import create_app
from settings import Settings

KEY = "test-key-12345678"
HEADERS = {"Authorization": f"Bearer {KEY}"}


class FakeAdapter:
    source = "kufar"

    def __init__(self):
        self.calls = 0

    async def fetch(self, watch):
        self.calls += 1
        return [Listing(external_id="1", title="Видеокарта MSI RTX 3090", price=5000.0,
                        currency="BYN", url="https://kufar.by/item/1", region="Минск")]


class ItemClient:
    def __init__(self):
        self.price_byn = "450000"

    async def get_text(self, url, tries=3):
        state = json.dumps({"props": {"initialState": {"adView": {"error": None, "data": {"initial": {
            "ad_id": 42, "subject": "RTX 3090 FE", "price_byn": self.price_byn, "price_usd": "0",
            "ad_link": "https://www.kufar.by/item/42",
            "ad_parameters": [{"p": "region", "vl": "Минск"}]}}}}}})
        return f'<html><script id="__NEXT_DATA__" type="application/json">{state}</script></html>'

    async def close(self):
        pass


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        seed = Path(self.tmp.name) / "watch.json"
        seed.write_text(json.dumps([
            {"source": "kufar", "ref": "/l/videokarty", "label": "K", "filter": ["3090"]},
        ]), encoding="utf-8")
        self.settings = Settings(
            db_path=Path(self.tmp.name) / "scraper.db",
            admin_key=KEY,
            tgbot_url="http://127.0.0.1:1",
            interval=900,
            delay=0,
            seed_path=seed,
            start_scheduler=False,
        )
        self.app = create_app(self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def wire_fakes(self):
        service = self.app.state.service
        service.adapters = {"kufar": FakeAdapter(), "onliner": FakeAdapter()}
        sent = []
        service.tgbot.notify = lambda text: _record(sent, text)
        return service, sent

    def test_health_and_auth(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/health/live").json(), {"status": "alive"})
            self.assertEqual(client.get("/health/ready").json()["watches"], 1)
            self.assertEqual(client.get("/api/watches").status_code, 401)
            self.assertEqual(
                client.get("/api/watches", headers={"Authorization": "Bearer wrong"}).status_code, 401)
            watches = client.get("/api/watches", headers=HEADERS).json()
            self.assertEqual(len(watches), 1)
            self.assertEqual(watches[0]["source"], "kufar")

    def test_watch_crud(self):
        with TestClient(self.app) as client:
            created = client.post("/api/watches", headers=HEADERS, json={
                "source": "onliner", "ref": "videocard", "label": "O", "filter": ["4090"]})
            self.assertEqual(created.status_code, 201)
            watch_id = created.json()["id"]
            self.assertTrue(client.post(f"/api/watches/{watch_id}/disable", headers=HEADERS).json()["ok"])
            watches = client.get("/api/watches", headers=HEADERS).json()
            self.assertFalse([w for w in watches if w["id"] == watch_id][0]["active"])
            self.assertTrue(client.delete(f"/api/watches/{watch_id}", headers=HEADERS).json()["ok"])
            self.assertEqual(len(client.get("/api/watches", headers=HEADERS).json()), 1)
            self.assertEqual(client.delete(f"/api/watches/{watch_id}", headers=HEADERS).status_code, 404)
            self.assertEqual(client.post("/api/watches", headers=HEADERS,
                                         json={"source": "nope", "ref": "x"}).status_code, 422)

    def test_manual_scrape_and_report(self):
        service, sent = self.wire_fakes()
        with TestClient(self.app) as client:
            summary = client.post("/api/scrape", headers=HEADERS).json()
            self.assertEqual(summary["watched"], 1)
            self.assertEqual(summary["new"], 1)
            self.assertEqual(client.get("/api/listings", headers=HEADERS).json()[0]["title"],
                             "Видеокарта MSI RTX 3090")
            alerts = client.get("/api/alerts", headers=HEADERS).json()
            self.assertEqual(alerts[0]["kind"], "new")
            self.assertTrue(alerts[0]["delivered"])
            status = client.get("/api/status", headers=HEADERS).json()
            self.assertEqual(status["pending_alerts"], 0)
            self.assertEqual(status["listings"].get("kufar"), 1)
            self.assertTrue(client.post("/api/report", headers=HEADERS).json()["ok"])
            self.assertEqual(len(sent), 2)  # alert message + report
            self.assertTrue(client.get("/api/scrapes", headers=HEADERS).json())

    def test_busy_returns_409(self):
        service, _ = self.wire_fakes()
        with TestClient(self.app) as client:
            service.running = True
            self.assertEqual(client.post("/api/scrape", headers=HEADERS).status_code, 409)

    def test_subscribe_and_owner_scoping(self):
        with TestClient(self.app) as client:
            created = client.post("/api/watches", headers=HEADERS, json={
                "source": "onliner", "ref": "videocard", "label": "O", "owner": 7})
            self.assertEqual(created.status_code, 201)
            watch_id = created.json()["id"]
            self.assertTrue(client.post(f"/api/watches/{watch_id}/subscribe",
                                        headers=HEADERS, json={"tg_id": 8}).json()["ok"])
            mine = client.get("/api/watches", headers=HEADERS, params={"owner": 8}).json()
            self.assertEqual(len(mine), 1)
            self.assertTrue(mine[0]["subscribed"])
            self.assertTrue(client.delete(f"/api/watches/{watch_id}/subscribe",
                                          headers=HEADERS, params={"tg_id": 8}).json()["ok"])
            self.assertEqual(client.get("/api/watches", headers=HEADERS, params={"owner": 8}).json(), [])
            self.assertEqual(client.get("/api/watches", headers=HEADERS, params={"owner": 7}).json()[0]["owner"], 7)

    def test_items_api(self):
        service, _ = self.wire_fakes()
        service.client = ItemClient()
        with TestClient(self.app) as client:
            self.assertEqual(client.post("/api/items", headers=HEADERS,
                                         json={"owner": 7, "url": "https://example.com/x"}).status_code, 400)
            created = client.post("/api/items", headers=HEADERS,
                                  json={"owner": 7, "url": "https://www.kufar.by/item/42"})
            self.assertEqual(created.status_code, 201)
            item = created.json()
            self.assertEqual(item["price"], 4500.0)
            self.assertEqual(item["title"], "RTX 3090 FE")
            self.assertEqual(client.get("/api/items", headers=HEADERS, params={"owner": 7}).json()[0]["id"], item["id"])
            self.assertEqual(client.get("/api/items", headers=HEADERS, params={"owner": 8}).json(), [])
            service.client.price_byn = "400000"
            self.assertEqual(client.post(f"/api/items/{item['id']}/check", headers=HEADERS).json()["price"], 4000.0)
            self.assertEqual(client.delete(f"/api/items/{item['id']}", headers=HEADERS,
                                           params={"owner": 8}).status_code, 404)
            self.assertTrue(client.delete(f"/api/items/{item['id']}", headers=HEADERS,
                                          params={"owner": 7}).json()["ok"])


async def _record(sent, text):
    sent.append(text)


if __name__ == "__main__":
    unittest.main()
