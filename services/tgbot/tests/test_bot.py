import asyncio
import itertools
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
from llm import LLM, ToolCalls, Unavailable
from market import Market
from settings import Settings, mask_token
from store import NotFound, Store
from torrents import NotFound as TorrentNotFound, Timeout, TorrentError, magnet_hash

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = Path(os.getenv("TGBOT_WORKFLOWS") or ROOT.parent.parent / "imagegen" / "qwen-image" / "workflows")
ADMIN, ALICE, BOB = User(1, "admin"), User(2, "alice", "Alice"), User(3, "bob", "Bob")
KEY = "k" * 32
TOKEN = "123456789:" + "A" * 35


class FakeLLM:
    def __init__(self):
        self.calls, self.gate, self.fail = [], None, None
        self.script, self.tools = [], []  # script: per-call lists of deltas (str or ToolCalls)

    async def active(self, backends):
        if self.fail:
            raise Unavailable(self.fail)
        return "qwen", backends["qwen"]

    async def stream(self, backends, messages, max_tokens, temperature, timeout, tools=None):
        await self.active(backends)
        self.calls.append([dict(m) for m in messages])
        self.tools.append(tools)
        if self.script:
            for delta in self.script.pop(0):
                yield delta
            return
        yield "Ответ "
        if self.gate:
            await self.gate.wait()
        yield str(len(self.calls))


class FakeImages:
    def __init__(self):
        self.prompts, self.fail = [], None

    async def active(self, backends):
        if self.fail:
            raise Unavailable(self.fail)
        return "sdxl", backends["sdxl"]

    async def generate(self, backends, prompt, timeout):
        self.prompts.append(prompt)
        return "sdxl", b"\x89PNG", 7


def draw(prompt, ident="c1"):
    return ToolCalls([{"id": ident, "name": "generate_image", "arguments": json.dumps({"prompt": prompt})}])


class FakeOut:
    def __init__(self):
        self.messages, self.photos, self.buttons = [], [], {}

    async def send(self, reply):
        self.messages.append(reply.text)
        if reply.buttons:
            self.buttons[len(self.messages) - 1] = reply.buttons
        return len(self.messages) - 1

    async def edit(self, handle, text, html=False, buttons=None):
        self.messages[handle] = text
        self.buttons[handle] = buttons

    async def photo(self, png, caption):
        self.photos.append((png, caption))


class FakeTorrents:
    def __init__(self):
        self.metadata_calls, self.start_calls, self.remove_calls = [], [], []
        self.pause_calls, self.resume_calls = [], []
        self.meta = {}
        self.rows = []
        self.fail = None        # exception raised by metadata() and start()
        self.remove_fail = None  # exception raised by remove()
        self.pause_fail = None   # exception raised by pause()
        self.resume_fail = None  # exception raised by resume()

    async def metadata(self, magnet):
        self.metadata_calls.append(magnet)
        if self.fail:
            raise self.fail
        return self.meta

    async def start(self, magnet):
        self.start_calls.append(magnet)
        if self.fail:
            raise self.fail
        return {"info_hash": magnet_hash(magnet), "state": "downloading"}

    async def list(self):
        return self.rows

    async def remove(self, key):
        self.remove_calls.append(key)
        if self.remove_fail:
            raise self.remove_fail
        return {"ok": True}

    def _row(self, key):
        row = next((r for r in self.rows if r["info_hash"] == key), None)
        return {"info_hash": key, "name": row["name"] if row else ""}

    async def pause(self, key):
        self.pause_calls.append(key)
        if self.pause_fail:
            raise self.pause_fail
        return self._row(key)

    async def resume(self, key):
        self.resume_calls.append(key)
        if self.resume_fail:
            raise self.resume_fail
        return self._row(key)


