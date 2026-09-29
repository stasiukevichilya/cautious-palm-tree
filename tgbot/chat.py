"""Bot behaviour independent of aiogram: access, sessions, LLM and image proxying.

Bot's own replies are plain text. A finished LLM answer is converted from Markdown to Telegram
HTML (render.py); while it streams it is shown as plain text, since half-written markup is invalid.
"""
import asyncio
import collections
import logging
import time
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from llm import Unavailable
from render import render
from store import NotFound

log = logging.getLogger("tgbot")
EDIT_INTERVAL = 1.5
IMAGE_PROMPT_LIMIT = 2000

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

Любой другой текст отправляется в LLM в активной сессии."""

ADMIN_HELP = """
Администратор:
/users — пользователи и заявки
/allow <tg_id> — открыть доступ
/block <tg_id> — закрыть доступ"""

COMMANDS = [("new", "Новая сессия"), ("sessions", "Мои сессии"), ("clear", "Очистить историю"),
            ("system", "Системный промпт сессии"), ("model", "Доступные модели"),
            ("image", "Сгенерировать изображение"), ("cancel", "Прервать запрос"), ("help", "Справка")]


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
    def __init__(self, store, llm, images, metrics=None):
        self.store, self.llm, self.images = store, llm, images
        self.metrics = metrics or Metrics()
        self.locks = {"llm": asyncio.Semaphore(1), "image": asyncio.Semaphore(1)}
        self.waiting = {"llm": 0, "image": 0}
        self.tasks = {}
        self.recent = collections.defaultdict(collections.deque)
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
        """Inline buttons: sw:/del: for own sessions, allow:/block: for admins."""
        action, _, argument = data.partition(":")
        if action == "sw":
            return await self.switch(user, argument)
        if action == "del":
            return await self.delete(user, argument)
        if action in ("allow", "block") and await self.role(user) == "admin":
            return await self.admin_access(argument, "allowed" if action == "allow" else "blocked")
        return Reply("Действие недоступно.")
