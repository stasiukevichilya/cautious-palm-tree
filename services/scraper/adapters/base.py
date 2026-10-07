"""Adapter contract: fetch the listings currently visible on a watch."""
import abc
from dataclasses import dataclass, field


@dataclass
class Listing:
    external_id: str
    title: str
    price: float | None
    currency: str
    url: str
    region: str = ""
    condition: str = ""
    description: str = ""
    images: list = field(default_factory=list)


class Adapter(abc.ABC):
    source = ""

    def __init__(self, client):
        self.client = client

    @abc.abstractmethod
    async def fetch(self, watch) -> list[Listing]:
        """watch: a config.Watch; returns all matching listings across its pages."""
