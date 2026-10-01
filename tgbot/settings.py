"""Process settings from the environment; runtime settings live in the database (see store.py)."""
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Runtime settings editable through the API; values are stored as JSON in the settings table.
DEFAULTS = {
    "bot_token": "",
    "admins": [],
    "system_prompt": "You are a helpful assistant. Answer in the user's language.",
    "max_history_messages": 40,
    "max_context_chars": 60000,
    "max_prompt_chars": 8000,
    "max_tokens": 2048,
    "temperature": 0.7,
    "request_timeout": 600,
    "rate_limit_per_min": 10,
    "llm_backends": {"qwen": "http://qwen:8080"},
    "image_backends": {"sdxl": "http://sdxl:8080", "qwen-image": "http://qwen-image:8188"},
}


# The bot may only talk to these compose services, addressed by service name. On Docker Desktop any
# container reaches host-published ports via host.docker.internal and other bridge networks by IP, so
# network separation alone cannot keep e.g. the uncensored qwen-image-uc service away from the bot.
ALLOWED_BACKENDS = {"llm_backends": {"qwen"}, "image_backends": {"sdxl", "qwen-image"}}


def backend_error(kind, name, url):
    """None if url is an allowed backend for this settings key, else the reason."""
    host = urlsplit(url).hostname
    if name not in ALLOWED_BACKENDS[kind]:
        return f"{name}: allowed names are {sorted(ALLOWED_BACKENDS[kind])}"
    if host != name:
        return f"{name}: URL host must be the service name {name!r}, got {host!r}"
    return None


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("/data/bot.db")
    admin_key: str = ""
    initial_token: str = ""
    initial_admins: tuple = ()
    workflows_path: Path = Path("/workflows")
    start_bot: bool = True
    torrent_url: str = ""
    scraper_url: str = "http://scraper:8080"
    torrent_metadata_timeout: float = 240.0

    @classmethod
    def from_env(cls):
        config = cls(
            db_path=Path(os.getenv("TGBOT_DB", "/data/bot.db")),
            admin_key=os.getenv("TGBOT_ADMIN_KEY", ""),
            initial_token=os.getenv("TGBOT_TOKEN", ""),
            initial_admins=tuple(int(x) for x in os.getenv("TGBOT_ADMINS", "").replace(",", " ").split()),
            workflows_path=Path(os.getenv("TGBOT_WORKFLOWS", "/workflows")),
            torrent_url=os.getenv("TGBOT_TORRENT_URL", ""),
            scraper_url=os.getenv("TGBOT_SCRAPER_URL", "http://scraper:8080"),
            torrent_metadata_timeout=float(os.getenv("TGBOT_TORRENT_METADATA_TIMEOUT", "240")),
        )
        if len(config.admin_key) < 16:
            raise ValueError("TGBOT_ADMIN_KEY must be set (at least 16 characters); see TGBOT-RU.md")
        return config


def mask_token(token):
    if not token:
        return ""
    bot_id, _, secret = token.partition(":")
    return f"{bot_id}:****{secret[-4:]}" if secret else "****"
