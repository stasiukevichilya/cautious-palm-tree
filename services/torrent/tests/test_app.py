"""Offline tests for the HTTP API: health, metrics, auth and error paths."""
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from app import create_app
from settings import Settings

KEY = "test-key-12345678"
HEADERS = {"Authorization": f"Bearer {KEY}"}
BAD_MAGNET = "http://example.com/a.torrent"


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            admin_key=KEY,
            destination=Path(self.tmp.name) / "downloads",
            tgbot_url="",
            metadata_timeout=1,
        )
        self.app = create_app(self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_and_metrics(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/health/live").json(), {"status": "alive"})
            ready = client.get("/health/ready").json()
            self.assertEqual(ready["status"], "ready")
            self.assertEqual(ready["torrents"], 0)
            metrics = client.get("/metrics")
            self.assertEqual(metrics.status_code, 200)
            self.assertIn("torrent_downloaded_bytes_total", metrics.text)
            self.assertIn("torrent_destination_free_bytes", metrics.text)

    def test_auth(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/api/torrents").status_code, 401)
            self.assertEqual(client.get("/api/torrents",
                                        headers={"Authorization": "Bearer wrong"}).status_code, 401)
            self.assertEqual(client.get("/api/torrents", headers=HEADERS).json(), [])

    def test_metadata_rejects_non_magnet(self):
        with TestClient(self.app) as client:
            response = client.post("/api/metadata", headers=HEADERS, json={"magnet": BAD_MAGNET})
            self.assertEqual(response.status_code, 400)
            response = client.post("/api/torrents", headers=HEADERS, json={"magnet": BAD_MAGNET})
            self.assertEqual(response.status_code, 400)

    def test_magnet_validation(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.post("/api/metadata", headers=HEADERS,
                                         json={"magnet": "short"}).status_code, 422)
            self.assertEqual(client.post("/api/metadata", headers=HEADERS,
                                         json={}).status_code, 422)

    def test_remove_unknown(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.delete("/api/torrents/" + "0" * 40,
                                           headers=HEADERS).status_code, 404)

    def test_pause_resume_unknown(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.post("/api/torrents/" + "0" * 40 + "/pause",
                                         headers=HEADERS).status_code, 404)
            self.assertEqual(client.post("/api/torrents/" + "0" * 40 + "/resume",
                                         headers=HEADERS).status_code, 404)
            # the key is required
            self.assertEqual(client.post("/api/torrents/" + "0" * 40 + "/pause").status_code, 401)


if __name__ == "__main__":
    unittest.main()
