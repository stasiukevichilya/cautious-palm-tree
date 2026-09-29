import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from app import create_app
from chat import Reply, Service, User
from images import Images
from llm import LLM, Unavailable
from settings import Settings, mask_token
from store import NotFound, Store

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = Path(os.getenv("TGBOT_WORKFLOWS", ROOT.parent / "qwen-image" / "workflows"))
ADMIN, ALICE, BOB = User(1, "admin"), User(2, "alice", "Alice"), User(3, "bob", "Bob")
KEY = "k" * 32
TOKEN = "123456789:" + "A" * 35


class FakeLLM:
    def __init__(self):
        self.calls, self.gate, self.fail = [], None, None

    async def active(self, backends):
        if self.fail:
            raise Unavailable(self.fail)
        return "qwen", backends["qwen"]

    async def stream(self, backends, messages, max_tokens, temperature, timeout):
        await self.active(backends)
        self.calls.append(messages)
        yield "Ответ "
        if self.gate:
            await self.gate.wait()
        yield str(len(self.calls))


class FakeImages:
    async def active(self, backends):
        return "sdxl", backends["sdxl"]

    async def generate(self, backends, prompt, timeout):
        return "sdxl", b"\x89PNG", 7


class FakeOut:
    def __init__(self):
        self.messages, self.photos = [], []

    async def send(self, reply):
        self.messages.append(reply.text)
        return len(self.messages) - 1

    async def edit(self, handle, text, html=False):
        self.messages[handle] = text

    async def photo(self, png, caption):
        self.photos.append((png, caption))


class Async(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "bot.db")
        await self.store.open({"admins": [ADMIN.id]})
        self.llm = FakeLLM()
        self.service = Service(self.store, self.llm, FakeImages())
        self.notified = []

        async def notify(chat_id, reply):
            self.notified.append((chat_id, reply))
        self.service.notify = notify

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def allow(self, *users):
        for user in users:
            await self.store.register(user.id, user.username, user.full_name, "allowed")


class StoreTests(Async):
    async def test_sessions_are_isolated_per_user(self):
        await self.allow(ALICE, BOB)
        alice = await self.store.create_session(ALICE.id, "A")
        await self.store.add_message(ALICE.id, alice["id"], "user", "secret")
        for call in (self.store.session(BOB.id, alice["id"]), self.store.switch_session(BOB.id, alice["id"]),
                     self.store.delete_session(BOB.id, alice["id"]), self.store.clear_session(BOB.id, alice["id"]),
                     self.store.history(BOB.id, alice["id"], 10, 1000),
                     self.store.add_message(BOB.id, alice["id"], "user", "x")):
            with self.assertRaises(NotFound):
                await call
        self.assertEqual(await self.store.sessions(BOB.id), [])
        self.assertEqual(len(await self.store.history(ALICE.id, alice["id"], 10, 1000)), 1)

    async def test_delete_moves_active_session_and_cascades(self):
        await self.allow(ALICE)
        first = await self.store.create_session(ALICE.id)
        second = await self.store.create_session(ALICE.id)
        await self.store.add_message(ALICE.id, second["id"], "user", "hi")
        active = await self.store.delete_session(ALICE.id, second["id"])
        self.assertEqual(active["id"], first["id"])
        count, = (await self.store.db.execute_fetchall("SELECT COUNT(*) FROM messages"))[0]
        self.assertEqual(count, 0)
        self.assertIsNone(await self.store.delete_session(ALICE.id, first["id"]))

    async def test_history_limits_and_starts_with_user(self):
        await self.allow(ALICE)
        session = await self.store.create_session(ALICE.id)
        for index in range(6):
            await self.store.add_message(ALICE.id, session["id"], "user" if index % 2 == 0 else "assistant",
                                         f"{index}" * 10)
        history = await self.store.history(ALICE.id, session["id"], 3, 1000)
        self.assertEqual([m["content"][0] for m in history], ["4", "5"])
        history = await self.store.history(ALICE.id, session["id"], 10, 25)
        self.assertEqual([m["content"][0] for m in history], ["4", "5"])

    async def test_database_is_private(self):
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    async def test_disallowed_stored_backends_are_ignored(self):
        await self.store.update_settings({"image_backends": {"qwen-image": "http://host.docker.internal:8085",
                                                             "sdxl": "http://sdxl:8080"},
                                          "llm_backends": {"qwen": "http://172.18.0.2:8188"}})
        values = await self.store.settings()
        self.assertEqual(values["image_backends"], {"sdxl": "http://sdxl:8080"})
        self.assertEqual(values["llm_backends"], {})

    async def test_environment_only_seeds_empty_settings(self):
        await self.store.update_settings({"admins": [5]})
        await self.store.close()
        await self.store.open({"admins": [1], "bot_token": TOKEN})
        values = await self.store.settings()
        self.assertEqual(values["admins"], [5])
        self.assertEqual(values["bot_token"], TOKEN)


