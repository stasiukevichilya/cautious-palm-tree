import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import os
import signal
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend import Backend
from jobs import Jobs, QueueFull, Unavailable
from settings import Settings


class Generation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=2000)
    negative_prompt: str = Field(default="", max_length=2000)
    seed: int | None = Field(default=None, ge=0, le=4294967295)
    steps: int = Field(default=30, ge=1, le=60)
    guidance_scale: float = Field(default=5.0, ge=1, le=15, allow_inf_nan=False)
    width: int = Field(default=1024, ge=1024, le=1024)
    height: int = Field(default=1024, ge=1024, le=1024)

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Prompt cannot be blank")
        return value


def create_app(settings=None, backend=None, exit_on_failure=True):
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("sdxl").setLevel(logging.INFO)
    settings = settings or Settings.from_env()
    jobs = Jobs(settings, backend or Backend(settings))

    @asynccontextmanager
    async def lifespan(app):
        async def supervise():
            while True:
                await asyncio.sleep(1)
                if jobs.error and exit_on_failure:
                    logging.getLogger("sdxl").error("Unrecoverable pipeline failure; shutting down")
                    os.kill(os.getpid(), signal.SIGTERM)
                    return

        jobs.start()
        supervisor = asyncio.create_task(supervise())
        try:
            yield
        finally:
            supervisor.cancel()
            with suppress(asyncio.CancelledError):
                await supervisor
            jobs.stop()

    app = FastAPI(title="Local SDXL", lifespan=lifespan)
    app.state.jobs = jobs

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        if not jobs.ready:
            raise HTTPException(503, jobs.error or "Model is loading")
        return {"status": "ready", "pipeline": jobs.backend.info}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(jobs.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.post("/api/generations", status_code=202)
    def generate(request: Generation):
        try:
            return jobs.submit(request.model_dump())
        except Unavailable as error:
            raise HTTPException(503, str(error)) from error
        except QueueFull as error:
            raise HTTPException(429, "Generation queue is full", headers={"Retry-After": "10"}) from error

    @app.get("/api/generations")
    def history():
        return jobs.history()

    @app.get("/api/generations/{identifier}")
    def get(identifier: UUID):
        try:
            return jobs.get(str(identifier))
        except KeyError as error:
            raise HTTPException(404, "Job not found or expired") from error

    @app.delete("/api/generations/{identifier}")
    def cancel(identifier: UUID):
        try:
            return jobs.cancel(str(identifier))
        except KeyError as error:
            raise HTTPException(404, "Job not found or expired") from error

    @app.get("/api/generations/{identifier}/image")
    def image(identifier: UUID):
        record = get(identifier)
        if record["status"] != "completed":
            raise HTTPException(409, "Image is not ready")
        path = settings.output_path / f"{identifier}.png"
        if not path.is_file():
            raise HTTPException(404, "Image expired")
        return FileResponse(path, media_type="image/png", filename=f"sdxl-{record['parameters']['seed']}.png")

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")
    return app
