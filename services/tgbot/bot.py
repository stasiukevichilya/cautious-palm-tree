"""aiogram adapter: Telegram updates -> chat.Service, with hot token replacement."""
import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, LinkPreviewOptions, Message)

from chat import COMMANDS, Reply, User
from render import plain

log = logging.getLogger("tgbot")


NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def formatted(call, text, html):
    """Send as HTML; if Telegram rejects the markup, send the same visible text unformatted."""
    if not html:
        return await call(text, None)
    try:
        return await call(text, "HTML")
    except TelegramBadRequest as error:
        if "parse entities" not in str(error) and "unsupported" not in str(error).lower():
            raise
        log.warning("Telegram rejected HTML, sending plain text: %s", error)
        return await call(plain(text), None)


def rows_markup(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows])


def markup(reply):
    return rows_markup(reply.buttons)


class Out:
    """Where a request writes its progress and result."""

    def __init__(self, bot, chat_id):
        self.bot, self.chat_id = bot, chat_id

    async def send(self, reply):
        async def call(text, parse_mode):
            return await self.bot.send_message(self.chat_id, text, reply_markup=markup(reply), parse_mode=parse_mode,
                                               link_preview_options=NO_PREVIEW)
        return await formatted(call, reply.text, reply.html)

    async def edit(self, handle, text, html=False, buttons=None):
        async def call(text, parse_mode):
            await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=handle.message_id,
                                             parse_mode=parse_mode, reply_markup=rows_markup(buttons),
                                             link_preview_options=NO_PREVIEW)
        try:
            await formatted(call, text, html)
        except TelegramBadRequest as error:
            if "not modified" not in str(error):
                raise

    async def photo(self, png, caption):
        file = BufferedInputFile(png, filename="image.png")
        try:
            await self.bot.send_photo(self.chat_id, file, caption=caption)
        except TelegramBadRequest:  # too large or too tall for a photo: send the PNG as a file
            await self.bot.send_document(self.chat_id, file, caption=caption)


def to_user(source):
    return User(source.id, source.username or "", source.full_name or "")


def make_router(service):
    router = Router()
    router.message.filter(F.chat.type == "private")

    async def gate(message, admin=False):
        """The caller's User if allowed (and admin when required); otherwise answers and returns None."""
        user = to_user(message.from_user)
        role = await service.role(user)
        text = {None: "Отправьте /start, чтобы запросить доступ.", "pending": "Заявка ожидает одобрения.",
                "blocked": "Доступ закрыт."}.get(role)
        if not text and admin and role != "admin":
            text = "Команда доступна только администратору."
        if text:
            await message.answer(text)
            return None
        return user

    async def answer(message, reply):
        if reply:
            async def call(text, parse_mode):
                return await message.answer(text, reply_markup=markup(reply), parse_mode=parse_mode)
            await formatted(call, reply.text, reply.html)

    @router.message(CommandStart())
    async def start(message: Message):
        await answer(message, await service.start(to_user(message.from_user)))

    def command(name, method, admin=False, with_args=False):
        async def handler(message: Message, command: CommandObject):
            if user := await gate(message, admin):
                args = (command.args or "",) if with_args else ()
                await answer(message, await method(user, *args))
        router.message(Command(name))(handler)

    command("help", service.help)
    command("new", service.new, with_args=True)
    command("sessions", service.sessions)
    command("switch", service.switch, with_args=True)
    command("rename", service.rename, with_args=True)
    command("delete", service.delete, with_args=True)
    command("clear", service.clear)
    command("system", service.system, with_args=True)
    command("model", service.model)
    command("cancel", service.cancel)
    command("users", lambda user: service.users(), admin=True)
    command("allow", lambda user, arg: service.admin_access(arg, "allowed"), admin=True, with_args=True)
    command("block", lambda user, arg: service.admin_access(arg, "blocked"), admin=True, with_args=True)
    command("torrents", service.torrent_list, admin=True)
    command("torrent-pause", service.torrent_pause, admin=True, with_args=True)
    command("torrent-resume", service.torrent_resume, admin=True, with_args=True)
    command("torrent-del", service.torrent_delete, admin=True, with_args=True)
    command("query", service.market_query, with_args=True)
    command("queries", service.market_queries)
    command("delquery", service.market_delquery, with_args=True)
    command("watch", service.market_watch, with_args=True)
    command("items", service.market_items)
    command("unitem", service.market_unitem, with_args=True)

    @router.message(Command("magnet"))
    async def magnet(message: Message, command: CommandObject, bot: Bot):
        if user := await gate(message, admin=True):
            await service.magnet(user, command.args or "", Out(bot, message.chat.id))

    @router.message(Command("image"))
    async def image(message: Message, command: CommandObject, bot: Bot):
        if user := await gate(message):
            await service.image(user, command.args or "", Out(bot, message.chat.id))

    @router.message(F.text.startswith("/"))
    async def unknown(message: Message):
        await message.answer("Неизвестная команда. /help")

    @router.message(F.text)
    async def text(message: Message, bot: Bot):
        if user := await gate(message):
            await service.chat(user, message.text, Out(bot, message.chat.id))

    @router.message()
    async def other(message: Message):
        await message.answer("Поддерживаются только текстовые сообщения.")

    @router.callback_query()
    async def button(query: CallbackQuery):
        user = to_user(query.from_user)
        if await service.role(user) not in ("admin", "allowed"):
            return await query.answer("Доступ закрыт", show_alert=True)
        reply = await service.callback(user, query.data or "")
        await query.answer()
        if reply and query.message:
            await query.message.answer(reply.text, reply_markup=markup(reply))

    return router


