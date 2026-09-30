"""HTTP API for the used-market scraper: watches, manual runs, listings, alerts, reports."""
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field

from adapters.kufar import item_url
from config import load_seed
from httpclient import Client
from metrics import Metrics
from scheduler import Scheduler
from service import Busy, Service
from settings import Settings
from store import Store
from tgbot import Tgbot


class WatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["kufar", "onliner"]
    ref: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=200)
    filter: list[str] = Field(default_factory=list, max_length=50)
    exclude: list[str] = Field(default_factory=list, max_length=50)
    params: dict = Field(default_factory=dict)
    active: bool = True
    owner: int | None = None  # Telegram id of the owning user; None = a shared watch


class SubscribeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tg_id: int


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: int
    url: str = Field(min_length=1, max_length=300)


def create_app(settings=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    metrics = Metrics()
    service = Service(store, Client(delay=settings.delay), Tgbot(settings.tgbot_url, settings.admin_key), metrics)
    scheduler = Scheduler(service, settings.interval, settings.report_hour)

    @asynccontextmanager
    async def lifespan(app):
        await store.open()
        await store.seed_watches(load_seed(settings.seed_path))
        if settings.start_scheduler:
            await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await service.close()
            await store.close()

    app = FastAPI(title="Used market scraper", lifespan=lifespan)
    app.state.store, app.state.service, app.state.scheduler = store, service, scheduler

    def authorized(request: Request):
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(key.encode(), settings.admin_key.encode()):
            raise HTTPException(401, "Invalid admin key", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready():
        """Ready means the database opens and the seed watches are in place."""
        watches = await store.watches()
        return {"status": "ready", "watches": len(watches), "scraping": service.running}

    @app.get("/metrics")
    def prometheus():
        return Response(generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.get("/api/status", dependencies=[Depends(authorized)])
    async def status():
        by_source, pending = await store.counts()
        last = await store.scrapes(limit=1)
        return {"scraping": service.running, "listings": by_source, "pending_alerts": pending,
                "last_scrape": last[0] if last else None}

    @app.get("/api/watches", dependencies=[Depends(authorized)])
    async def list_watches(owner: int | None = None):
        """All watches, or (with ?owner=) the ones the user owns or is subscribed to."""
        return await store.watches_for_owner(owner) if owner is not None else await store.watches()

    @app.post("/api/watches", dependencies=[Depends(authorized)], status_code=201)
    async def create_watch(watch: WatchIn):
        return await store.add_watch(watch.model_dump(mode="json"))

    @app.delete("/api/watches/{watch_id}", dependencies=[Depends(authorized)])
    async def delete_watch(watch_id: int):
        if not await store.delete_watch(watch_id):
            raise HTTPException(404, "Watch not found")
        return {"ok": True}

    # The fixed 'subscribe' routes must come before the {action} wildcard below.
    @app.post("/api/watches/{watch_id}/subscribe", dependencies=[Depends(authorized)])
    async def subscribe_watch(watch_id: int, body: SubscribeIn):
        if not await store.subscribe(watch_id, body.tg_id):
            raise HTTPException(404, "Watch not found")
        return {"ok": True}

    @app.delete("/api/watches/{watch_id}/subscribe", dependencies=[Depends(authorized)])
    async def unsubscribe_watch(watch_id: int, tg_id: int):
        if not await store.unsubscribe(watch_id, tg_id):
            raise HTTPException(404, "Not subscribed")
        return {"ok": True}

    @app.post("/api/watches/{watch_id}/{action}", dependencies=[Depends(authorized)])
    async def watch_action(watch_id: int, action: Literal["enable", "disable"]):
        if not await store.set_watch_active(watch_id, action == "enable"):
            raise HTTPException(404, "Watch not found")
        return {"ok": True}

    @app.post("/api/items", dependencies=[Depends(authorized)], status_code=201)
    async def create_item(body: ItemIn):
        """Register a product link and check its price immediately."""
        normalized = item_url(body.url)
        if not normalized:
            raise HTTPException(400, "Only kufar.by item links are supported: https://www.kufar.by/item/<id>")
        try:
            return await service.add_item(body.owner, normalized)
        except Exception as error:
            raise HTTPException(502, f"Could not check the item: {type(error).__name__}") from error

    @app.get("/api/items", dependencies=[Depends(authorized)])
    async def list_items(owner: int):
        return await store.items(owner=owner)

    @app.delete("/api/items/{item_id}", dependencies=[Depends(authorized)])
    async def delete_item(item_id: int, owner: int):
        record = await store.item(item_id)
        if not record or record["owner"] != owner:
            raise HTTPException(404, "Item not found")
        await store.delete_item(item_id)
        return {"ok": True}

    @app.post("/api/items/{item_id}/check", dependencies=[Depends(authorized)])
    async def check_item(item_id: int):
        try:
            return await service.check_item(item_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except Exception as error:
            raise HTTPException(502, f"Could not check the item: {type(error).__name__}") from error

    @app.post("/api/scrape", dependencies=[Depends(authorized)])
    async def scrape_now(body: dict | None = None):
        """Run a scrape now; watch_ids limits it to particular watches."""
        try:
            watch_ids = (body or {}).get("watch_ids")
            return await service.scrape(watch_ids)
        except Busy as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/report", dependencies=[Depends(authorized)])
    async def report_now():
        try:
            text = await service.report()
        except Exception as error:  # usually the tgbot is unreachable
            raise HTTPException(502, f"Could not send the report: {type(error).__name__}") from error
        if text is None:
            raise HTTPException(409, "No active watches, nothing to report")
        return {"ok": True, "chars": len(text)}

    @app.get("/api/listings", dependencies=[Depends(authorized)])
    async def list_listings(watch_id: int | None = None, limit: int = 100):
        return await store.listings(watch_id=watch_id, limit=min(max(limit, 1), 500))

    @app.get("/api/alerts", dependencies=[Depends(authorized)])
    async def list_alerts(limit: int = 50):
        return await store.alerts(limit=min(max(limit, 1), 500))

    @app.get("/api/scrapes", dependencies=[Depends(authorized)])
    async def list_scrapes(limit: int = 20):
        return await store.scrapes(limit=min(max(limit, 1), 200))

    return app