class ServiceTests(Async):
    async def test_access_flow(self):
        self.assertIsNone(await self.service.role(ALICE))
        reply = await self.service.start(ALICE)
        self.assertIn("Заявка", reply.text)
        self.assertEqual(await self.service.role(ALICE), "pending")
        (chat_id, request), = self.notified
        self.assertEqual(chat_id, ADMIN.id)
        self.assertEqual(request.buttons[0][0][1], "allow:2")
        await self.service.start(ALICE)
        self.assertEqual(len(self.notified), 1)
        denied = await self.service.callback(ALICE, "allow:2")
        self.assertEqual(denied.text, "Действие недоступно.")
        await self.service.callback(ADMIN, "allow:2")
        self.assertEqual(await self.service.role(ALICE), "allowed")
        self.assertEqual(self.notified[-1][0], ALICE.id)
        self.assertIn("Готово", (await self.service.start(ADMIN)).text)
        self.assertEqual(await self.service.role(ADMIN), "admin")

    async def test_chat_keeps_separate_histories(self):
        await self.allow(ALICE, BOB)
        out = FakeOut()
        await self.service.chat(ALICE, "привет", out)
        await self.service.chat(BOB, "hello", out)
        await self.service.chat(ALICE, "еще", out)
        self.assertEqual(out.messages, ["Ответ 1", "Ответ 2", "Ответ 3"])
        alice_last = self.llm.calls[2]
        self.assertEqual(alice_last[0]["role"], "system")
        self.assertEqual([m["content"] for m in alice_last[1:]], ["привет", "Ответ 1", "еще"])
        self.assertEqual([m["content"] for m in self.llm.calls[1][1:]], ["hello"])

    async def test_session_commands(self):
        await self.allow(ALICE)
        out = FakeOut()
        await self.service.chat(ALICE, "one", out)
        first = await self.store.active_session(ALICE.id)
        self.assertIn("Создана", (await self.service.new(ALICE, "Проект")).text)
        await self.service.chat(ALICE, "two", out)
        self.assertEqual([m["content"] for m in self.llm.calls[1][1:]], ["two"])
        listing = await self.service.sessions(ALICE)
        self.assertIn("▶", listing.text)
        self.assertEqual(len(listing.buttons), 2)
        await self.service.switch(ALICE, f"#{first['id']}")
        await self.service.system(ALICE, "Будь краток")
        await self.service.chat(ALICE, "three", out)
        self.assertEqual(self.llm.calls[2][0], {"role": "system", "content": "Будь краток"})
        self.assertEqual([m["content"] for m in self.llm.calls[2][1:]], ["one", "Ответ 1", "three"])
        await self.service.clear(ALICE)
        self.assertEqual((await self.store.active_session(ALICE.id))["messages"], 0)
        await self.service.rename(ALICE, "Новое имя")
        self.assertEqual((await self.store.active_session(ALICE.id))["title"], "Новое имя")
        self.assertIn("не найдена", (await self.service.switch(ALICE, "999")).text)
        self.assertIn("удалена", (await self.service.callback(ALICE, f"del:{first['id']}")).text)

    async def test_cancel_keeps_partial_answer(self):
        await self.allow(ALICE)
        self.llm.gate = asyncio.Event()
        out = FakeOut()
        task = asyncio.create_task(self.service.chat(ALICE, "long", out))
        while not self.llm.calls:
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)
        busy = FakeOut()
        await self.service.chat(ALICE, "again", busy)
        self.assertIn("/cancel", busy.messages[0])
        self.assertIsNone(await self.service.cancel(ALICE))
        await task
        self.assertEqual(out.messages[-1], "Запрос прерван.")
        session = await self.store.active_session(ALICE.id)
        history = await self.store.history(ALICE.id, session["id"], 10, 10000)
        self.assertEqual(history[-1]["content"], "Ответ \n[прервано]")
        self.assertIn("Нет", (await self.service.cancel(ALICE)).text)

    async def test_concurrent_messages_from_one_user(self):
        await self.allow(ALICE)
        self.llm.gate = asyncio.Event()
        outs = [FakeOut(), FakeOut()]
        tasks = [asyncio.create_task(self.service.chat(ALICE, text, out)) for text, out in zip("ab", outs)]
        while not self.llm.calls or not outs[1].messages:
            await asyncio.sleep(0)
        self.llm.gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("/cancel", outs[1].messages[0])

    async def test_queue_and_unavailable_llm(self):
        await self.allow(ALICE, BOB)
        self.llm.gate = asyncio.Event()
        first, second = FakeOut(), FakeOut()
        task = asyncio.create_task(self.service.chat(ALICE, "a", first))
        while not self.llm.calls:
            await asyncio.sleep(0)
        waiting = asyncio.create_task(self.service.chat(BOB, "b", second))
        while not second.messages:
            await asyncio.sleep(0)
        self.assertIn("В очереди: 1", second.messages[0])
        self.llm.gate.set()
        await asyncio.gather(task, waiting)
        self.assertEqual(second.messages[-1], "Ответ 2")
        self.llm.fail = "LLM не запущена"
        out = FakeOut()
        await self.service.chat(ALICE, "c", out)
        self.assertEqual(out.messages, ["⚠️ LLM не запущена"])

    async def test_rate_limit(self):
        await self.allow(ALICE)
        await self.store.update_settings({"rate_limit_per_min": 1})
        out = FakeOut()
        await self.service.chat(ALICE, "a", out)
        await self.service.chat(ALICE, "b", out)
        self.assertIn("Слишком много", out.messages[-1])

    async def test_image(self):
        await self.allow(ALICE)
        out = FakeOut()
        await self.service.image(ALICE, "cat", out)
        self.assertEqual(out.photos, [(b"\x89PNG", out.photos[0][1])])
        self.assertIn("seed 7", out.photos[0][1])
        await self.service.image(ALICE, "  ", out)
        self.assertIn("Использование", out.messages[-1])


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_picks_running_backend_and_streams(self):
        def handler(request):
            if request.url.host == "qwen":
                return httpx.Response(502)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            body = json.loads(request.content)
            self.assertTrue(body["stream"])
            events = [{"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {}}]},
                      {"choices": [{"delta": {"content": "lo"}}]}]
            text = "".join(f"data: {json.dumps(x)}\n\n" for x in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=text)

        llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        backends = {"qwen": "http://qwen:8080", "gemma": "http://gemma:8080"}
        self.assertEqual(await llm.active(backends), ("gemma", "http://gemma:8080"))
        parts = [x async for x in llm.stream(backends, [], 10, 0.5, 30)]
        self.assertEqual(parts, ["Hel", "lo"])

    async def test_llm_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("down")

        llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with self.assertRaisesRegex(Unavailable, "make qwen"):
            await llm.active({"qwen": "http://qwen:8080"})

    async def test_sdxl_flow_and_failure(self):
        state = {"polls": 0, "final": "completed"}

        def handler(request):
            path = request.url.path
            if path == "/health/ready":
                return httpx.Response(200, json={"status": "ready"})
            if path == "/api/generations":
                body = json.loads(request.content)
                self.assertEqual(body["prompt"], "a cat")
                return httpx.Response(202, json={"id": "j1"})
            if path == "/api/generations/j1":
                state["polls"] += 1
                status = "running" if state["polls"] < 2 else state["final"]
                return httpx.Response(200, json={"status": status, "error": "Pipeline failed"})
            if path == "/api/generations/j1/image":
                return httpx.Response(200, content=b"sdxl-png")
            return httpx.Response(404)

        images = Images(WORKFLOWS, httpx.AsyncClient(transport=httpx.MockTransport(handler)), poll_seconds=0)
        backends = {"sdxl": "http://sdxl:8080", "qwen-image": "http://qwen-image:8188"}
        name, png, _ = await images.generate(backends, "a cat", 60)
        self.assertEqual((name, png), ("sdxl", b"sdxl-png"))
        state.update(polls=0, final="interrupted")
        with self.assertRaisesRegex(Unavailable, "Pipeline failed"):
            await images.generate(backends, "a cat", 60)

    async def test_qwen_image_graph_and_flow(self):
        posted = {}

        def handler(request):
            path = request.url.path
            if request.url.host == "sdxl":
                raise httpx.ConnectError("down")
            if path == "/system_stats":
                return httpx.Response(200, json={})
            if path == "/local-qwen-image/config":
                return httpx.Response(200, json={"workflow": "qwen-image21-unsloth-q8"})
            if path == "/prompt":
                posted.update(json.loads(request.content)["prompt"])
                return httpx.Response(200, json={"prompt_id": "p1"})
            if path == "/history/p1":
                return httpx.Response(200, json={"p1": {"status": {"status_str": "success"}, "outputs": {
                    "8": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}}}})
            if path == "/view":
                self.assertEqual(request.url.params["filename"], "a.png")
                return httpx.Response(200, content=b"png")
            return httpx.Response(404)

        images = Images(WORKFLOWS, httpx.AsyncClient(transport=httpx.MockTransport(handler)), poll_seconds=0)
        name, png, seed = await images.generate(
            {"sdxl": "http://sdxl:8080", "qwen-image": "http://qwen-image:8188"}, "a cat", 60)
        self.assertEqual((name, png), ("qwen-image", b"png"))
        self.assertEqual(posted["4"]["inputs"]["prompt"], "a cat")
        self.assertEqual(posted["6"]["inputs"]["seed"], seed)
        self.assertEqual(posted["1"]["inputs"]["model_name"], "qwen-image-2.1-Q8_0.gguf")
        self.assertEqual(posted["5"]["inputs"]["device"], "gpu")


