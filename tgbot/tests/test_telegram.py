"""End-to-end through aiogram polling against a fake Telegram Bot API server."""
import asyncio
import itertools
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web

from bot import Runner
from chat import Metrics, Service
from llm import LLM
from store import Store
from test_bot import FakeImages

TOKEN = "123456789:" + "A" * 35
ADMIN = {"id": 1, "is_bot": False, "first_name": "Admin", "username": "admin"}
ALICE = {"id": 2, "is_bot": False, "first_name": "Alice", "username": "alice"}
BOB = {"id": 3, "is_bot": False, "first_name": "Bob"}


class FakeTelegram:
    def __init__(self):
        self.updates, self.calls = [], []
        self.update_ids, self.message_ids = itertools.count(1), itertools.count(100)

    def message(self, user, text, chat_type="private"):
        chat = {"id": user["id"] if chat_type == "private" else -100, "type": chat_type}
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text[0] == "/" else []
        self.updates.append({"update_id": next(self.update_ids), "message": {
            "message_id": next(self.message_ids), "date": 0, "chat": chat, "from": user, "text": text,
            "entities": entities}})

    def press(self, user, data):
        self.updates.append({"update_id": next(self.update_ids), "callback_query": {
            "id": str(next(self.update_ids)), "from": user, "chat_instance": "x", "data": data,
            "message": {"message_id": next(self.message_ids), "date": 0,
                        "chat": {"id": user["id"], "type": "private"}, "text": "menu"}}})

    def sent(self, chat_id, method=None):
        return [c for c in self.calls if str(c.get("chat_id")) == str(chat_id)
                and (method is None or c["method"] == method)]

    async def handle(self, request):
        token, method = request.match_info["token"], request.match_info["method"]
        assert token == TOKEN, token
        data = dict(await request.post())
        if method == "getMe":
            return self.ok({"id": 123456789, "is_bot": True, "first_name": "Local", "username": "local_bot"})
        if method == "getUpdates":
            offset = int(data.get("offset", 0))
            for _ in range(20):
                pending = [u for u in self.updates if u["update_id"] >= offset]
                if pending:
                    return self.ok(pending)
                await asyncio.sleep(0.02)
            return self.ok([])
        call = {"method": method, **{k: v for k, v in data.items() if isinstance(v, str)}}
        if call.get("parse_mode") == "HTML" and "REJECT" in call.get("text", ""):
            self.calls.append({**call, "rejected": True})
            return web.json_response({"ok": False, "error_code": 400,
                                      "description": "Bad Request: can't parse entities: test"}, status=400)
        if "reply_markup" in call:
            call["reply_markup"] = json.loads(call["reply_markup"])
        self.calls.append(call)
        if method in ("sendMessage", "sendPhoto", "editMessageText"):
            message = {"message_id": int(call.get("message_id") or next(self.message_ids)), "date": 0,
                       "chat": {"id": int(call["chat_id"]), "type": "private"}}
            if method == "sendPhoto":
                message["photo"] = [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}]
            else:
                message["text"] = call["text"]
            return self.ok(message)
        return self.ok(True)

    @staticmethod
    def ok(result):
        return web.json_response({"ok": True, "result": result})


