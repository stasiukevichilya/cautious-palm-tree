"""FastAPI service: magnet metadata and downloads, controlled from the tgbot."""
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field

from engine import Duplicate, Engine, MagnetError, MagnetTimeout
from metrics import Metrics
from settings import Settings
from tgbot import Tgbot

log = logging.getLogger("torrent")


class MagnetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    magnet: str = Field(min_length=10, max_length=4000)


def create_app(settings=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = settings or Settings.from_env()
    metrics = Metrics()
    tgbot = Tgbot(settings.tgbot_url, settings.admin_key) if settings.tgbot_url else None
    engine = Engine(settings.destination, metrics, notify=tgbot.notify if tgbot else None)

    @asynccontextmanager
    async def lifespan(app):
        settings.destination.mkdir(parents=True, exist_ok=True)
        await engine.start()
        try:
            yield
        finally:
            await engine.stop()
            if tgbot:
                await tgbot.close()

    app = FastAPI(title="Local torrent service", lifespan=lifespan)
    app.state.engine = engine

    def authorized(request: Request):
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(key.encode(), settings.admin_key.encode()):
            raise HTTPException(401, "Invalid admin key", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        return {"status": "ready", "destination": str(settings.destination),
                "torrents": len(engine.torrents)}

    @app.get("/metrics")
    def prometheus():
        return Response(generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.post("/api/metadata", dependencies=[Depends(authorized)])
    async def metadata(update: MagnetIn):
        try:
            return await engine.metadata(update.magnet, settings.metadata_timeout)
        except Duplicate:
            raise HTTPException(409, "Torrent is already downloading")
        except MagnetTimeout:
            raise HTTPException(504, "Could not fetch metadata in time (no seeders found)")
        except MagnetError as error:
            raise HTTPException(400, str(error))

    @app.post("/api/torrents", dependencies=[Depends(authorized)])
    async def start(update: MagnetIn):
        try:
            return await engine.start_download(update.magnet, settings.metadata_timeout)
        except Duplicate:
            raise HTTPException(409, "Torrent is already downloading")
        except MagnetTimeout:
            raise HTTPException(504, "Could not fetch metadata in time (no seeders found)")
        except MagnetError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/torrents", dependencies=[Depends(authorized)])
    def torrents():
        return engine.list()

    @app.delete("/api/torrents/{info_hash}", dependencies=[Depends(authorized)])
    def remove(info_hash: str):
        try:
            engine.remove(info_hash)
        except KeyError:
            raise HTTPException(404, "Torrent not found in queue")
        return {"ok": True}

    @app.post("/api/torrents/{info_hash}/pause", dependencies=[Depends(authorized)])
    def pause(info_hash: str):
        try:
            return engine.pause(info_hash)
        except KeyError:
            raise HTTPException(404, "Torrent not found in queue")
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.post("/api/torrents/{info_hash}/resume", dependencies=[Depends(authorized)])
    def resume(info_hash: str):
        try:
            return engine.resume(info_hash)
        except KeyError:
            raise HTTPException(404, "Torrent not found in queue")
        except ValueError as error:
            raise HTTPException(400, str(error))

    return app