class FakeRunner:
    def __init__(self, service, metrics):
        self.tokens = []
        self.status = {"polling": False, "username": None, "error": None}

    async def check(self, token):
        if token.endswith("B"):
            raise RuntimeError("Unauthorized")
        return SimpleNamespace(username="local_bot")

    async def replace(self, token):
        self.tokens.append(token)
        self.status = {"polling": bool(token), "username": "local_bot" if token else None, "error": None}

    async def stop(self):
        pass


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        settings = Settings(db_path=Path(self.directory.name) / "bot.db", admin_key=KEY, initial_token=TOKEN,
                            initial_admins=(1,), workflows_path=WORKFLOWS)
        llm, images = FakeLLM(), FakeImages()
        self.app = create_app(settings, llm, images, runner_factory=FakeRunner)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.auth = {"Authorization": f"Bearer {KEY}"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def test_requires_admin_key(self):
        self.assertEqual(self.client.get("/health/ready").status_code, 200)
        self.assertEqual(self.client.get("/api/settings").status_code, 401)
        self.assertEqual(self.client.get("/api/settings", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(self.client.get("/", ).status_code, 200)

    def test_token_is_masked_and_replaced(self):
        self.assertEqual(self.app.state.runner.tokens, [TOKEN])
        values = self.client.get("/api/settings", headers=self.auth).json()
        self.assertEqual(values["bot_token"], mask_token(TOKEN))
        self.assertNotIn("A" * 20, json.dumps(values))
        self.assertEqual(values["admins"], [1])
        bad = self.client.put("/api/settings/token", headers=self.auth, json={"token": "123456789:" + "B" * 35})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.put("/api/settings/token", headers=self.auth, json={"token": "x"}).status_code, 422)
        new = "987654321:" + "C" * 35
        result = self.client.put("/api/settings/token", headers=self.auth, json={"token": new}).json()
        self.assertEqual(result["username"], "local_bot")
        self.assertEqual(self.app.state.runner.tokens[-1], new)
        self.client.delete("/api/settings/token", headers=self.auth)
        self.assertEqual(self.app.state.runner.tokens[-1], "")

    def test_settings_validation(self):
        response = self.client.put("/api/settings", headers=self.auth, json={
            "admins": [1, 2], "rate_limit_per_min": 5, "llm_backends": {"qwen": "http://qwen:8080/"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["llm_backends"], {"qwen": "http://qwen:8080"})
        for bad in ({"rate_limit_per_min": 0}, {"bot_token": TOKEN}, {"llm_backends": {"x": "not a url"}},
                    {"image_backends": {"other": "http://x"}},
                    {"image_backends": {"qwen-image-uc": "http://qwen-image-uc:8188"}},
                    {"image_backends": {"qwen-image": "http://qwen-image-uc:8188"}},
                    {"image_backends": {"qwen-image": "http://host.docker.internal:8085"}},
                    {"image_backends": {"sdxl": "http://172.18.0.2:8188"}},
                    {"llm_backends": {"qwen": "http://host.docker.internal:8085"}},
                    {"llm_backends": {"other": "http://other:8080"}}):
            self.assertEqual(self.client.put("/api/settings", headers=self.auth, json=bad).status_code, 422, bad)

    def test_users_and_status(self):
        store = self.app.state.store
        self.client.portal.call(store.register, 2, "alice", "Alice")
        users = self.client.get("/api/users", headers=self.auth).json()
        self.assertEqual([(u["tg_id"], u["status"], u["admin"]) for u in users], [(2, "pending", False)])
        allowed = self.client.post("/api/users/2/allow", headers=self.auth).json()
        self.assertEqual(allowed["status"], "allowed")
        self.assertEqual(self.client.post("/api/users/9/allow", headers=self.auth).status_code, 404)
        self.assertEqual(self.client.post("/api/users/2/delete", headers=self.auth).status_code, 422)
        status = self.client.get("/api/status", headers=self.auth).json()
        self.assertEqual(status["backends"]["llm"], "qwen")
        self.assertTrue(status["bot"]["polling"])
        self.assertIn(b"tgbot_requests_total", self.client.get("/metrics").content)


class SettingsTests(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(mask_token(TOKEN), "123456789:****AAAA")
        self.assertEqual(mask_token(""), "")

    def test_reply_defaults(self):
        self.assertEqual(Reply("x").buttons, [])


if __name__ == "__main__":
    unittest.main()
