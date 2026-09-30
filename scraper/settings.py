"""Process settings from the environment; watches and listings live in the database (see store.py)."""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("/data/scraper.db")
    admin_key: str = ""
    tgbot_url: str = "http://tgbot:8080"
    interval: int = 900
    delay: float = 1.0
    seed_path: Path = Path("/app/watch.json")
    report_hour: int = 10
    start_scheduler: bool = True

    @classmethod
    def from_env(cls):
        config = cls(
            db_path=Path(os.getenv("SCRAPER_DB", "/data/scraper.db")),
            admin_key=os.getenv("SCRAPER_ADMIN_KEY", ""),
            tgbot_url=os.getenv("SCRAPER_TGBOT_URL", "http://tgbot:8080").rstrip("/"),
            interval=int(os.getenv("SCRAPER_INTERVAL", "900")),
            delay=float(os.getenv("SCRAPER_DELAY", "1.0")),
            seed_path=Path(os.getenv("SCRAPER_SEED", "/app/watch.json")),
            report_hour=int(os.getenv("SCRAPER_REPORT_HOUR", "10")),
            start_scheduler=os.getenv("SCRAPER_START", "1") == "1",
        )
        if len(config.admin_key) < 16:
            raise ValueError("SCRAPER_ADMIN_KEY must be set (at least 16 characters); see README-RU.md")
        if config.interval < 60:
            raise ValueError("SCRAPER_INTERVAL must be at least 60 seconds")
        if not 0 <= config.report_hour < 24:
            raise ValueError("SCRAPER_REPORT_HOUR must be 0-23")
        return config