class FakeMarket:
    def __init__(self):
        self.watches, self.items = {}, {}
        self.created, self.deleted_watches, self.subscribed = [], [], []
        self.deleted_items, self.checked = [], []
        self.next_id = itertools.count(1)
        self.fail = None

    async def find_watch(self, source, ref):
        for watch in self.watches.values():
            if watch["source"] == source and watch["ref"] == ref:
                return watch
        return None

    async def create_watch(self, watch):
        if self.fail:
            raise self.fail
        watch = dict(watch, id=next(self.next_id), active=1, listings=0)
        self.watches[watch["id"]] = watch
        self.created.append(watch)
        return watch

    async def delete_watch(self, watch_id):
        if self.fail:
            raise self.fail
        self.watches.pop(watch_id, None)
        self.deleted_watches.append(watch_id)

    async def subscribe(self, watch_id, tg_id):
        self.subscribed.append((watch_id, tg_id))

    async def unsubscribe(self, watch_id, tg_id):
        pass

    async def my_watches(self, owner):
        return [w for w in self.watches.values() if w.get("owner") == owner]

    async def add_item(self, owner, url):
        if self.fail:
            raise self.fail
        item = {"id": next(self.next_id), "owner": owner, "url": url, "title": "RTX 5090",
                "price": 30000.0, "currency": "BYN", "active": True}
        self.items[item["id"]] = item
        return item

    async def my_items(self, owner):
        if self.fail:
            raise self.fail
        return [i for i in self.items.values() if i["owner"] == owner]

    async def delete_item(self, item_id, owner):
        if item_id not in self.items:
            raise httpx.HTTPStatusError("gone", request=httpx.Request("DELETE", "http://x"),
                                        response=httpx.Response(404))
        self.items.pop(item_id)
        self.deleted_items.append(item_id)

    async def check_item(self, item_id):
        self.checked.append(item_id)
        return {"price": 29000.0}


class Async(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "bot.db")
        await self.store.open({"admins": [ADMIN.id]})
        self.llm = FakeLLM()
        self.torrents = FakeTorrents()
        self.images = FakeImages()
        self.service = Service(self.store, self.llm, self.images, torrents=self.torrents)
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


class ImageToolTests(Async):
    async def test_llm_draws_and_answers(self):
        await self.allow(ALICE)
        self.llm.script = [["Сейчас нарисую.", draw("a red cat")], ["Вот кот."]]
        out = FakeOut()
        await self.service.chat(ALICE, "нарисуй кота", out)
        self.assertEqual(self.images.prompts, ["a red cat"])
        self.assertEqual(out.photos[0][0], b"\x89PNG")
        self.assertEqual(out.messages[-1], "Сейчас нарисую.Вот кот.")
        self.assertEqual(self.llm.tools[0][0]["function"]["name"], "generate_image")
        second = self.llm.calls[1]
        self.assertEqual(second[-2]["tool_calls"][0]["function"]["name"], "generate_image")
        self.assertEqual(second[-2]["content"], "Сейчас нарисую.")
        self.assertEqual((second[-1]["role"], second[-1]["tool_call_id"]), ("tool", "c1"))
        self.assertIn("already sent", second[-1]["content"])
        session = await self.store.active_session(ALICE.id)
        history = await self.store.history(ALICE.id, session["id"], 10, 10000)
        self.assertEqual(history[-1]["content"], "Сейчас нарисую.Вот кот.\n[Отправлено изображение: a red cat]")

    async def test_generator_down_is_reported_to_model(self):
        await self.allow(ALICE)
        self.images.fail = "Генератор изображений не запущен"
        self.llm.script = [[draw("a cat")], ["Генератор сейчас выключен."]]
        out = FakeOut()
        await self.service.chat(ALICE, "нарисуй", out)
        self.assertEqual(out.photos, [])
        self.assertIn("не запущен", self.llm.calls[1][-1]["content"])
        self.assertEqual(out.messages[-1], "Генератор сейчас выключен.")

    async def test_rounds_are_bounded_and_bad_calls_rejected(self):
        await self.allow(ALICE)
        bad = ToolCalls([{"id": "x", "name": "rm_rf", "arguments": "{}"},
                         {"id": "y", "name": "generate_image", "arguments": "not json"}])
        self.llm.script = [[bad], [draw("1")], [draw("2")], ["конец"]]
        out = FakeOut()
        await self.service.chat(ALICE, "рисуй", out)
        self.assertIn("unknown tool", self.llm.calls[1][-2]["content"])
        self.assertIn("prompt argument is required", self.llm.calls[1][-1]["content"])
        self.assertEqual(self.images.prompts, ["1", "2"])
        self.assertIsNone(self.llm.tools[-1])  # the last round cannot call tools again
        self.assertEqual(out.messages[-1], "конец")

    async def test_image_only_answer_and_setting_off(self):
        await self.allow(ALICE)
        self.llm.script = [[draw("a dog")], [""]]
        out = FakeOut()
        await self.service.chat(ALICE, "собаку", out)
        self.assertEqual(out.messages[-1], "Готово.")
        await self.store.update_settings({"image_tool": False})
        await self.service.chat(ALICE, "ещё", out)
        self.assertIsNone(self.llm.tools[-1])


