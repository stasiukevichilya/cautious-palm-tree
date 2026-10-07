import base64
import json
import os
import unittest

from fastapi.testclient import TestClient

os.environ.setdefault("IMAGES_API_KEY", "k" * 32)
os.environ.setdefault("IMAGES_BACKENDS", '{"sdxl": "http://sdxl:8080", "qwen-image": "http://qwen-image:8188"}')

from app import Settings, create_app  # noqa: E402
from llm import Unavailable  # noqa: E402

AUTH = {"Authorization": "Bearer " + "k" * 32}
MCP_HEADERS = {**AUTH, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class FakeImages:
    def __init__(self):
        self.calls, self.fail = [], None

    async def generate(self, backends, prompt, timeout):
        self.calls.append((dict(backends), prompt))
        if self.fail:
            raise Unavailable(self.fail)
        return next(iter(backends)), b"\x89PNG", 7


def rpc(client, method, params, ident=1):
    response = client.post("/mcp", headers=MCP_HEADERS,
                           json={"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
    assert response.status_code == 200, response.text
    lines = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
    return json.loads(lines[-1]) if lines else response.json()


class ImagesTest(unittest.TestCase):
    def setUp(self):
        self.images = FakeImages()
        self.client = TestClient(create_app(Settings(), self.images))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_auth_required(self):
        self.assertEqual(self.client.get("/v1/models").status_code, 401)
        self.assertEqual(self.client.post("/mcp", json={}).status_code, 401)
        self.assertEqual(self.client.get("/health/ready").status_code, 200)

    def test_models(self):
        ids = [m["id"] for m in self.client.get("/v1/models", headers=AUTH).json()["data"]]
        self.assertEqual(ids, ["auto", "sdxl", "qwen-image"])

    def test_openai_generation(self):
        response = self.client.post("/v1/images/generations", headers=AUTH,
                                    json={"prompt": "a cat", "model": "qwen-image", "size": "512x512"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(base64.b64decode(response.json()["data"][0]["b64_json"]), b"\x89PNG")
        self.assertEqual(self.images.calls, [({"qwen-image": "http://qwen-image:8188"}, "a cat")])

    def test_openai_errors(self):
        unknown = self.client.post("/v1/images/generations", headers=AUTH, json={"prompt": "x", "model": "dalle"})
        self.assertEqual(unknown.status_code, 503)
        self.images.fail = "Генератор изображений не запущен"
        down = self.client.post("/v1/images/generations", headers=AUTH, json={"prompt": "x"})
        self.assertEqual((down.status_code, down.json()["detail"]), (503, "Генератор изображений не запущен"))

    def test_mcp_tool(self):
        init = rpc(self.client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "images")
        tools = rpc(self.client, "tools/list", {}, 2)["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["generate_image"])
        result = rpc(self.client, "tools/call", {"name": "generate_image", "arguments": {"prompt": "a dog"}}, 3)
        content = result["result"]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual((content[0]["mimeType"], base64.b64decode(content[0]["data"])), ("image/png", b"\x89PNG"))
        self.assertIn("seed 7", content[1]["text"])
        self.assertFalse(result["result"].get("isError"))

    def test_mcp_tool_error(self):
        self.images.fail = "Генератор изображений не запущен"
        result = rpc(self.client, "tools/call", {"name": "generate_image", "arguments": {"prompt": "x"}})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("не запущен", result["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
