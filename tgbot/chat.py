"""Bot behaviour independent of aiogram: access, sessions, LLM and image proxying.

Bot's own replies are plain text. A finished LLM answer is converted from Markdown to Telegram
HTML (render.py); while it streams it is shown as plain text, since half-written markup is invalid.
"""
import asyncio
import collections
import logging
import re
import secrets
import time
from dataclasses import dataclass, field

import httpx
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from llm import Unavailable
from render import render
from store import NotFound
from market import Market
from torrents import Duplicate, InvalidMagnet, NotFound as TorrentNotFound, Timeout, TorrentError, human_size, magnet_hash

log = logging.getLogger("tgbot")
EDIT_INTERVAL = 1.5
IMAGE_PROMPT_LIMIT = 2000
MAGNET_TTL = 900  # seconds a pending magnet waits for the confirm button

HELP = """Команды:
/new [название] — новая сессия
/sessions — список сессий, переключение и удаление
/switch <id> — переключиться на сессию
/rename <название> — переименовать активную сессию
/delete <id> — удалить сессию
/clear — очистить историю активной сессии
/system [текст] — показать или задать системный промпт сессии (/system reset — сбросить)
/model — какая LLM и генератор изображений доступны
/image <промпт> — сгенерировать изображение
/cancel — прервать текущий запрос
/help — эта справка

Мониторинг рынка БУ:
/query <запрос> — следить за новыми объявлениями Kufar по запросу (например: /query rtx 5090)
/queries — мои поисковые запросы
/delquery <id> — убрать запрос
/watch <ссылка на товар> — следить за ценой конкретного товара (например: /watch https://www.kufar.by/item/123)
/items — товары, за которыми я слежу
/unitem <id> — убрать товар из отслеживания

Уведомления о новых объявлениях и изменениях цен приходят только вам, по вашим запросам.

Любой другой текст отправляется в LLM в активной сессии."""

ADMIN_HELP = """
Администратор:
/users — пользователи и заявки
/allow <tg_id> — открыть доступ
/block <tg_id> — закрыть доступ
/magnet <magnet-ссылка> — скачать торрент (покажет имя и размер, кнопка подтверждения)
/torrents — текущие загрузки
/torrent-del <hash> — убрать загрузку из очереди"""

COMMANDS = [("new", "Новая сессия"), ("sessions", "Мои сессии"), ("clear", "Очистить историю"),
            ("system", "Системный промпт сессии"), ("model", "Доступные модели"),
            ("image", "Сгенерировать изображение"), ("cancel", "Прервать запрос"),
            ("query", "Следить за поисковым запросом"), ("queries", "Мои поисковые запросы"),
            ("watch", "Следить за ценой товара"), ("items", "Товары на отслеживании"),
            ("help", "Справка")]


@dataclass
class Reply:
    text: str
    buttons: list = field(default_factory=list)  # rows of (label, callback_data)
    html: bool = False


@dataclass(frozen=True)
class User:
    id: int
    username: str = ""
    full_name: str = ""


def describe(user):
    name = user.get("full_name") or ""
    handle = f"@{user['username']}" if user.get("username") else ""
    return " ".join(x for x in (name, handle, f"id {user['tg_id']}") if x)


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter("tgbot_requests_total", "Proxied requests", ["kind", "result"],
                                registry=self.registry)
        self.latency = Histogram("tgbot_request_seconds", "Proxied request duration", ["kind"],
                                 buckets=(1, 5, 15, 30, 60, 120, 300, 600), registry=self.registry)
        self.waiting = Gauge("tgbot_waiting_requests", "Requests waiting for a model", ["kind"],
                             registry=self.registry)
        self.polling = Gauge("tgbot_polling", "1 while the bot is polling Telegram", registry=self.registry)