def openai_stream(request):
    if request.url.path == "/health":
        return httpx.Response(200)
    body = json.loads(request.content)["messages"][-1]["content"]
    pieces = ("REJECT **me**",) if body == "reject" else ("**Hello**", ", ", "world")
    events = [{"choices": [{"delta": {"content": x}}]} for x in pieces]
    return httpx.Response(200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n")


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.telegram = FakeTelegram()
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self.telegram.handle)
        self.web = web.AppRunner(app)
        await self.web.setup()
        site = web.TCPSite(self.web, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "bot.db")
        await self.store.open({"admins": [ADMIN["id"]]})
        llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(openai_stream)))
        metrics = Metrics()
        self.runner = Runner(Service(self.store, llm, FakeImages(), metrics), metrics,
                             api=TelegramAPIServer.from_base(f"http://127.0.0.1:{port}"))
        await self.runner.replace(TOKEN)

    async def asyncTearDown(self):
        await self.runner.stop()
        await self.store.close()
        await self.web.cleanup()
        self.directory.cleanup()

    async def until(self, predicate):
        for _ in range(250):
            if result := predicate():
                return result
            await asyncio.sleep(0.02)
        self.fail(f"Timed out; calls: {self.telegram.calls}")

    async def test_full_flow(self):
        telegram = self.telegram
        self.assertEqual(self.runner.status, {"polling": True, "username": "local_bot", "error": None})
        self.assertTrue(any(c["method"] == "setMyCommands" for c in telegram.calls))

        telegram.message(BOB, "hello")
        await self.until(lambda: telegram.sent(3))
        self.assertIn("/start", telegram.sent(3)[0]["text"])

        telegram.message(ALICE, "/start")
        request = await self.until(lambda: telegram.sent(1, "sendMessage"))
        self.assertIn("@alice", request[0]["text"])
        buttons = [b["callback_data"] for b in request[0]["reply_markup"]["inline_keyboard"][0]]
        self.assertEqual(buttons, ["allow:2", "block:2"])
        await self.until(lambda: telegram.sent(2))
        self.assertIn("Заявка", telegram.sent(2)[0]["text"])

        telegram.message(ALICE, "до одобрения")
        await self.until(lambda: len(telegram.sent(2)) == 2)
        self.assertIn("ожидает", telegram.sent(2)[1]["text"])

        telegram.press(ADMIN, "allow:2")
        await self.until(lambda: any("Доступ открыт" in c.get("text", "") for c in telegram.sent(2)))

        telegram.message(ALICE, "привет")
        edit = await self.until(lambda: telegram.sent(2, "editMessageText"))
        self.assertEqual((edit[-1]["text"], edit[-1]["parse_mode"]), ("<b>Hello</b>, world", "HTML"))
        history = await self.store.history(2, (await self.store.active_session(2))["id"], 10, 1000)
        self.assertEqual([m["content"] for m in history], ["привет", "**Hello**, world"])

        telegram.message(ALICE, "reject")
        fallback = await self.until(lambda: [c for c in telegram.sent(2, "editMessageText")
                                             if "REJECT" in c["text"] and "parse_mode" not in c])
        self.assertEqual(fallback[-1]["text"], "REJECT me")
        self.assertTrue(any(c.get("rejected") for c in telegram.calls))

        telegram.message(ALICE, "/new Проект")
        await self.until(lambda: any("Проект" in c.get("text", "") for c in telegram.sent(2)))
        telegram.message(ALICE, "/sessions")
        listing = await self.until(lambda: [c for c in telegram.sent(2) if "reply_markup" in c])
        self.assertEqual(len(listing[-1]["reply_markup"]["inline_keyboard"]), 2)

        telegram.message(ALICE, "/image a cat")
        photo = await self.until(lambda: telegram.sent(2, "sendPhoto"))
        self.assertIn("seed 7", photo[0]["caption"])

        telegram.message(ALICE, "/users")
        await self.until(lambda: any("администратору" in c.get("text", "") for c in telegram.sent(2)))

        before = len(telegram.calls)
        telegram.message(ALICE, "/sessions", chat_type="group")
        telegram.message(ALICE, "/help")
        await self.until(lambda: any("/cancel" in c.get("text", "") for c in telegram.calls[before:]))
        self.assertFalse([c for c in telegram.calls[before:] if str(c.get("chat_id")) == "-100"])

    async def test_token_replacement_stops_polling(self):
        await self.runner.replace("")
        self.assertFalse(self.runner.status["polling"])
        self.telegram.message(ALICE, "/start")
        await asyncio.sleep(0.3)
        self.assertEqual(self.telegram.sent(2), [])
        await self.runner.replace(TOKEN)
        await self.until(lambda: self.telegram.sent(2))


if __name__ == "__main__":
    unittest.main()
