"""Canonical model -> RSS 2.0 + Atom, atomic file publication, strict validation."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import feedparser

from .models import Item
from .parse import TAIPEI, TRUNC_SUFFIX

log = logging.getLogger("ctime_rss.render")

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _rfc822(dt: datetime) -> str:
    return format_datetime(dt.astimezone(TAIPEI))  # aware -> numeric +0800


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(TAIPEI).isoformat(timespec="seconds")


def _cap(s: str, limit: int) -> tuple[str, bool]:
    if len(s) <= limit:
        return s, False
    return s[: limit - len(TRUNC_SUFFIX)] + TRUNC_SUFFIX, True


def render_rss(
    items: list[Item],
    title: str,
    site_url: str,
    self_url: str,
    contact: str,
    max_items: int,
    fulltext: bool,
    summary_chars: int,
) -> bytes:
    items = sorted(
        items, key=lambda i: i.published or datetime.min.replace(tzinfo=UTC), reverse=True
    )[:max_items]
    build = max((i.published for i in items if i.published), default=datetime.now(UTC))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"'
        ' xmlns:content="http://purl.org/rss/1.0/modules/content/"'
        ' xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        f"<title>{escape(title)}</title>",
        f"<link>{escape(site_url)}</link>",
        f"<description>{escape(title)}</description>",
        "<language>zh-Hant</language>",
        f"<managingEditor>{escape(contact)}</managingEditor>",
        f"<webMaster>{escape(contact)}</webMaster>",
        f"<lastBuildDate>{_rfc822(build)}</lastBuildDate>",
        "<ttl>15</ttl>",
        f'<atom:link rel="self" href="{escape(self_url)}"/>',
    ]
    for i in items:
        pub = i.published or datetime.now(UTC)
        desc, _ = _cap(i.summary, summary_chars)
        lines.append("  <item>")
        lines.append(f"    <title>{escape(i.title)}</title>")
        lines.append(f"    <link>{escape(i.url)}</link>")
        lines.append(f'    <guid isPermaLink="true">{escape(i.guid)}</guid>')
        lines.append(f"    <pubDate>{_rfc822(pub)}</pubDate>")
        if i.author:
            lines.append(f"    <author>{escape(i.author)}</author>")
        lines.append(f"    <description>{escape(desc)}</description>")
        if fulltext and i.summary:
            body, _ = _cap(i.summary, 1500)
            inner = escape(f"{body}\n\n來源：{i.url}")
            lines.append(f"    <content:encoded><![CDATA[{inner}]]></content:encoded>")
        for img in i.images[:1]:
            lines.append(f'    <media:content url="{escape(img)}" medium="image"/>')
        lines.append("  </item>")
    lines += ["</channel>", "</rss>", ""]
    return "\n".join(lines).encode("utf-8")  # UTF-8, no BOM


def render_atom(
    items: list[Item], title: str, site_url: str, self_url: str, contact: str, max_items: int
) -> bytes:
    items = sorted(
        items, key=lambda i: i.published or datetime.min.replace(tzinfo=UTC), reverse=True
    )[:max_items]
    build = max((i.published for i in items if i.published), default=datetime.now(UTC))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"<title>{escape(title)}</title>",
        f'<link rel="alternate" href="{escape(site_url)}"/>',
        f'<link rel="self" href="{escape(self_url)}"/>',
        f"<id>{escape(self_url)}</id>",
        f"<updated>{_rfc3339(build)}</updated>",
        f"<author><name>{escape(contact)}</name></author>",
    ]
    for i in items:
        pub = i.published or datetime.now(UTC)
        summary, _ = _cap(i.summary, 1500)
        lines.append("  <entry>")
        lines.append(f"    <title>{escape(i.title)}</title>")
        lines.append(f'    <link href="{escape(i.url)}"/>')
        lines.append(f"    <id>{escape(i.guid)}</id>")
        lines.append(f"    <published>{_rfc3339(pub)}</published>")
        lines.append(f"    <updated>{_rfc3339(pub)}</updated>")
        if i.author:
            lines.append(f"    <author><name>{escape(i.author)}</name></author>")
        lines.append(f"    <summary>{escape(summary)}</summary>")
        lines.append(f'    <category term="{escape(i.site_section)}"/>')
        lines.append("  </entry>")
    lines += ["</feed>", ""]
    return "\n".join(lines).encode("utf-8")


# ------------------------------------------------------------ publication ----
def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# -------------------------------------------------------------- validation ----
def validate_feed(path: Path, max_items: int, expect_tz_offset: str = "+0800") -> list[str]:
    """Loud validation of one published feed. Returns list of problems (empty = OK)."""
    problems: list[str] = []
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("UTF-8 BOM present")
    if CONTROL_CHARS.search(raw.decode("utf-8", errors="replace")):
        problems.append("stray control chars present")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return [f"malformed XML: {exc}"]

    fp = feedparser.parse(raw)
    if fp.get("bozo_exception") is not None:
        problems.append(f"feedparser bozo: {fp.bozo_exception}")
    if not fp.entries:
        problems.append("feedparser parsed 0 entries")

    is_atom = root.tag == "{http://www.w3.org/2005/Atom}feed"
    now = datetime.now(UTC)
    if is_atom:
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
        if len(entries) > max_items:
            problems.append(f"too many entries: {len(entries)} > {max_items}")
        ids = set()
        for e in entries:
            eid = e.findtext("{http://www.w3.org/2005/Atom}id")
            pub = e.findtext("{http://www.w3.org/2005/Atom}published")
            if not eid:
                problems.append("entry missing <id>")
            if not pub:
                problems.append(f"entry {eid} missing <published>")
                continue
            if eid in ids:
                problems.append(f"duplicate id {eid}")
            ids.add(eid)
            dt = datetime.fromisoformat(pub)
            if dt > now:
                problems.append(f"future date {pub} on {eid}")
            if dt.year < 2000:
                problems.append(f"epoch-zero date on {eid}")
            link = e.find("{http://www.w3.org/2005/Atom}link")
            href = link.get("href") if link is not None else ""
            if href and not href.startswith(("http://", "https://")):
                problems.append(f"relative link {href}")
        return problems

    # RSS 2.0
    ch = root.find("channel")
    if ch is None:
        return ["no <channel>"]
    item_els = ch.findall("item")
    if len(item_els) > max_items:
        problems.append(f"too many items: {len(item_els)} > {max_items}")
    guids = set()
    for it in item_els:
        guid = it.findtext("guid")
        pub = it.findtext("pubDate")
        link = it.findtext("link")
        if not guid:
            problems.append("item missing <guid>")
        if not pub:
            problems.append(f"item {guid} missing <pubDate>")
            continue
        if guid in guids:
            problems.append(f"duplicate guid {guid}")
        guids.add(guid)
        if link and not link.startswith(("http://", "https://")):
            problems.append(f"relative link {link}")
        try:
            dt = datetime.fromisoformat(pub)
        except ValueError:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(pub)
        if dt > now:
            problems.append(f"future date {pub} on {guid}")
        if dt.year < 2000:
            problems.append(f"epoch-zero date on {guid}")
        if dt.utcoffset() is None or dt.strftime("%z") != expect_tz_offset:
            problems.append(f"wrong tz offset on {guid}: {pub} (want {expect_tz_offset})")
    return problems
