"""Process settings from the environment."""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    admin_key: str = ""
    destination: Path = Path("/data/downloads")
    tgbot_url: str = ""
    metadata_timeout: float = 180.0

    @classmethod
    def from_env(cls):
        config = cls(
            admin_key=os.getenv("TORRENT_ADMIN_KEY", ""),
            destination=Path(os.getenv("TORRENT_DESTINATION", "/data/downloads")),
            tgbot_url=os.getenv("TORRENT_TGBOT_URL", ""),
            metadata_timeout=float(os.getenv("TORRENT_METADATA_TIMEOUT", "180")),
        )
        if len(config.admin_key) < 16:
            raise ValueError("TORRENT_ADMIN_KEY must be set (at least 16 characters)")
        return config
