"""Parsers: RSS via feedparser, JSON via YAML-declared field mapping,
HTML via lxml selectors declared in YAML. Nothing selector-shaped lives here.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import feedparser
from lxml import html as lxml_html

from .config import FeedConfig, codec_for
from .http import absolutize, clean_text, strip_tracking
from .models import Item

log = logging.getLogger("ctime_rss.parse")

TAIPEI = ZoneInfo("Asia/Taipei")
TRUNC_SUFFIX = "…（全文見原文）"


# ---------------------------------------------------------------- encoding ----
def decode_body(raw: bytes, http_charset: str | None, configured: str) -> str:
    """Trust HTTP charset, then XML/HTML meta decl, then detection. Log the winner."""
    if http_charset:
        try:
            text = raw.decode(http_charset)
            log.info("encoding: HTTP charset %s won", http_charset)
            return text
        except (UnicodeDecodeError, LookupError):
            pass
    if configured != "auto":
        text = _decode(codec_for(configured), raw)
        log.info("encoding: configured %s used", configured)
        return text
    m = re.search(rb"encoding=[\"']?([A-Za-z0-9_-]+)", raw[:2048])
    if m:
        enc = m.group(1).decode("ascii")
        try:
            text = raw.decode(enc)
            log.info("encoding: document declaration %s won", enc)
            return text
        except (UnicodeDecodeError, LookupError):
            pass
    for guess in ("utf-8", "big5-tw", "big5"):
        try:
            raw.decode(guess)
            text = _decode(guess, raw)
            log.info("encoding: detection guess %s won", guess)
            return text
        except UnicodeDecodeError:
            continue
    log.info("encoding: utf-8 with replacement (last resort)")
    return raw.decode("utf-8", errors="replace")


def _decode(codec: str, raw: bytes) -> str:
    """Decode with explicit codec; unmappable bytes become U+FFFD, never a crash."""
    return raw.decode(codec, errors="replace")


# ------------------------------------------------------------------- dates ----
def parse_date(value: str, fmt: str, tz_name: str = "Asia/Taipei") -> datetime | None:
    """Return an aware datetime. Naive source dates are stamped with the site tz."""
    value = (value or "").strip()
    if not value:
        return None
    tz = ZoneInfo(tz_name)
    dt: datetime | None = None
    if fmt in ("iso8601", "iso"):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        for candidate in (fmt, fmt.replace("Z", "")):
            try:
                dt = datetime.strptime(value, candidate)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


# ---------------------------------------------------------------- RSS items ----
def parse_rss(text: str, feed: FeedConfig, fetched_at: datetime) -> list[Item]:
    parsed = feedparser.parse(text)
    if parsed.get("bozo") and not parsed.entries:
        raise ValueError(f"feed {feed.slug}: bozo feed: {parsed.bozo_exception}")
    items: list[Item] = []
    for e in parsed.entries[: feed.max_items]:
        link = strip_tracking(e.get("link") or e.get("id") or "")
        if not link:
            continue
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                published = datetime(*e[key][:6], tzinfo=UTC)
                break
        if published is None:
            log.warning("feed %s: entry %s has no date, using fetched-at", feed.slug, link)
            published = fetched_at
        summary = clean_text(e.get("summary") or "")
        truncated = len(summary) > 1500
        items.append(
            Item(
                guid=link,
                url=link,
                title=clean_text(e.get("title") or "(無標題)"),
                summary=summary[:1500] + TRUNC_SUFFIX if truncated else summary,
                content_truncated=truncated,
                author=clean_text(e.get("author") or feed.author),
                published=published,
                site_section=feed.slug,
                images=[
                    absolutize(link, im["href"])
                    for im in (e.get("media_content") or [])
                    if im.get("href")
                ]
                + ([enc["href"]] if (enc := e.get("enclosure") or {}) else []),
            )
        )
    return items


# --------------------------------------------------------------- JSON items ----
def _dig(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def parse_json(raw: bytes, feed: FeedConfig, fetched_at: datetime) -> list[Item]:
    data = json.loads(raw)
    lst = _dig(data, feed.json.items_path) if feed.json else None
    if not isinstance(lst, list):
        raise ValueError(f"feed {feed.slug}: json.items_path did not resolve to a list")
    spec = feed.json
    if spec is None:
        raise ValueError(f"feed {feed.slug}: kind json but no json: spec")
    items: list[Item] = []
    for entry in lst[: feed.max_items]:
        f = spec.fields
        link = strip_tracking(str(_dig(entry, f.get("url", "url")) or ""))
        if not link:
            continue
        published = (
            parse_date(
                str(_dig(entry, f.get("published", "published")) or ""), spec.date_format, spec.tz
            )
            or fetched_at
        )
        summary = clean_text(str(_dig(entry, f.get("summary", "summary")) or ""))
        truncated = len(summary) > 1500
        imgs = _dig(entry, f.get("images", "images")) or []
        if isinstance(imgs, str):
            imgs = [imgs]
        items.append(
            Item(
                guid=link,
                url=link,
                title=clean_text(str(_dig(entry, f.get("title", "title")) or "(無標題)")),
                summary=summary[:1500] + TRUNC_SUFFIX if truncated else summary,
                content_truncated=truncated,
                author=clean_text(str(_dig(entry, f.get("author", "author")) or "")) or feed.author,
                published=published,
                site_section=feed.slug,
                images=[absolutize(link, str(i)) for i in imgs],
            )
        )
    return items


# -------------------------------------------------------------- HTML items ----
def _resolve_image(el, spec, base_url: str) -> str | None:
    """Resolve lazy-load attrs (data-src/data-original) ahead of a placeholder src."""
    for attr in [spec.attr] + ["data-src", "data-original", "src"]:
        if not attr:
            continue
        val = (el.get(attr) or "").strip()
        if val and not val.startswith("data:"):
            return absolutize(base_url, val)
    img = el.find(".//img")
    if img is not None:
        for attr in ("data-src", "data-original", "src"):
            val = (img.get(attr) or "").strip()
            if val and not val.startswith("data:"):
                return absolutize(base_url, val)
    return None


def parse_html(text: str, feed: FeedConfig, fetched_at: datetime) -> list[Item]:
    spec = feed.html
    if spec is None:
        raise ValueError(f"feed {feed.slug}: kind html but no html: spec")
    doc = lxml_html.fromstring(text)
    out: list[Item] = []
    seen: set[str] = set()
    for node in doc.cssselect(spec.item):
        link_el = (
            node.cssselect(spec.link.selector)[0] if node.cssselect(spec.link.selector) else None
        )
        if link_el is None:
            continue
        href = link_el.get(spec.link.attr or "href") or ""
        link = strip_tracking(absolutize(feed.site_url or href, href))
        if not link or link in seen:
            continue
        seen.add(link)

        title = ""
        if node.cssselect(spec.title.selector):
            t = node.cssselect(spec.title.selector)[0]
            title = clean_text(t.text_content() if not spec.title.attr else t.get(spec.title.attr))
        summary = ""
        if spec.summary and node.cssselect(spec.summary.selector):
            s = node.cssselect(spec.summary.selector)[0]
            summary = clean_text(
                s.get(spec.summary.attr) if spec.summary.attr else s.text_content()
            )
        truncated = len(summary) > 1500

        published = None
        if spec.list_date and node.cssselect(spec.list_date.selector):
            d = node.cssselect(spec.list_date.selector)[0]
            # item_date is authoritative for attr/format/tz; list_date picks the node.
            attr = feed.item_date.attr if feed.item_date else spec.list_date.attr
            raw_date = d.get(attr) if attr else d.text_content()
            published = parse_date(
                clean_text(raw_date),
                feed.item_date.format if feed.item_date else "iso8601",
                feed.item_date.tz if feed.item_date else "Asia/Taipei",
            )
        if published is None:
            log.warning("feed %s: entry %s listing has no date, using fetched-at", feed.slug, link)
            published = fetched_at
        if published > fetched_at + timedelta(minutes=10):
            log.warning(
                "feed %s: future date %s on %s, clamping to fetched-at", feed.slug, published, link
            )
            published = fetched_at

        image = None
        if spec.image:
            targets = node.cssselect(spec.image.selector) or [node]
            image = _resolve_image(targets[0], spec.image, feed.site_url or link)

        out.append(
            Item(
                guid=link,
                url=link,
                title=title or "(無標題)",
                summary=summary[:1500] + TRUNC_SUFFIX if truncated else summary,
                content_truncated=truncated,
                author=feed.author,
                published=published,
                site_section=feed.slug,
                images=[image] if image else [],
            )
        )
        if len(out) >= feed.max_items:
            break
    return out


def parse_body(
    raw: bytes, http_charset: str | None, feed: FeedConfig, fetched_at: datetime | None = None
) -> list[Item]:
    fetched_at = fetched_at or datetime.now(UTC)
    if feed.kind == "rss":
        text = decode_body(raw, http_charset, feed.encoding)
        return parse_rss(text, feed, fetched_at)
    if feed.kind == "json":
        # JSON is UTF-8 by spec; decode_body still logs the charset path.
        text = decode_body(raw, http_charset, "auto" if feed.encoding == "auto" else feed.encoding)
        return parse_json(text.encode("utf-8"), feed, fetched_at)
    text = decode_body(raw, http_charset, feed.encoding)
    return parse_html(text, feed, fetched_at)
