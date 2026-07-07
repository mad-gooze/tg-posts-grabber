from dataclasses import dataclass
from datetime import datetime


@dataclass
class Item:
    source: str
    item_id: str
    url: str
    title: str
    text: str
    published: datetime | None = None
    image_url: str | None = None