class MagnetTests(Async):
    MAGNET = "magnet:?xt=urn:btih:" + "a" * 40

    def configure(self, name="linux-2024.iso", size=5 * 1024 ** 3):
        self.torrents.meta = {"name": name, "size": size, "num_files": 1, "files": [[name, size]]}

    async def token_of(self, out):
        return out.buttons[0][0][0][1].partition(":")[2]

    async def test_magnet_not_configured(self):
        await self.allow(ADMIN)
        self.service.torrents = None
        out = FakeOut()
        await self.service.magnet(ADMIN, self.MAGNET, out)
        self.assertIn("не настроен", out.messages[-1])

    async def test_magnet_rejects_non_magnet(self):
        await self.allow(ADMIN)
        out = FakeOut()
        await self.service.magnet(ADMIN, "http://x/y.torrent", out)
        self.assertIn("не magnet-ссылка", out.messages[-1])
        self.assertEqual(self.torrents.metadata_calls, [])

    async def test_magnet_shows_metadata_and_confirms(self):
        await self.allow(ADMIN)
        self.configure()
        out = FakeOut()
        await self.service.magnet(ADMIN, self.MAGNET, out)
        self.assertEqual(len(out.messages), 1)  # the placeholder was edited in place
        self.assertIn("linux-2024.iso", out.messages[0])
        self.assertIn("5.0 ГБ", out.messages[0])
        self.assertIn("Скачать?", out.messages[0])
        label, data = out.buttons[0][0][0]
        self.assertEqual(label, "Скачать")
        reply = await self.service.callback(ADMIN, data)
        self.assertIn("запущено", reply.text)
        self.assertEqual(self.torrents.start_calls, [self.MAGNET])
        self.assertEqual(self.service.pending_magnets, {})

    async def test_magnet_confirm_by_non_admin_denied(self):
        await self.allow(ADMIN, ALICE)
        self.configure()
        out = FakeOut()
        await self.service.magnet(ADMIN, self.MAGNET, out)
        token = await self.token_of(out)
        reply = await self.service.callback(ALICE, f"magnet-yes:{token}")
        self.assertEqual(reply.text, "Действие недоступно.")
        self.assertEqual(self.torrents.start_calls, [])

    async def test_magnet_cancel(self):
        await self.allow(ADMIN)
        self.configure()
        out = FakeOut()
        await self.service.magnet(ADMIN, self.MAGNET, out)
        token = await self.token_of(out)
        reply = await self.service.callback(ADMIN, f"magnet-no:{token}")
        self.assertIn("Отменено", reply.text)
        self.assertEqual(self.service.pending_magnets, {})
        self.assertEqual(self.torrents.start_calls, [])

    async def test_magnet_stale_token(self):
        await self.allow(ADMIN)
        self.configure()
        reply = await self.service.callback(ADMIN, "magnet-yes:deadbeef")
        self.assertIn("устарел", reply.text)

    async def test_magnet_metadata_timeout(self):
        await self.allow(ADMIN)
        self.torrents.fail = Timeout("Не удалось получить метаданные (нет сидов).")
        out = FakeOut()
        await self.service.magnet(ADMIN, self.MAGNET, out)
        self.assertIn("⚠️", out.messages[0])
        self.assertIn("метаданные", out.messages[0])

    async def test_magnet_service_down(self):
        await self.allow(ADMIN)

        async def broken(magnet):
            raise httpx.ConnectError("down")
        self.torrents.metadata = broken
        out = FakeOut()
        await self.service.magnet(ADMIN, self.MAGNET, out)
        self.assertIn("недоступен", out.messages[0])

    async def test_magnet_duplicate_pending(self):
        await self.allow(ADMIN)
        self.configure()
        first, second = FakeOut(), FakeOut()
        await self.service.magnet(ADMIN, self.MAGNET, first)
        await self.service.magnet(ADMIN, self.MAGNET, second)
        self.assertIn("уже ожидает", second.messages[0])
        self.assertEqual(len(self.service.pending_magnets), 1)

    async def test_torrent_list(self):
        await self.allow(ADMIN)
        self.torrents.rows = [
            {"info_hash": "b" * 40, "name": "a.iso", "state": "downloading", "progress": 0.5,
             "download_speed": 1.2 * 1024 ** 2, "size": 10},
            {"info_hash": "c" * 40, "name": "b.zip", "state": "seeding", "progress": 1.0,
             "upload_speed": 300 * 1024, "size": 10},
            {"info_hash": "d" * 40, "name": "c.iso", "state": "paused", "progress": 0.25, "size": 10},
        ]
        reply = await self.service.torrent_list(ADMIN)
        self.assertIn("⬇", reply.text)
        self.assertIn("50%", reply.text)
        self.assertIn("1.2 МБ/с", reply.text)
        self.assertIn("↥", reply.text)
        self.assertIn("300.0 КБ/с", reply.text)
        self.assertIn("⏸", reply.text)
        self.assertIn("b" * 40, reply.text)  # the full hash is copyable for /torrent-del
        self.assertIn("<code>" + "b" * 40 + "</code>", reply.text)  # hash is a copyable code entity
        self.assertTrue(reply.html)

    async def test_torrent_list_empty_and_down(self):
        await self.allow(ADMIN)
        self.assertIn("Загрузок нет", (await self.service.torrent_list(ADMIN)).text)
        self.service.torrents = None
        self.assertIn("не настроен", (await self.service.torrent_list(ADMIN)).text)

    async def test_torrent_delete(self):
        await self.allow(ADMIN)
        reply = await self.service.torrent_delete(ADMIN, "b" * 40)
        self.assertIn("убрана", reply.text)
        self.assertEqual(self.torrents.remove_calls, ["b" * 40])
        self.assertIn("hash", (await self.service.torrent_delete(ADMIN, "nope")).text)
        self.torrents.remove_fail = TorrentNotFound("Загрузка не найдена.")
        self.assertIn("не найдена", (await self.service.torrent_delete(ADMIN, "c" * 40)).text)

    async def test_torrent_hash_prefix(self):
        await self.allow(ADMIN)
        first = "aa" + "1" * 38
        second = "aa" + "1" * 30 + "2" * 8
        self.torrents.rows = [
            {"info_hash": first, "name": "one", "state": "downloading", "progress": 0.1},
            {"info_hash": second, "name": "two", "state": "downloading", "progress": 0.2},
        ]
        # a shared prefix is ambiguous
        self.assertIn("неоднозначно", (await self.service.torrent_delete(ADMIN, "aa" + "1" * 10)).text)
        self.assertEqual(self.torrents.remove_calls, [])
        # a unique prefix resolves to the full hash
        self.torrents.rows = [self.torrents.rows[0]]
        reply = await self.service.torrent_delete(ADMIN, "aa" + "1" * 5)
        self.assertIn("убрана", reply.text)
        self.assertEqual(self.torrents.remove_calls, [first])
        # an unknown prefix is a lookup miss, not a service error
        self.assertIn("не найдена", (await self.service.torrent_delete(ADMIN, "f" * 8)).text)

    async def test_torrent_pause_resume(self):
        await self.allow(ADMIN)
        self.torrents.rows = [
            {"info_hash": "b" * 40, "name": "a.iso", "state": "downloading", "progress": 0.5},
        ]
        reply = await self.service.torrent_pause(ADMIN, "b" * 40)
        self.assertIn("на паузе", reply.text)
        self.assertIn("/torrent-resume " + "b" * 12, reply.text)
        self.assertEqual(self.torrents.pause_calls, ["b" * 40])
        reply = await self.service.torrent_resume(ADMIN, "b" * 40)
        self.assertIn("продолжается", reply.text)
        self.assertEqual(self.torrents.resume_calls, ["b" * 40])
        # a wrong state comes back as a service error text
        self.torrents.pause_fail = TorrentError("Загрузка не активна (состояние: paused).")
        self.assertIn("не активна", (await self.service.torrent_pause(ADMIN, "b" * 40)).text)
        self.torrents.resume_fail = TorrentError("Загрузка не на паузе (состояние: downloading).")
        self.assertIn("не на паузе", (await self.service.torrent_resume(ADMIN, "b" * 40)).text)