class Runner:
    """Owns the polling task; replace() swaps the token without restarting the process."""

    def __init__(self, service, metrics, api=None):
        self.service, self.metrics, self.api = service, metrics, api  # api: TelegramAPIServer for tests
        self.dispatcher = self.task = self.bot = None
        self.username, self.error = None, None
        self.lock = asyncio.Lock()

    @property
    def status(self):
        return {"polling": bool(self.task and not self.task.done()), "username": self.username, "error": self.error}

    def bot_for(self, token):
        return Bot(token, session=AiohttpSession(api=self.api) if self.api else None)

    async def check(self, token):
        """Validate a token with getMe; raises aiogram errors for bad tokens."""
        bot = self.bot_for(token)
        try:
            return await bot.get_me()
        finally:
            await bot.session.close()

    async def replace(self, token):
        async with self.lock:
            await self._stop()
            if token:
                await self._start(token)

    async def stop(self):
        async with self.lock:
            await self._stop()

    async def _start(self, token):
        bot = self.bot_for(token)
        try:
            me = await bot.get_me()
            await bot.set_my_commands([BotCommand(command=name, description=text) for name, text in COMMANDS])
        except Exception as error:
            await bot.session.close()
            self.error = f"{type(error).__name__}: {error}".replace(token, "***")
            log.error("Bot start failed: %s", self.error)
            return
        self.bot, self.username, self.error = bot, me.username, None
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(make_router(self.service))
        self.service.notify = lambda chat_id, reply: Out(bot, chat_id).send(reply)
        self.task = asyncio.create_task(self._poll())
        log.info("Polling as @%s", me.username)

    async def _poll(self):
        self.metrics.polling.set(1)
        try:
            await self.dispatcher.start_polling(self.bot, handle_signals=False, close_bot_session=True,
                                                allowed_updates=["message", "callback_query"])
        except Exception as error:
            self.error = type(error).__name__
            log.error("Polling stopped: %s", self.error)
        finally:
            self.metrics.polling.set(0)

    async def _stop(self):
        if self.task and not self.task.done():
            await self.dispatcher.stop_polling()
            await self.task
        self.task = self.dispatcher = self.bot = None
        self.username = None
        self.service.notify = None
