"""Offline-first tests: no network. Recorded fixtures drive all three kinds."""

from __future__ import annotations

import copy  # noqa: F401
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from ctime_rss.config import (
    ConfigError,
    FeedConfig,
    HTMLSpec,
    ItemDateSpec,
    JSONSpec,
    Politeness,
    SelectorSpec,
    load_settings,
)
from ctime_rss.http import HostAborted, PoliteSession, ResponseTooLarge
from ctime_rss.models import Item
from ctime_rss.parse import decode_body, parse_body, parse_date
from ctime_rss.render import (
    TAIPEI,
    atomic_write,
    render_atom,
    render_rss,
    validate_feed,
)
from ctime_rss.sources import Compliance, run_feed
from ctime_rss.store import Store  # noqa: F811

ROOT = Path(__file__).resolve().parent.parent
FX = ROOT / "fixtures"


def html_feed(**over) -> FeedConfig:
    """HTML feed with the real chinatimes listing selectors."""
    f = FeedConfig(
        slug="realtime",
        title="測試",
        site_url="https://www.chinatimes.com/realtimenews/",
        kind="html",
        url="https://www.chinatimes.com/realtimenews/",
        html=HTMLSpec(
            item="div.articlebox-compact",
            title=SelectorSpec("h3.title a"),
            link=SelectorSpec("h3.title a"),
            summary=SelectorSpec("p.intro"),
            image=SelectorSpec("img.photo", "src"),
            list_date=SelectorSpec("time"),
        ),
        item_date=ItemDateSpec("time", "datetime", "%Y-%m-%d %H:%M", "Asia/Taipei"),
        author="中時新聞網",
    )
    for k, v in over.items():
        setattr(f, k, v)
    return f


def rss_feed(**over) -> FeedConfig:
    f = FeedConfig(
        slug="realtime",
        title="測試",
        site_url="https://www.chinatimes.com/",
        kind="rss",
        url="https://x/rss.xml",
    )
    for k, v in over.items():
        setattr(f, k, v)
    return f


def json_feed(**over) -> FeedConfig:
    f = FeedConfig(
        slug="realtime",
        title="測試",
        site_url="https://www.chinatimes.com/",
        kind="json",
        url="https://x/api",
        json=JSONSpec(
            "data.list",
            {
                "url": "url",
                "title": "title",
                "summary": "summary",
                "author": "author",
                "images": "images",
                "published": "publish_time",
            },
            date_format="%Y-%m-%dT%H:%M:%S%z",
        ),
        author="中時新聞網",
    )
    for k, v in over.items():
        setattr(f, k, v)
    return f


FETCHED = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


# ----------------------------------------------------------------- kinds ----
def test_html_pipeline_end_to_end():
    raw = (FX / "realtime.html").read_bytes()
    items = parse_body(raw, "utf-8", html_feed(), FETCHED)
    # 4 boxes, 1 duplicate URL -> 3 items
    assert len(items) == 3
    it = items[0]
    assert it.url == "https://www.chinatimes.com/realtimenews/20261016000001-260407"
    assert it.title == "測試標題一"
    assert it.published.tzinfo is not None
    # 12:00 site-local (Asia/Taipei) == 04:00 UTC
    assert it.published.astimezone(TAIPEI).hour == 12
    assert it.published.astimezone(UTC).hour == 4


def test_rss_cdata_round_trip():
    raw = (FX / "realtime.rss.xml").read_bytes()
    items = parse_body(raw, "utf-8", rss_feed(), FETCHED)
    assert len(items) == 2
    assert "<b>HTML</b>" in items[0].summary  # CDATA preserved as text
    xml = render_rss(items, "T", "https://x/", "https://x/f.xml", "c@x", 200, False, 1500)
    assert b"CDATA" in xml or b"&lt;b&gt;HTML&lt;/b&gt;" in xml


def test_json_mapping():
    raw = (FX / "realtime.json").read_bytes()
    items = parse_body(raw, "utf-8", json_feed(), FETCHED)
    assert len(items) == 2
    assert items[0].author == "記者甲"
    assert items[1].author == "中時新聞網"  # falls back to feed author
    assert items[1].images[0].startswith("https://www.chinatimes.com/imgs/")


# ------------------------------------------------------------ encodings ----
def test_big5_transcode():
    raw = (FX / "realtime.big5.html").read_bytes()
    items = parse_body(raw, None, html_feed(encoding="big5ua"), FETCHED)
    assert items[0].title == "測試標題一"


def test_encoding_detection_prefers_utf8():
    raw = (FX / "realtime.big5.html").read_bytes()
    # no charset, auto: utf-8 decode of big5 bytes must fail -> big5 guess wins
    text = decode_body(raw, None, "auto")
    assert "測試標題一" in text


