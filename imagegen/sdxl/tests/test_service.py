import json
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image

from app import create_app
from backend import Cancelled, assert_cuda_components
from settings import Settings


class FakeBackend:
    info = {"mode": "single"}

    def __init__(self):
        self.release = threading.Event()
        self.fail = False

    def load(self):
        pass

    def generate(self, parameters, cancel, progress):
        for step in range(1, 4):
            while not self.release.wait(.01):
                if cancel.is_set():
                    raise Cancelled()
            if cancel.is_set():
                raise Cancelled()
            progress(step)
        if self.fail:
            self.fail = False
            raise MemoryError()
        return Image.new("RGB", (1024, 1024), "green"), {"seconds": .1}

    def recover(self):
        pass

    def is_oom(self, error):
        return isinstance(error, MemoryError)


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Condition timed out")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(output_path=Path(self.temp.name), queue_size=1, max_results=10)
        self.backend = FakeBackend()
        self.app = create_app(self.settings, self.backend, exit_on_failure=False)
        self.client = TestClient(self.app).__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        wait_for(lambda: self.app.state.jobs.ready)

    def submit(self):
        response = self.client.post("/api/generations", json={"prompt": "Test cup"})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["id"]

    def status(self, identifier):
        return self.client.get(f"/api/generations/{identifier}").json()["status"]

    def test_validation(self):
        for body in ({"prompt": " "}, {"prompt": "a", "width": 4096},
                     {"prompt": "a", "steps": 0}, {"prompt": "a", "seed": -1},
                     {"prompt": "a", "batch": 2}, {"prompt": "a", "steps": "30"}):
            self.assertEqual(self.client.post("/api/generations", json=body).status_code, 422)
        self.assertEqual(self.client.get("/api/generations/not-a-uuid").status_code, 422)

    def test_queue_and_queued_cancel(self):
        first = self.submit()
        wait_for(lambda: self.status(first) == "running")
        second = self.submit()
        self.assertEqual(self.client.post("/api/generations", json={"prompt":"extra"}).status_code, 429)
        self.client.delete(f"/api/generations/{second}")
        self.assertEqual(self.status(second), "cancelled")
        third = self.submit()
        self.backend.release.set()
        wait_for(lambda: self.status(third) == "completed")
        self.assertEqual(self.status(first), "completed")

    def test_active_cancel_and_next_request(self):
        first = self.submit()
        wait_for(lambda: self.status(first) == "running")
        self.client.delete(f"/api/generations/{first}")
        wait_for(lambda: self.status(first) == "cancelled")
        wait_for(lambda: self.app.state.jobs.ready)
        self.backend.release.set()
        second = self.submit()
        wait_for(lambda: self.status(second) == "completed")

    def test_image_metadata_metrics_and_restart(self):
        self.backend.release.set()
        identifier = self.submit()
        wait_for(lambda: self.status(identifier) == "completed")
        result = self.client.get(f"/api/generations/{identifier}/image")
        self.assertEqual(result.headers["content-type"], "image/png")
        self.assertTrue(result.content.startswith(b"\x89PNG"))
        self.assertIn("sdxl_images_total 1.0", self.client.get("/metrics").text)
        self.assertNotIn("Test cup", self.client.get("/metrics").text)
        self.app.state.jobs.stop()
        app = create_app(self.settings, FakeBackend(), exit_on_failure=False)
        with TestClient(app) as client:
            self.assertEqual(client.get(f"/api/generations/{identifier}").json()["status"], "completed")

    def test_restart_marks_unfinished(self):
        identifier = str(uuid.uuid4())
        (self.settings.output_path / f"{identifier}.json").write_text(json.dumps({
            "id":identifier,"status":"running","created_at":time.time(),"parameters":{}}))
        restored = create_app(self.settings, FakeBackend(), exit_on_failure=False).state.jobs
        self.assertEqual(restored.get(identifier)["status"], "interrupted")

    def test_oom_recovery(self):
        self.backend.fail = True
        self.backend.release.set()
        first = self.submit()
        wait_for(lambda: self.status(first) == "failed")
        wait_for(lambda: self.app.state.jobs.ready)
        second = self.submit()
        wait_for(lambda: self.status(second) == "completed")
        self.assertIn("sdxl_oom_total 1.0", self.client.get("/metrics").text)

    def test_unready_and_not_found(self):
        self.app.state.jobs.ready = False
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        self.assertEqual(self.client.get("/health/ready").status_code, 503)
        self.assertEqual(self.client.post("/api/generations", json={"prompt":"a"}).status_code, 503)
        self.assertEqual(self.client.get(f"/api/generations/{uuid.uuid4()}").status_code, 404)

    def test_unrecoverable_failure_rejects_new_jobs(self):
        first = self.submit()
        wait_for(lambda: self.status(first) == "running")
        second = self.submit()
        self.backend.fail = True

        def broken_recovery():
            raise RuntimeError("GPU unavailable")

        self.backend.recover = broken_recovery
        self.backend.release.set()
        wait_for(lambda: self.app.state.jobs.error is not None)
        self.assertEqual(self.status(first), "failed")
        self.assertEqual(self.status(second), "failed")
        self.assertEqual(self.client.get("/health/ready").status_code, 503)

    def test_retention_removes_old_images(self):
        self.backend.release.set()
        first = self.submit()
        wait_for(lambda: self.status(first) == "completed")
        second = self.submit()
        wait_for(lambda: self.status(second) == "completed")
        self.app.state.jobs.settings = replace(self.settings, max_results=1)
        history = self.client.get("/api/generations").json()
        self.assertEqual([r["id"] for r in history], [second])
        self.assertFalse((self.settings.output_path / f"{first}.png").exists())
        self.assertEqual(self.client.get(f"/api/generations/{first}").status_code, 404)

    def test_cuda_guard(self):
        class Module:
            def parameters(self):
                return [SimpleNamespace(device="cpu")]
            def buffers(self):
                return []
        with self.assertRaises(RuntimeError):
            assert_cuda_components(SimpleNamespace(unet=Module()))


if __name__ == "__main__":
    unittest.main()
