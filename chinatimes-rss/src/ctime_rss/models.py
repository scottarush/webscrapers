"""Canonical item model shared by parsers and renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Item:
    """Canonical feed item. `published` must always be timezone-aware."""

    guid: str
    url: str
    title: str
    summary: str = ""
    content_truncated: bool = False
    author: str = ""
    published: datetime | None = None
    site_section: str = ""
    images: list[str] = field(default_factory=list)
    lang: str = "zh-Hant"
