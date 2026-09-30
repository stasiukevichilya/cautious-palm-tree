"""Watch definitions: what to scrape and which ads to keep (seed file watch.json)."""
import json
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

SOURCES = ("kufar", "onliner")


class Watch(BaseModel):
    source: str
    ref: str = Field(min_length=1, max_length=200)  # kufar: category path or search (?query=...), onliner: schema key (e.g. "videocard")
    label: str = Field(default="", max_length=200)
    filter: list[str] = Field(default_factory=list, max_length=50)  # keep an ad if any keyword matches
    exclude: list[str] = Field(default_factory=list, max_length=50)  # drop an ad if any keyword matches
    params: dict = Field(default_factory=dict)  # adapter options, e.g. {"max_pages": 40}
    active: bool = True

    @field_validator("source")
    @classmethod
    def known_source(cls, value):
        if value not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        return value

    def matches(self, *texts) -> bool:
        haystack = " ".join(t for t in texts if t).lower()
        if any(keyword.lower() in haystack for keyword in self.exclude):
            return False
        if not self.filter:
            return True
        return any(keyword.lower() in haystack for keyword in self.filter)


def load_seed(path: Path) -> list[dict]:
    """Validated seed watches; a missing file means an empty database stays empty."""
    if not path.exists():
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Watch(**item).model_dump(mode="json") for item in raw]
