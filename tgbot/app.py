"""Settings API and UI for the Telegram bot; the bot itself polls inside this process."""
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from bot import Runner
from chat import Metrics, Reply, Service
from images import Images
from llm import LLM
from market import Market
from settings import ALLOWED_BACKENDS, Settings, backend_error, mask_token
from store import NotFound, Store
from torrents import Torrents

TOKEN_PATTERN = r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$"


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    admins: list[int] | None = Field(default=None, max_length=50)
    system_prompt: str | None = Field(default=None, max_length=4000)
    max_history_messages: int | None = Field(default=None, ge=0, le=500)
    max_context_chars: int | None = Field(default=None, ge=1000, le=1_000_000)
    max_prompt_chars: int | None = Field(default=None, ge=100, le=100_000)
    max_tokens: int | None = Field(default=None, ge=16, le=32768)
    temperature: float | None = Field(default=None, ge=0, le=2)
    request_timeout: int | None = Field(default=None, ge=10, le=3600)
    rate_limit_per_min: int | None = Field(default=None, ge=1, le=600)
    llm_backends: dict[str, HttpUrl] | None = Field(default=None, min_length=1, max_length=10)
    image_backends: dict[Literal["sdxl", "qwen-image"], HttpUrl] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def only_stack_services(self):
        for kind in ALLOWED_BACKENDS:
            for name, url in (getattr(self, kind) or {}).items():
                if error := backend_error(kind, name, str(url)):
                    raise ValueError(error)
        return self


class TokenUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(pattern=TOKEN_PATTERN)


class NotifyIn(BaseModel):
    """Plain-text push to all admins, used by other stack services (e.g. the scraper)."""
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=8000)


class NotifyUserIn(BaseModel):
    """Plain-text push to one user's chat; the user must have access to the bot."""
    model_config = ConfigDict(extra="forbid")
    tg_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=8000)


class TokenFilter(logging.Filter):
    """Last line of defence: never let a bot token reach the logs."""

    def __init__(self):
        super().__init__()
        self.token = ""

    def filter(self, record):
        if self.token and self.token in (message := record.getMessage()):
            record.msg, record.args = message.replace(self.token, "***"), ()
        return True


def create_app(settings=None, llm=None, images=None, runner_factory=Runner):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    metrics = Metrics()
    torrents = (Torrents(settings.torrent_url, settings.admin_key,
                         metadata_timeout=settings.torrent_metadata_timeout)
                if settings.torrent_url else None)
    market = Market(settings.scraper_url, settings.admin_key) if settings.scraper_url else None
    service = Service(store, llm or LLM(), images or Images(settings.workflows_path), metrics,
                      torrents=torrents, market=market)
    runner = runner_factory(service, metrics)
    token_filter = TokenFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(token_filter)

    @asynccontextmanager
    async def lifespan(app):
        await store.open({"bot_token": settings.initial_token, "admins": list(settings.initial_admins)})
        token = (await store.settings())["bot_token"]
        token_filter.token = token
        if settings.start_bot:
            await runner.replace(token)
        try:
            yield
        finally:
            await runner.stop()
            if torrents:
                await torrents.close()
            if market:
                await market.close()
            await store.close()

    app = FastAPI(title="Local Telegram bot", lifespan=lifespan)
    app.state.store, app.state.service, app.state.runner = store, service, runner

    def authorized(request: Request):
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(key.encode(), settings.admin_key.encode()):
            raise HTTPException(401, "Invalid admin key", headers={"WWW-Authenticate": "Bearer"})

    async def public_settings():
        values = await store.settings()
        return {**values, "bot_token": mask_token(values["bot_token"])}

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready():
        # Ready means the API works; a missing or bad token is reported, not fatal.
        await store.settings()
        return {"status": "ready", "bot": runner.status}

    @app.get("/metrics")
    def prometheus():
        return Response(generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.get("/api/status", dependencies=[Depends(authorized)])
    async def status():
        values = await store.settings()
        backends = {}
        for kind, probe, configured in (("llm", service.llm.active, values["llm_backends"]),
                                        ("image", service.images.active, values["image_backends"])):
            try:
                backends[kind] = (await probe(configured))[0]
            except Exception as error:
                backends[kind] = None
                backends[f"{kind}_error"] = str(error)
        return {"bot": runner.status, "backends": backends, "active_requests": len(service.tasks)}

    @app.get("/api/settings", dependencies=[Depends(authorized)])
    async def get_settings():
        return await public_settings()

    @app.put("/api/settings", dependencies=[Depends(authorized)])
    async def put_settings(update: SettingsUpdate):
        values = update.model_dump(exclude_none=True, mode="json")
        for key in ("llm_backends", "image_backends"):
            if key in values:
                values[key] = {name: url.rstrip("/") for name, url in values[key].items()}
        await store.update_settings(values)
        return await public_settings()

    @app.put("/api/settings/token", dependencies=[Depends(authorized)])
    async def put_token(update: TokenUpdate):
        try:
            me = await runner.check(update.token)
        except Exception as error:
            raise HTTPException(400, f"Telegram rejected the token: {type(error).__name__}") from error
        await store.update_settings({"bot_token": update.token})
        token_filter.token = update.token
        await runner.replace(update.token)
        return {"username": me.username, "bot": runner.status}

    @app.delete("/api/settings/token", dependencies=[Depends(authorized)])
    async def delete_token():
        await store.update_settings({"bot_token": ""})
        await runner.replace("")
        return {"bot": runner.status}

    @app.get("/api/users", dependencies=[Depends(authorized)])
    async def users():
        admins = set((await store.settings())["admins"])
        return [{**user, "admin": user["tg_id"] in admins} for user in await store.users()]

    @app.post("/api/users/{tg_id}/{action}", dependencies=[Depends(authorized)])
    async def user_access(tg_id: int, action: Literal["allow", "block"]):
        try:
            return await service.set_access(tg_id, "allowed" if action == "allow" else "blocked")
        except NotFound as error:
            raise HTTPException(404, "User has not sent /start") from error

    @app.post("/api/notify", dependencies=[Depends(authorized)])
    async def notify(update: NotifyIn):
        """Push a plain-text message to all admins (Bearer TGBOT_ADMIN_KEY)."""
        if not (await store.settings())["admins"]:
            raise HTTPException(409, "No administrators configured")
        if not service.notify:
            raise HTTPException(503, "Bot is not polling")
        await service.to_admins(Reply(update.text))
        return {"ok": True}

    @app.post("/api/notify-user", dependencies=[Depends(authorized)])
    async def notify_user(update: NotifyUserIn):
        """Push a plain-text message to one user (Bearer TGBOT_ADMIN_KEY)."""
        allowed = update.tg_id in (await store.settings())["admins"] or (
            (record := await store.user(update.tg_id)) and record["status"] == "allowed")
        if not allowed:
            raise HTTPException(409, f"User {update.tg_id} has no access to the bot")
        if not service.notify:
            raise HTTPException(503, "Bot is not polling")
        await service.notify(update.tg_id, Reply(update.text))
        return {"ok": True}

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
    return app
