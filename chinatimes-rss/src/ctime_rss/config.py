"""YAML config loading and validation (settings.yaml + sources.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

VALID_KINDS = {"rss", "json", "html"}
VALID_ROBOTS = {"allowed", "disallowed", "not-listed"}
VALID_ENCODINGS = {"utf-8", "big5", "big5ua", "auto"}

# Config-friendly aliases -> Python codec names.
ENCODING_ALIAS = {"big5ua": "big5-tw"}


def codec_for(name: str) -> str:
    return ENCODING_ALIAS.get(name.lower(), name)


class ConfigError(Exception):
    pass


@dataclass
class SelectorSpec:
    selector: str
    attr: str | None = None  # None -> element text


@dataclass
class HTMLSpec:
    item: str
    title: SelectorSpec
    link: SelectorSpec
    image: SelectorSpec | None = None
    summary: SelectorSpec | None = None
    list_date: SelectorSpec | None = None


@dataclass
class JSONSpec:
    items_path: str  # dotted path to the list of items, e.g. "data.list"
    fields: dict[str, str] = field(default_factory=dict)  # canonical -> dotted field
    date_format: str = "iso8601"
    tz: str = "Asia/Taipei"


@dataclass
class ItemDateSpec:
    selector: str
    attr: str | None = None
    format: str = "iso8601"  # "iso8601" or explicit strftime pattern
    tz: str = "Asia/Taipei"


@dataclass
class FeedConfig:
    slug: str
    title: str
    site_url: str
    kind: str
    url: str | None = None
    robots: str = "not-listed"
    enabled: bool = True
    html: HTMLSpec | None = None
    json: JSONSpec | None = None
    item_date: ItemDateSpec | None = None
    encoding: str = "auto"
    max_items: int = 200
    pages: int = 1  # listing pages to walk (adds ?page=N)
    author: str = ""  # static author attribution when source has none per-item
    merge_of: list[str] = field(default_factory=list)  # combined feed: merge these slugs
    fulltext: bool = False


@dataclass
class Politeness:
    rps_per_host: float = 1.0
    host_concurrency: int = 1
    jitter_s: tuple[float, float] = (2.0, 4.0)
    max_requests_per_run: int = 60
    max_bytes: int = 400_000
    timeout_s: float = 20.0
    max_backoff_s: float = 120.0
    abort_after_errors: int = 3
    robots_cache_h: float = 6.0


@dataclass
class Settings:
    base_url: str
    user_agent: str
    contact: str
    output_dir: Path
    state_dir: Path
    log_dir: Path
    log_level: str
    summary_chars: int
    max_age_days: int
    ping_url: str | None
    politeness: Politeness
    feeds: list[FeedConfig]

    def feed(self, slug: str) -> FeedConfig | None:
        return next((f for f in self.feeds if f.slug == slug), None)


def _sel(spec: Any, name: str) -> SelectorSpec:
    if isinstance(spec, str):
        return SelectorSpec(spec)
    if isinstance(spec, dict) and "selector" in spec:
        return SelectorSpec(spec["selector"], spec.get("attr"))
    raise ConfigError(f"bad selector spec for {name!r}: {spec!r}")


def _parse_html(raw: dict, slug: str) -> HTMLSpec:
    for req in ("item", "title", "link"):
        if req not in raw:
            raise ConfigError(f"feed {slug}: html.{req} is required")
    return HTMLSpec(
        item=raw["item"],
        title=_sel(raw["title"], f"{slug}.html.title"),
        link=_sel(raw["link"], f"{slug}.html.link"),
        image=_sel(raw["image"], f"{slug}.html.image") if "image" in raw else None,
        summary=_sel(raw["summary"], f"{slug}.html.summary") if "summary" in raw else None,
        list_date=_sel(raw["list_date"], f"{slug}.html.list_date") if "list_date" in raw else None,
    )


def _parse_feed(raw: dict) -> FeedConfig:
    slug = raw.get("slug")
    if not slug:
        raise ConfigError(f"feed entry missing slug: {raw!r}")
    kind = raw.get("kind", "rss")
    if kind not in VALID_KINDS:
        raise ConfigError(f"feed {slug}: kind must be one of {VALID_KINDS}")
    robots = raw.get("robots", "not-listed")
    if robots not in VALID_ROBOTS:
        raise ConfigError(f"feed {slug}: robots must be one of {VALID_ROBOTS}")
    encoding = raw.get("encoding", "auto")
    if encoding not in VALID_ENCODINGS:
        raise ConfigError(f"feed {slug}: encoding must be one of {VALID_ENCODINGS}")
    html = _parse_html(raw["html"], slug) if "html" in raw else None
    jsonspec = None
    if "json" in raw:
        j = raw["json"]
        if "items_path" not in j:
            raise ConfigError(f"feed {slug}: json.items_path required")
        jsonspec = JSONSpec(
            j["items_path"],
            dict(j.get("fields", {})),
            j.get("date_format", "iso8601"),
            j.get("tz", "Asia/Taipei"),
        )
    item_date = None
    if "item_date" in raw:
        d = raw["item_date"]
        item_date = ItemDateSpec(
            d["selector"], d.get("attr"), d.get("format", "iso8601"), d.get("tz", "Asia/Taipei")
        )
    merge_of = list(raw.get("merge_of", []))
    if kind != "rss" and merge_of:
        raise ConfigError(f"feed {slug}: merge_of only valid with kind: rss (combined feeds)")
    if not merge_of and not raw.get("url"):
        raise ConfigError(f"feed {slug}: url required unless merge_of is set")
    return FeedConfig(
        slug=slug,
        title=raw.get("title", slug),
        site_url=raw.get("site_url", ""),
        kind=kind,
        url=raw.get("url"),
        robots=robots,
        enabled=bool(raw.get("enabled", True)),
        html=html,
        json=jsonspec,
        item_date=item_date,
        encoding=encoding,
        max_items=int(raw.get("max_items", 200)),
        pages=max(1, int(raw.get("pages", 1))),
        author=raw.get("author", ""),
        merge_of=merge_of,
    )


def load_settings(sources_path: Path, settings_path: Path) -> Settings:
    src = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    st = yaml.safe_load(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    if not src or "feeds" not in src:
        raise ConfigError(f"{sources_path}: missing 'feeds'")
    pol_raw = src.get("politeness", {})
    pol = Politeness(
        rps_per_host=float(pol_raw.get("rps_per_host", 1.0)),
        host_concurrency=int(pol_raw.get("host_concurrency", 1)),
        jitter_s=(
            float(pol_raw.get("jitter_s", (2, 4))[0]),
            float(pol_raw.get("jitter_s", (2, 4))[1]),
        ),
        max_requests_per_run=int(pol_raw.get("max_requests_per_run", 60)),
        max_bytes=int(pol_raw.get("max_bytes", 400_000)),
        max_backoff_s=float(pol_raw.get("max_backoff_s", 120)),
        abort_after_errors=int(pol_raw.get("abort_after_errors", 3)),
        robots_cache_h=float(pol_raw.get("robots_cache_h", 6)),
    )
    if pol.rps_per_host > 1.0:
        raise ConfigError("politeness.rps_per_host must not exceed 1.0")
    feeds = [_parse_feed(f) for f in src["feeds"]]
    slugs = [f.slug for f in feeds]
    if len(slugs) != len(set(slugs)):
        raise ConfigError("duplicate feed slugs")
    for f in feeds:
        for m in f.merge_of:
            if m not in slugs:
                raise ConfigError(f"feed {f.slug}: merge_of references unknown slug {m}")
    return Settings(
        base_url=src.get("base_url", "http://localhost:8080/feeds").rstrip("/"),
        user_agent=src.get("user_agent", "ctime-rss/1.0"),
        contact=src.get("contact", "abuse@localhost"),
        output_dir=Path(st.get("output_dir", "./out")),
        state_dir=Path(st.get("state_dir", "./state")),
        log_dir=Path(st.get("log_dir", "./logs")),
        log_level=st.get("log_level", "INFO"),
        summary_chars=int(st.get("summary_chars", 1500)),
        max_age_days=int(st.get("max_age_days", 7)),
        ping_url=st.get("ping_url"),
        politeness=pol,
        feeds=feeds,
    )