# ----------------------------------------------------- dates and fallback ----
def test_missing_date_fallback_logged(caplog):
    raw = (FX / "realtime.html").read_bytes()
    with caplog.at_level(logging.WARNING, logger="ctime_rss.parse"):
        items = parse_body(raw, "utf-8", html_feed(), FETCHED)
    last = items[-1]
    assert last.published == FETCHED
    assert any("no date" in r.message or "no date" in r.getMessage() for r in caplog.records)


def test_taipei_offset_math():
    dt = parse_date("2026-10-16 20:36", "%Y-%m-%d %H:%M")
    assert dt.utcoffset().total_seconds() == 8 * 3600
    assert dt.astimezone(UTC).hour == 12


# ----------------------------------------------------------------- http ----
class FakeResp:
    def __init__(self, status=200, body=b"", headers=None, declared=None):
        self.status_code = status
        self._body = body
        self.headers = dict(headers or {})
        if declared:
            self.headers["Content-Length"] = str(declared)
        self.encoding = "utf-8"
        self.closed = False

    @property
    def text(self):
        return self._body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        pass


def make_session(**pol_over):
    pol = Politeness(jitter_s=(0, 0), rps_per_host=1000, **pol_over)
    s = PoliteSession("test-ua", pol)
    slept = []
    s._sleep = slept.append
    now = [0.0]
    s._now = lambda: now[0]

    def advance(dt):
        now[0] += dt

    s._advance = advance
    # Never touch the network from tests: robots.txt answers come from patch_http.
    s.robots._session = s.session
    return s, slept


def patch_http(session, responses, robots="User-agent: *\nAllow: /\n"):
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None, stream=None):
        if url.endswith("/robots.txt"):
            return FakeResp(200, robots.encode())
        r = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        if isinstance(r, Exception):
            raise r
        return r

    session.session.get = fake_get
    return calls


def test_429_backoff_honors_retry_after():
    s, slept = make_session()
    patch_http(s, [FakeResp(429, headers={"Retry-After": "7"}), FakeResp(200, b"ok")])
    res = s.get("https://example.org/x")
    assert res.status == 200
    assert 7 in slept  # exact Retry-After honored before the retry


def test_abort_after_three_429s():
    s, slept = make_session()
    patch_http(s, [FakeResp(429)] * 10)
    with pytest.raises(HostAborted):
        s.get("https://example.org/x")
    with pytest.raises(HostAborted):
        s.get("https://example.org/y")  # host stays aborted


def test_size_cap_aborts():
    s, _ = make_session(max_bytes=100)
    patch_http(s, [FakeResp(200, b"x" * 500)])
    with pytest.raises(ResponseTooLarge):
        s.get("https://example.org/big")
    # declared Content-Length over cap aborts before body read
    patch_http(s, [FakeResp(200, b"", declared=999999)])
    with pytest.raises(ResponseTooLarge):
        s.get("https://example.org/big2")


def test_request_budget():
    s, _ = make_session(max_requests_per_run=2)
    patch_http(s, [FakeResp(200, b"ok")] * 5)
    s.get("https://example.org/a")
    s.get("https://example.org/b")
    from ctime_rss.http import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        s.get("https://example.org/c")


def test_robots_disallowed_skipped_and_recorded(tmp_path):
    s, _ = make_session()
    patch_http(s, [FakeResp(200, b"<html></html>")], robots="User-agent: *\nDisallow: /secret/\n")
    from ctime_rss.store import Store

    store = Store(tmp_path / "state.db")
    compliance = Compliance(tmp_path)
    feed = html_feed(url="https://example.org/secret/page")
    res = run_feed(s, store, None, feed, compliance)
    assert not res.ok
    assert "disallowed" in res.error
    compliance.save()
    import json

    entries = json.loads((tmp_path / "compliance.json").read_text())["entries"]
    assert any("secret" in e["url"] for e in entries)


# ------------------------------------------------------------ text rules ----
def test_control_chars_stripped():
    raw = (FX / "realtime.html").read_bytes()
    items = parse_body(raw, "utf-8", html_feed(), FETCHED)
    assert "\x07" not in items[1].title


def test_tracking_params_stripped_from_guid():
    raw = (FX / "tracking.html").read_bytes()
    items = parse_body(raw, "utf-8", html_feed(), FETCHED)
    assert items[0].guid == "https://www.chinatimes.com/realtimenews/20261016000001-260407"


def test_lazy_image_resolution():
    raw = (FX / "realtime.html").read_bytes()
    items = parse_body(raw, "utf-8", html_feed(), FETCHED)
    assert items[1].images == ["https://images.chinatimes.com/b.jpg"]  # data-src won
    assert items[0].images == ["https://images.chinatimes.com/a.jpg"]