class MarketClientTests(Async):
    async def test_find_watch_is_a_point_lookup(self):
        seen = []

        async def handler(request):
            seen.append(request.url.path)
            if request.url.path == "/api/watches/find":
                if request.url.params["ref"] == "/l?query=rtx+5090&sort=lst.d":
                    return httpx.Response(200, json={"id": 7, "source": "kufar",
                                                     "ref": "/l?query=rtx+5090&sort=lst.d"})
                return httpx.Response(404)
            raise AssertionError("the full watch list must not be fetched: " + str(request.url))

        market = Market("http://scraper.test", KEY,
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            found = await market.find_watch("kufar", "/l?query=rtx+5090&sort=lst.d")
            self.assertEqual(found["id"], 7)
            self.assertIsNone(await market.find_watch("kufar", "/l?query=none&sort=lst.d"))
            self.assertEqual(seen, ["/api/watches/find", "/api/watches/find"])
        finally:
            await market.close()

    async def test_find_watch_keeps_other_status_errors(self):
        async def handler(request):
            return httpx.Response(502, text="upstream failure")

        market = Market("http://scraper.test", KEY,
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            with self.assertRaises(httpx.HTTPStatusError):
                await market.find_watch("kufar", "/l?query=x&sort=lst.d")
        finally:
            await market.close()


class MarketTests(Async):
    async def test_market_not_configured(self):
        await self.allow(ALICE)
        self.service.market = None
        calls = (lambda: self.service.market_query(ALICE, "rtx 5090"),
                 lambda: self.service.market_queries(ALICE),
                 lambda: self.service.market_delquery(ALICE, "1"),
                 lambda: self.service.market_watch(ALICE, "https://www.kufar.by/item/1"),
                 lambda: self.service.market_items(ALICE),
                 lambda: self.service.market_unitem(ALICE, "1"))
        for call in calls:
            self.assertIn("не настроен", (await call()).text)

    async def test_query_creates_watch_for_owner(self):
        await self.allow(ALICE)
        self.service.market = FakeMarket()
        reply = await self.service.market_query(ALICE, "rtx 5090")
        self.assertIn("Слежу", reply.text)
        watch = self.service.market.created[0]
        self.assertEqual(watch["source"], "kufar")
        self.assertEqual(watch["ref"], "/l?query=rtx+5090&sort=lst.d")
        self.assertEqual(watch["owner"], ALICE.id)
        self.assertEqual(watch["filter"], ["rtx 5090"])
        self.assertIn("ноутбук", watch["exclude"])

    async def test_query_rejects_empty_and_long(self):
        await self.allow(ALICE)
        self.service.market = FakeMarket()
        self.assertIn("Запрос от 1 до 60", (await self.service.market_query(ALICE, "")).text)
        self.assertIn("Запрос от 1 до 60", (await self.service.market_query(ALICE, "x" * 61)).text)
        self.assertEqual(self.service.market.created, [])

    async def test_query_existing_watch_subscribes(self):
        await self.allow(ALICE)
        market = FakeMarket()
        market.watches[7] = {"id": 7, "source": "kufar", "ref": "/l?query=rtx+5090&sort=lst.d",
                             "label": "Kufar: rtx 5090", "owner": BOB.id, "active": 1, "listings": 3}
        self.service.market = market
        reply = await self.service.market_query(ALICE, "rtx 5090")
        self.assertIn("подписал вас", reply.text)
        self.assertEqual(market.subscribed, [(7, ALICE.id)])
        self.assertEqual(market.created, [])

    async def test_query_own_duplicate_is_rejected(self):
        await self.allow(ALICE)
        market = FakeMarket()
        market.watches[7] = {"id": 7, "source": "kufar", "ref": "/l?query=rtx+5090&sort=lst.d",
                             "label": "Kufar: rtx 5090", "owner": ALICE.id, "active": 1, "listings": 0}
        self.service.market = market
        self.assertIn("уже следите", (await self.service.market_query(ALICE, "rtx 5090")).text)
        self.assertEqual(market.created, [])

    async def test_queries_lists_only_own_watches(self):
        await self.allow(ALICE)
        market = FakeMarket()
        market.watches = {1: {"id": 1, "label": "Kufar: rtx 5090", "owner": ALICE.id, "active": 1, "listings": 4},
                          2: {"id": 2, "label": "Kufar: rtx 4090", "owner": BOB.id, "active": 0, "listings": 0}}
        self.service.market = market
        text = (await self.service.market_queries(ALICE)).text
        self.assertIn("Kufar: rtx 5090", text)
        self.assertNotIn("Kufar: rtx 4090", text)
        bob_text = (await self.service.market_queries(BOB)).text
        self.assertIn("Kufar: rtx 4090", bob_text)
        self.assertNotIn("Kufar: rtx 5090", bob_text)
        self.assertEqual((await self.service.market_queries(User(9))).text,
                         "Запросов нет. Добавьте: /query rtx 5090")

    async def test_delquery(self):
        await self.allow(ALICE)
        market = FakeMarket()
        market.watches[1] = {"id": 1, "label": "Kufar: rtx 5090", "owner": ALICE.id, "active": 1, "listings": 0}
        self.service.market = market
        self.assertIn("удалён", (await self.service.market_delquery(ALICE, "1")).text)
        self.assertEqual(market.deleted_watches, [1])
        self.assertIn("не найден", (await self.service.market_delquery(ALICE, "1")).text)
        self.assertIn("Укажите id", (await self.service.market_delquery(ALICE, "x")).text)
        # bob cannot delete alice's watch
        self.assertIn("не найден", (await self.service.market_delquery(BOB, "1")).text)

    async def test_watch_adds_item_and_validates_url(self):
        await self.allow(ALICE)
        self.service.market = FakeMarket()
        for url in ("https://example.com/item/1", "https://www.kufar.by/l?query=rtx", "", "not a url"):
            self.assertIn("Нужна ссылка", (await self.service.market_watch(ALICE, url)).text)
        reply = await self.service.market_watch(ALICE, "https://www.kufar.by/item/1085074728")
        self.assertIn("Слежу за ценой", reply.text)
        self.assertIn("30000", reply.text)
        item = list(self.service.market.items.values())[0]
        self.assertEqual((item["owner"], item["url"]), (ALICE.id, "https://www.kufar.by/item/1085074728"))

    async def test_items_and_unitem(self):
        await self.allow(ALICE)
        self.service.market = FakeMarket()
        await self.service.market_watch(ALICE, "https://www.kufar.by/item/1")
        item = list(self.service.market.items.values())[0]
        text = (await self.service.market_items(ALICE)).text
        self.assertIn("RTX 5090", text)
        self.assertIn(item["url"], text)
        self.assertIn("убран", (await self.service.market_unitem(ALICE, str(item["id"]))).text)
        self.assertEqual(self.service.market.deleted_items, [item["id"]])
        self.assertIn("не найден", (await self.service.market_unitem(ALICE, str(item["id"]))).text)
        self.assertIn("Укажите id", (await self.service.market_unitem(ALICE, "x")).text)

    async def test_market_down(self):
        await self.allow(ALICE)
        market = FakeMarket()
        market.fail = httpx.ConnectError("down")
        self.service.market = market
        self.assertIn("недоступен", (await self.service.market_query(ALICE, "rtx")).text)
        self.assertIn("недоступен", (await self.service.market_items(ALICE)).text)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_uses_running_backend_and_streams(self):
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            body = json.loads(request.content)
            self.assertTrue(body["stream"])
            events = [{"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {}}]},
                      {"choices": [{"delta": {"content": "lo"}}]}]
            text = "".join(f"data: {json.dumps(x)}\n\n" for x in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=text)

        llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        backends = {"qwen": "http://qwen:8080"}
        self.assertEqual(await llm.active(backends), ("qwen", "http://qwen:8080"))
        parts = [x async for x in llm.stream(backends, [], 10, 0.5, 30)]
        self.assertEqual(parts, ["Hel", "lo"])

    async def test_llm_streams_tool_calls(self):
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            self.assertEqual(json.loads(request.content)["tools"], [{"type": "function"}])
            events = [{"choices": [{"delta": {"content": "Ok"}}]},
                      {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {
                          "name": "generate_image", "arguments": '{"prom'}}]}}]},
                      {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'pt": "x"}'}}]}}]}]
            text = "".join(f"data: {json.dumps(x)}\n\n" for x in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=text)

        llm = LLM(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        parts = [x async for x in llm.stream({"qwen": "http://qwen:8080"}, [], 10, 0.5, 30, tools=[{"type": "function"}])]
        self.assertEqual(parts[0], "Ok")
        self.assertIsInstance(parts[1], ToolCalls)
        self.assertEqual(list(parts[1]), [{"id": "c1", "name": "generate_image", "arguments": '{"prompt": "x"}'}])

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


class FixedWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_workflow_skips_config(self):
        posted, paths = {}, []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/system_stats":
                return httpx.Response(200, json={})
            if request.url.path == "/prompt":
                posted.update(json.loads(request.content)["prompt"])
                return httpx.Response(200, json={"prompt_id": "p1"})
            if request.url.path == "/history/p1":
                return httpx.Response(200, json={"p1": {"status": {"status_str": "success"}, "outputs": {
                    "8": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}}}})
            if request.url.path == "/view":
                return httpx.Response(200, content=b"png")
            return httpx.Response(404)

        images = Images(WORKFLOWS, httpx.AsyncClient(transport=httpx.MockTransport(handler)), poll_seconds=0,
                        workflows={"qwen-image": "qwen-image21"}, prefix="images")
        name, png, _ = await images.generate({"qwen-image": "http://qwen-image:8188"}, "a cat", 60)
        self.assertEqual((name, png), ("qwen-image", b"png"))
        self.assertNotIn("/local-qwen-image/config", paths)
        self.assertEqual(posted["8"]["inputs"]["filename_prefix"], "images")


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

    def test_notify_user(self):
        store = self.app.state.store
        self.client.portal.call(store.register, 99, "nobody", "Nobody")
        body = {"text": "hi"}
        self.assertEqual(self.client.post("/api/notify-user", headers=self.auth,
                                          json={**body, "tg_id": 12345}).status_code, 409)  # unknown
        self.assertEqual(self.client.post("/api/notify-user", headers=self.auth,
                                          json={**body, "tg_id": 99}).status_code, 409)  # pending
        self.client.post("/api/users/99/allow", headers=self.auth)
        self.assertEqual(self.client.post("/api/notify-user", headers=self.auth,
                                          json={**body, "tg_id": 99}).status_code, 503)  # not polling
        sent = []

        async def notify(chat_id, reply):
            sent.append((chat_id, reply.text))

        def install(service, fn):
            service.notify = fn

        self.client.portal.call(install, self.app.state.service, notify)
        self.assertEqual(self.client.post("/api/notify-user", headers=self.auth,
                                          json={**body, "tg_id": 99}).status_code, 200)
        self.assertEqual(sent, [(99, "hi")])
        # an admin with no /start record can still be messaged
        self.assertEqual(self.client.post("/api/notify-user", headers=self.auth,
                                          json={**body, "tg_id": 1}).status_code, 200)
        self.assertEqual(sent, [(99, "hi"), (1, "hi")])


class SettingsTests(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(mask_token(TOKEN), "123456789:****AAAA")
        self.assertEqual(mask_token(""), "")

    def test_reply_defaults(self):
        self.assertEqual(Reply("x").buttons, [])


if __name__ == "__main__":
    unittest.main()