class Service:
    def __init__(self, store, llm, images, metrics=None, torrents=None, market=None):
        self.store, self.llm, self.images = store, llm, images
        self.torrents = torrents  # torrents.Torrents client, or None when TGBOT_TORRENT_URL is unset
        self.market = market  # market.Market client, or None when TGBOT_SCRAPER_URL is unset
        self.metrics = metrics or Metrics()
        self.locks = {"llm": asyncio.Semaphore(1), "image": asyncio.Semaphore(1)}
        self.waiting = {"llm": 0, "image": 0}
        self.tasks = {}
        self.recent = collections.defaultdict(collections.deque)
        self.pending_magnets = {}  # info_hash -> {magnet, name, size, num_files, files, expires}
        self.notify = None  # async (chat_id, Reply) -> None, set by the Telegram adapter

    # access
    async def role(self, user):
        """admin | allowed | pending | blocked | None (never sent /start)."""
        if user.id in (await self.store.settings())["admins"]:
            return "admin"
        record = await self.store.user(user.id)
        return record["status"] if record else None

    async def start(self, user):
        admin = user.id in (await self.store.settings())["admins"]
        record, created = await self.store.register(user.id, user.username, user.full_name,
                                                    "allowed" if admin else "pending")
        if admin and record["status"] != "allowed":
            record = await self.store.set_status(user.id, "allowed")
        if record["status"] == "allowed":
            return Reply("Готово. Пишите сообщение — оно уйдет в LLM.\n\n" + (await self.help(user)).text)
        if record["status"] == "blocked":
            return Reply("Доступ закрыт.")
        if created:
            await self.to_admins(Reply(f"Заявка на доступ: {describe(record)}",
                                       [[("Одобрить", f"allow:{user.id}"), ("Отклонить", f"block:{user.id}")]]))
        return Reply(f"Заявка отправлена администратору. Ваш Telegram id: {user.id}")

    async def to_admins(self, reply):
        if not self.notify:
            return
        for admin in (await self.store.settings())["admins"]:
            try:
                await self.notify(admin, reply)
            except Exception as error:  # an admin who never opened the bot cannot be messaged
                log.warning("Cannot notify admin %s: %s", admin, type(error).__name__)

    async def help(self, user):
        return Reply(HELP + (ADMIN_HELP if await self.role(user) == "admin" else ""))

    async def set_access(self, tg_id, status):
        record = await self.store.set_status(tg_id, status)
        if self.notify:
            text = "Доступ открыт. Отправьте /help." if status == "allowed" else "Доступ закрыт."
            try:
                await self.notify(tg_id, Reply(text))
            except Exception as error:
                log.warning("Cannot notify user %s: %s", tg_id, type(error).__name__)
        return record

    # admin commands
    async def users(self):
        records = await self.store.users()
        if not records:
            return Reply("Пользователей нет.")
        lines, buttons = [], []
        for record in records:
            lines.append(f"{record['status']:8} {describe(record)}, сессий: {record['sessions']}")
            if record["status"] != "allowed":
                buttons.append([(f"Одобрить {record['tg_id']}", f"allow:{record['tg_id']}")])
            else:
                buttons.append([(f"Заблокировать {record['tg_id']}", f"block:{record['tg_id']}")])
        return Reply("\n".join(lines), buttons[:20])

    async def admin_access(self, argument, status):
        try:
            record = await self.set_access(int(argument), status)
        except (ValueError, NotFound):
            return Reply("Укажите tg_id пользователя, который отправлял /start (см. /users).")
        return Reply(f"{describe(record)}: {status}")

    # sessions
    def session_line(self, session):
        mark = "▶" if session.get("active") else " "
        return f"{mark} #{session['id']} {session['title']} — сообщений: {session['messages']}"

    async def new(self, user, title):
        session = await self.store.create_session(user.id, title.strip() or None)
        return Reply(f"Создана и выбрана сессия #{session['id']} «{session['title']}».")

    async def sessions(self, user):
        sessions = await self.store.sessions(user.id)
        if not sessions:
            return Reply("Сессий пока нет. Напишите сообщение или /new.")
        buttons = [[(("▶ " if s["active"] else "") + f"#{s['id']} {s['title']}"[:40], f"sw:{s['id']}"),
                    ("Удалить", f"del:{s['id']}")] for s in sessions[:20]]
        return Reply("Ваши сессии:\n" + "\n".join(self.session_line(s) for s in sessions), buttons)

    async def switch(self, user, argument):
        try:
            session = await self.store.switch_session(user.id, int(argument.lstrip("#")))
        except (ValueError, NotFound):
            return Reply("Сессия не найдена. Список: /sessions")
        return Reply(f"Активна сессия #{session['id']} «{session['title']}», сообщений: {session['messages']}.")

    async def rename(self, user, title):
        session = await self.store.active_session(user.id)
        if not session or not title.strip():
            return Reply("Использование: /rename <название> (нужна активная сессия)")
        session = await self.store.update_session(user.id, session["id"], title=title.strip()[:64])
        return Reply(f"Сессия #{session['id']} переименована в «{session['title']}».")

    async def delete(self, user, argument):
        try:
            active = await self.store.delete_session(user.id, int(argument.lstrip("#")))
        except (ValueError, NotFound):
            return Reply("Сессия не найдена. Список: /sessions")
        suffix = f" Активна #{active['id']} «{active['title']}»." if active else ""
        return Reply("Сессия удалена." + suffix)

    async def clear(self, user):
        session = await self.store.active_session(user.id)
        if not session:
            return Reply("Активной сессии нет.")
        await self.store.clear_session(user.id, session["id"])
        return Reply(f"История сессии #{session['id']} очищена.")

    async def system(self, user, text):
        session = await self.store.active_session(user.id, create=True)
        text = text.strip()
        if not text:
            current = session["system_prompt"] or (await self.store.settings())["system_prompt"]
            origin = "сессии" if session["system_prompt"] else "по умолчанию"
            return Reply(f"Системный промпт ({origin}):\n{current}")
        value = None if text == "reset" else text[:4000]
        await self.store.update_session(user.id, session["id"], system_prompt=value)
        return Reply("Системный промпт сброшен." if value is None else "Системный промпт сессии обновлен.")

    async def model(self, user):
        settings = await self.store.settings()
        lines = []
        for kind, probe, backends in (("LLM", self.llm.active, settings["llm_backends"]),
                                      ("Изображения", self.images.active, settings["image_backends"])):
            try:
                name, _ = await probe(backends)
                lines.append(f"{kind}: {name}")
            except Unavailable as error:
                lines.append(f"{kind}: {error}")
        return Reply("\n".join(lines))

    # torrents (admin only)
    def _magnet_text(self, meta):
        files = meta.get("files", [])
        lines = [f"Название: {meta.get('name') or '—'}",
                 f"Размер: {human_size(meta.get('size', 0))}",
                 f"Файлов: {meta.get('num_files', 0)}"]
        if files:
            lines.append("")
            lines.extend(f"· {path} — {human_size(size)}" for path, size in files[:10])
            if meta.get("num_files", 0) > 10:
                lines.append(f"… и ещё {meta['num_files'] - 10}")
        lines += ["", "Скачать?"]
        return "\n".join(lines)

    def _expire_magnets(self):
        now = time.monotonic()
        for token in [t for t, p in self.pending_magnets.items() if p["expires"] < now]:
            del self.pending_magnets[token]

    async def magnet(self, user, argument, out):
        if not self.torrents:
            return await out.send(Reply("Сервис торрентов не настроен (TGBOT_TORRENT_URL)."))
        self._expire_magnets()
        magnet = argument.strip()
        key = magnet_hash(magnet)
        if key is None:
            return await out.send(Reply("Это не magnet-ссылка. Пример: magnet:?xt=urn:btih:…"))
        if any(p["key"] == key and p["expires"] >= time.monotonic() for p in self.pending_magnets.values()):
            return await out.send(Reply("Эта ссылка уже ожидает подтверждения — нажмите кнопку в прошлом сообщении."))
        handle = await out.send(Reply("Получаю метаданные…"))
        try:
            meta = await self.torrents.metadata(magnet)
        except InvalidMagnet as error:
            return await out.edit(handle, f"⚠️ {error}")
        except Timeout as error:
            return await out.edit(handle, f"⚠️ {error}")
        except TorrentError as error:
            return await out.edit(handle, f"⚠️ {error}")
        except httpx.HTTPError:
            return await out.edit(handle, "⚠️ Сервис торрентов недоступен (make logs-torrent).")
        token = secrets.token_hex(3)
        self.pending_magnets[token] = {"key": key, "magnet": magnet, "name": meta.get("name", ""),
                                       "size": meta.get("size", 0), "num_files": meta.get("num_files", 0),
                                       "files": meta.get("files", []),
                                       "expires": time.monotonic() + MAGNET_TTL}
        return await out.edit(handle, self._magnet_text(meta),
                              buttons=[[("Скачать", f"magnet-yes:{token}"), ("Отмена", f"magnet-no:{token}")]])

    async def magnet_confirm(self, token):
        pending = self.pending_magnets.pop(token, None)
        if not pending or pending["expires"] < time.monotonic():
            return Reply("Запрос устарел. Отправьте /magnet заново.")
        try:
            await self.torrents.start(pending["magnet"])
        except Duplicate as error:
            return Reply(f"⚠️ {error}")
        except Timeout as error:
            return Reply(f"⚠️ {error}")
        except TorrentError as error:
            return Reply(f"⚠️ {error}")
        except httpx.HTTPError:
            return Reply("⚠️ Сервис торрентов недоступен (make logs-torrent).")
        return Reply(f"⬇️ Скачивание «{pending['name']}» запущено. Прогресс: /torrents")

    async def magnet_cancel(self, token):
        return Reply("Отменено.") if self.pending_magnets.pop(token, None) else Reply("Запрос устарел.")

    async def torrent_list(self, user):
        if not self.torrents:
            return Reply("Сервис торрентов не настроен (TGBOT_TORRENT_URL).")
        try:
            rows = await self.torrents.list()
        except httpx.HTTPError:
            return Reply("⚠️ Сервис торрентов недоступен (make logs-torrent).")
        except TorrentError as error:
            return Reply(f"⚠️ {error}")
        if not rows:
            return Reply("Загрузок нет.")
        marks = {"downloading": "⬇", "seeding": "↥", "metadata": "…", "failed": "⚠"}
        lines = []
        for row in rows:
            mark = marks.get(row["state"], "?")
            progress = f" {row['progress'] * 100:.0f}%" if row["state"] == "downloading" else ""
            lines.append(f"{mark} {row['name'] or row['info_hash'][:12]} — {row['state']}{progress}")
        return Reply("\n".join(lines))

    async def torrent_delete(self, user, argument):
        if not self.torrents:
            return Reply("Сервис торрентов не настроен (TGBOT_TORRENT_URL).")
        key = argument.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40,64}", key):
            return Reply("Укажите hash из /torrents.")
        try:
            await self.torrents.remove(key)
        except TorrentNotFound:
            return Reply("Загрузка не найдена. Список: /torrents")
        except httpx.HTTPError:
            return Reply("⚠️ Сервис торрентов недоступен (make logs-torrent).")
        except TorrentError as error:
            return Reply(f"⚠️ {error}")
        return Reply(f"Загрузка {key[:12]}… убрана из очереди.")

    # market monitoring (per user)
    async def market_query(self, user, argument):
        if not self.market:
            return Reply("Мониторинг рынка не настроен (TGBOT_SCRAPER_URL).")
        query = argument.strip()
        if not 1 <= len(query) <= 60:
            return Reply("Запрос от 1 до 60 символов. Пример: /query rtx 5090")
        try:
            existing = await self.market.find_watch("kufar", f"/l?query={query.replace(' ', '+')}&sort=lst.d")
            if existing:
                if existing.get("owner") == user.id:
                    return Reply(f"Вы уже следите за «{query}» (id {existing['id']}). Список: /queries")
                await self.market.subscribe(existing["id"], user.id)
                return Reply(f"Такой запрос уже есть (id {existing['id']}) — я подписал вас на него. "
                             "Новые объявления придут только вам и владельцу.")
            watch = await self.market.create_watch(Market.query_watch(query, user.id))
            return Reply(f"Слежу за новыми объявлениями «{query}» (id {watch['id']}). "
                         "Уведомления — только вам. Список: /queries")
        except httpx.HTTPError:
            return Reply("⚠️ Скрейпер недоступен (make logs-scraper).")
        except httpx.HTTPStatusError as error:
            return Reply(f"⚠️ {error.response.status_code}: {error.response.text[:200]}")

    async def market_queries(self, user):
        if not self.market:
            return Reply("Мониторинг рынка не настроен (TGBOT_SCRAPER_URL).")
        try:
            watches = await self.market.my_watches(user.id)
        except httpx.HTTPError:
            return Reply("⚠️ Скрейпер недоступен (make logs-scraper).")
        if not watches:
            return Reply("Запросов нет. Добавьте: /query rtx 5090")
        lines = [f"Ваши поисковые запросы (удалить: /delquery <id>):"]
        for watch in watches:
            state = "активен" if watch["active"] else "выключен"
            lines.append(f"{watch['id']}. {watch['label']} — {state}, {watch['listings']} объявлений")
        return Reply("\n".join(lines))

    async def market_delquery(self, user, argument):
        if not self.market:
            return Reply("Мониторинг рынка не настроен (TGBOT_SCRAPER_URL).")
        try:
            watch_id = int(argument.strip())
        except ValueError:
            return Reply("Укажите id из /queries.")
        try:
            watches = await self.market.my_watches(user.id)
            watch = next((w for w in watches if w["id"] == watch_id), None)
            if watch is None:
                return Reply(f"Запрос {watch_id} не найден. Список: /queries")
            await self.market.delete_watch(watch_id)
        except httpx.HTTPError:
            return Reply("⚠️ Скрейпер недоступен (make logs-scraper).")
        return Reply(f"Запрос {watch_id} удалён.")

    async def market_watch(self, user, argument):
        if not self.market:
            return Reply("Мониторинг рынка не настроен (TGBOT_SCRAPER_URL).")
        url = argument.strip()
        if not re.fullmatch(r"https://www\.kufar\.by/item/\d+([?/].*)?", url):
            return Reply("Нужна ссылка на товар Kufar: https://www.kufar.by/item/…")
        try:
            item = await self.market.add_item(user.id, url)
        except httpx.HTTPError:
            return Reply("⚠️ Скрейпер недоступен (make logs-scraper).")
        except httpx.HTTPStatusError as error:
            return Reply(f"⚠️ Не удалось открыть объявление ({error.response.status_code}). "
                         "Возможно, его удалили.")
        return Reply(f"Слежу за ценой «{item['title']}» (id {item['id']}, сейчас {item['price']} {item['currency']}). "
                     "Сообщу при любом изменении цены. Список: /items")

    async def market_items(self, user):
        if not self.market:
            return Reply("Мониторинг рынка не настроен (TGBOT_SCRAPER_URL).")
        try:
            items = await self.market.my_items(user.id)
        except httpx.HTTPError:
            return Reply("⚠️ Скрейпер недоступен (make logs-scraper).")
        if not items:
            return Reply("Товаров нет. Добавьте: /watch <ссылка на Kufar>")
        lines = ["Ваши товары (убрать: /unitem <id>):"]
        for item in items:
            lines.append(f"{item['id']}. {item['title']} — {item['price']} {item['currency']}")
            lines.append(f"   {item['url']}")
        return Reply("\n".join(lines))

    async def market_unitem(self, user, argument):
        if not self.market:
            return Reply("Мониторинг рынка не настроен (TGBOT_SCRAPER_URL).")
        try:
            item_id = int(argument.strip())
        except ValueError:
            return Reply("Укажите id из /items.")
        try:
            await self.market.delete_item(item_id, user.id)
        except httpx.HTTPStatusError:
            return Reply(f"Товар {item_id} не найден. Список: /items")
        except httpx.HTTPError:
            return Reply("⚠️ Скрейпер недоступен (make logs-scraper).")
        return Reply(f"Товар {item_id} убран из отслеживания.")

    # proxying
    async def admit(self, user, settings):
        """None and the request is registered as the user's active one, or a refusal Reply.
        Registration happens here, with no await after the check, so two quick messages cannot both pass."""
        if user.id in self.tasks:
            return Reply("Предыдущий запрос еще выполняется. Дождитесь ответа или отправьте /cancel.")
        if user.id not in settings["admins"] and self.limited(user, settings):
            return Reply("Слишком много запросов, повторите через минуту.")
        self.tasks[user.id] = asyncio.current_task()
        return None

    def limited(self, user, settings):
        recent, now = self.recent[user.id], time.monotonic()
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= settings["rate_limit_per_min"]:
            return True
        recent.append(now)
        return False

    async def run(self, user, kind, work, out):
        """Run work() under the model lock; admit() registered the task so /cancel can stop it."""
        started = time.monotonic()
        lock = self.locks[kind]
        try:
            if lock.locked():
                await out.send(Reply(f"В очереди: {self.waiting[kind] + 1}. Ответ придет, когда модель освободится."))
            self.waiting[kind] += 1
            self.metrics.waiting.labels(kind).inc()
            try:
                await lock.acquire()
            finally:
                self.waiting[kind] -= 1
                self.metrics.waiting.labels(kind).dec()
            try:
                await work()
            finally:
                lock.release()
            self.metrics.requests.labels(kind, "ok").inc()
        except asyncio.CancelledError:
            self.metrics.requests.labels(kind, "cancelled").inc()
            await out.send(Reply("Запрос прерван."))
        except Unavailable as error:
            self.metrics.requests.labels(kind, "unavailable").inc()
            await out.send(Reply(f"⚠️ {error}"))
        except Exception as error:
            self.metrics.requests.labels(kind, "error").inc()
            log.exception("Request failed: %s", type(error).__name__)
            await out.send(Reply("⚠️ Внутренняя ошибка, см. make logs-tgbot"))
        finally:
            self.tasks.pop(user.id, None)
            self.metrics.latency.labels(kind).observe(time.monotonic() - started)

    async def cancel(self, user):
        task = self.tasks.get(user.id)
        if not task:
            return Reply("Нет выполняющегося запроса.")
        task.cancel()
        return None  # the cancelled request reports itself

    async def chat(self, user, text, out):
        settings = await self.store.settings()
        if len(text) > settings["max_prompt_chars"]:
            return await out.send(Reply(f"Сообщение длиннее {settings['max_prompt_chars']} символов."))
        if rejected := await self.admit(user, settings):
            return await out.send(rejected)

        async def work():
            session = await self.store.active_session(user.id, create=True)
            history = await self.store.history(user.id, session["id"], settings["max_history_messages"],
                                               settings["max_context_chars"] - len(text))
            system = session["system_prompt"] or settings["system_prompt"]
            messages = ([{"role": "system", "content": system}] if system else []) + history
            messages.append({"role": "user", "content": text})
            await self.llm.active(settings["llm_backends"])  # fail before showing a placeholder
            handle = await out.send(Reply("…"))
            answer, shown, last_edit = "", "…", time.monotonic()
            try:
                async for delta in self.llm.stream(settings["llm_backends"], messages, settings["max_tokens"],
                                                   settings["temperature"], settings["request_timeout"]):
                    answer += delta
                    if time.monotonic() - last_edit >= EDIT_INTERVAL and len(answer) <= 4000:  # past that only the final split shows it
                        shown, last_edit = answer + " …", time.monotonic()
                        await out.edit(handle, shown)
            except asyncio.CancelledError:
                if answer:  # keep what the user already saw, so the dialogue stays consistent
                    await self.store.add_message(user.id, session["id"], "user", text)
                    await self.store.add_message(user.id, session["id"], "assistant", answer + "\n[прервано]")
                    await out.edit(handle, render(answer)[0], html=True)
                raise
            answer = answer.strip() or "(пустой ответ)"
            await self.store.add_message(user.id, session["id"], "user", text)
            await self.store.add_message(user.id, session["id"], "assistant", answer)
            parts = render(answer)
            await out.edit(handle, parts[0], html=True)
            for part in parts[1:]:
                await out.send(Reply(part, html=True))

        await self.run(user, "llm", work, out)

    async def image(self, user, prompt, out):
        prompt = prompt.strip()
        if not prompt:
            return await out.send(Reply("Использование: /image <промпт>"))
        if len(prompt) > IMAGE_PROMPT_LIMIT:
            return await out.send(Reply(f"Промпт длиннее {IMAGE_PROMPT_LIMIT} символов."))
        settings = await self.store.settings()
        if rejected := await self.admit(user, settings):
            return await out.send(rejected)

        async def work():
            name, _ = await self.images.active(settings["image_backends"])
            handle = await out.send(Reply(f"Генерирую ({name})…"))
            started = time.monotonic()
            name, png, seed = await self.images.generate(settings["image_backends"], prompt,
                                                         settings["request_timeout"])
            await out.photo(png, f"{name}, seed {seed}, {time.monotonic() - started:.0f} с")
            await out.edit(handle, f"Готово ({name}).")

        await self.run(user, "image", work, out)

    async def callback(self, user, data):
        """Inline buttons: sw:/del: for own sessions, allow:/block: and magnet-yes:/magnet-no: for admins."""
        action, _, argument = data.partition(":")
        if action == "sw":
            return await self.switch(user, argument)
        if action == "del":
            return await self.delete(user, argument)
        if action in ("allow", "block") and await self.role(user) == "admin":
            return await self.admin_access(argument, "allowed" if action == "allow" else "blocked")
        if action == "magnet-yes" and await self.role(user) == "admin":
            return await self.magnet_confirm(argument)
        if action == "magnet-no" and await self.role(user) == "admin":
            return await self.magnet_cancel(argument)
        return Reply("Действие недоступно.")