def test_exact_1500_char_truncation():
    long_item = Item(
        guid="https://x/1", url="https://x/1", title="t", summary="字" * 2000, published=FETCHED
    )
    xml = render_rss(
        [long_item], "T", "https://x/", "https://x/f.xml", "c@x", 200, False, 1500
    ).decode("utf-8")
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml)
    d = root.find("channel/item/description").text
    assert len(d) == 1500
    assert d.endswith("…（全文見原文）")


# ----------------------------------------------------------- storage/pub ----
def test_page_overlap_deduped_across_pages(tmp_path):
    """Site reordering can put the same article on two listing pages; feed must not
    contain duplicate guids."""
    s, _ = make_session()
    page1 = (FX / "realtime.html").read_bytes()
    # page 2 repeats page 1's first item (site reordered mid-walk)
    page2 = page1.replace(b"20261016000002", b"20261016000009")
    patch_http(s, [FakeResp(200, page1), FakeResp(200, page2)])
    store = Store(tmp_path / "state.db")
    compliance = Compliance(tmp_path)
    feed = html_feed(pages=2)
    res = run_feed(s, store, None, feed, compliance)
    assert res.ok
    urls = [i.url for i in res.items]
    assert len(urls) == len(set(urls))


def test_duplicate_urls_deduped_in_store(tmp_path):
    store = Store(tmp_path / "state.db")
    assert store.upsert_item("https://x/1", "https://x/1", "t", None) is True
    assert store.upsert_item("https://x/1", "https://x/1", "t", None) is False
    assert len(store.known_urls()) == 1


def test_atomic_write_leaves_no_tmp(tmp_path):
    target = tmp_path / "feed.xml"
    atomic_write(target, b"<rss/>")
    assert target.read_bytes() == b"<rss/>"
    assert not list(tmp_path.glob("*.tmp"))
    assert target.stat().st_mode & 0o777 == 0o644


# ------------------------------------------------------------ validation ----
def test_validate_passes_on_good_feed(tmp_path):
    items = [
        Item(
            guid="https://x/1",
            url="https://x/1",
            title="標題",
            summary="s",
            published=datetime(2026, 10, 16, 20, 0, tzinfo=TAIPEI),
        ),
        Item(
            guid="https://x/2",
            url="https://x/2",
            title="標題二",
            published=datetime(2026, 10, 16, 19, 0, tzinfo=TAIPEI),
        ),
    ]
    p = tmp_path / "f.xml"
    atomic_write(
        p,
        render_rss(
            items,
            "中國時報 – 測試",
            "https://x/",
            "https://aiserver.lan/feeds/chinatimes/f.xml",
            "c@x",
            200,
            True,
            1500,
        ),
    )
    assert validate_feed(p, 200) == []
    a = tmp_path / "f.atom.xml"
    atomic_write(a, render_atom(items, "T", "https://x/", "https://x/f.atom.xml", "c@x", 200))
    assert validate_feed(a, 200) == []
    assert b"+0800" in p.read_bytes()


def test_validate_flags_bad_items(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_bytes(
        b'<?xml version="1.0"?>\n<rss><channel><item><guid>a</guid></item></channel></rss>'
    )
    problems = validate_feed(bad, 10)
    assert any("pubDate" in p for p in problems)


def test_validate_flags_future_and_dup(tmp_path):
    future = Item(
        guid="https://x/1",
        url="https://x/1",
        title="t",
        published=datetime.now(UTC).replace(tzinfo=TAIPEI)
        + __import__("datetime").timedelta(days=2),
    )
    dup = Item(
        guid="https://x/1",
        url="https://x/1",
        title="t2",
        published=datetime(2026, 10, 16, 10, 0, tzinfo=TAIPEI),
    )
    p = tmp_path / "f.xml"
    atomic_write(
        p, render_rss([future, dup], "T", "https://x/", "https://x/f.xml", "c@x", 200, False, 1500)
    )
    problems = validate_feed(p, 200)
    assert any("future" in x for x in problems)
    assert any("duplicate" in x for x in problems)


def test_no_bom_utf8():
    items = [
        Item(
            guid="https://x/1",
            url="https://x/1",
            title="中",
            published=datetime(2026, 10, 16, 10, 0, tzinfo=TAIPEI),
        )
    ]
    xml = render_rss(items, "中國時報", "https://x/", "https://x/f.xml", "c@x", 200, False, 1500)
    assert not xml.startswith(b"\xef\xbb\xbf")
    assert "中國時報".encode() in xml


# --------------------------------------------------------------- config ----
def test_settings_load_and_rps_guard(tmp_path):
    st = load_settings(ROOT / "config" / "sources.yaml", ROOT / "config" / "settings.yaml")
    slugs = {f.slug for f in st.feeds}
    assert {
        "realtime",
        "focal",
        "china",
        "world",
        "money",
        "sports",
        "showbiz",
        "opinion",
        "all",
    } <= slugs
    raw = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())
    raw["politeness"]["rps_per_host"] = 5
    p = tmp_path / "sources.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(p, ROOT / "config" / "settings.yaml")
