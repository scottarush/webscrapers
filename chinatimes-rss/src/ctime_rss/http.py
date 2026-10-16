"""Polite HTTP session: honest UA, token bucket per host, conditional GET,
exponential backoff with full jitter, 400 KB response cap, robots.txt cache,
abort after 3 consecutive 429/403 from one host.

No evasion of any kind: one UA, no rotation, no proxies, no cookie harvesting.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from .config import Politeness

log = logging.getLogger("ctime_rss.http")


class ResponseTooLarge(Exception):
    pass


class HostAborted(Exception):
    """Too many consecutive 429/403 from one host; the run must stop using it."""


class BudgetExceeded(Exception):
    """Per-run request budget exhausted."""


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes = b""
    charset: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    cached_body: bytes | None = None  # previous body when 304


class RobotsCache:
    """Fetch robots.txt once per host per run (cache >= robots_cache_h), real UA."""

    def __init__(self, session: requests.Session, politeness: Politeness, ua: str):
        self._session = session
        self._pol = politeness
        self._ua = ua
        self._hosts: dict[str, tuple[float, RobotFileParser]] = {}

    def allowed(self, url: str, now: float | None = None) -> bool:
        host = urlparse(url).netloc
        now = time.time() if now is None else now
        entry = self._hosts.get(host)
        if entry and now - entry[0] < self._pol.robots_cache_h * 3600:
            rp = entry[1]
        else:
            rp = RobotFileParser()
            robots_url = f"{urlparse(url).scheme}://{host}/robots.txt"
            rp.set_url(robots_url)
            try:
                r = self._session.get(
                    robots_url, timeout=self._pol.timeout_s, headers={"User-Agent": self._ua}
                )
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException as exc:
                log.warning(
                    "robots.txt fetch failed for %s: %s -> treating as allowed",
                    host,
                    exc.__class__.__name__,
                )
                rp.parse([])
            self._hosts[host] = (now, rp)
        ok = rp.can_fetch(self._ua, url)
        log.info("robots %s: %s", "ALLOW" if ok else "DISALLOW", url)
        return ok


class PoliteSession:
    def __init__(self, user_agent: str, politeness: Politeness):
        self.ua = user_agent
        self.pol = politeness
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.robots = RobotsCache(self.session, politeness, user_agent)
        self._last_hit: dict[str, float] = {}
        self._consecutive_errors: dict[str, int] = {}
        self.requests_this_run = 0
        # injectable clock for tests
        self._sleep = time.sleep
        self._now = time.monotonic
        self._rng = random.Random()

    # -- budget / pacing -----------------------------------------------------
    def _spend_budget(self) -> None:
        if self.requests_this_run >= self.pol.max_requests_per_run:
            raise BudgetExceeded(f">{self.pol.max_requests_per_run} requests this run")
        self.requests_this_run += 1

    def _pace(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = 1.0 / self.pol.rps_per_host
            elapsed = self._now() - last
            if elapsed < wait:
                self._sleep(wait - elapsed)
        lo, hi = self.pol.jitter_s
        self._sleep(self._rng.uniform(lo, hi))
        self._last_hit[host] = self._now()

    # -- core fetch ----------------------------------------------------------
    def get(
        self, url: str, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        host = urlparse(url).netloc
        if self._consecutive_errors.get(host, 0) >= self.pol.abort_after_errors:
            raise HostAborted(f"{host}: {self._consecutive_errors[host]} consecutive 429/403")
        if not self.robots.allowed(url):
            log.warning("skipping disallowed URL: %s", url)
            self._skip_reason = url  # recorded by runner into compliance.json
            raise PermissionError(f"robots.txt disallows {url}")

        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        attempt = 0
        while True:
            self._spend_budget()
            self._pace(host)
            try:
                resp = self.session.get(
                    url, headers=headers, timeout=self.pol.timeout_s, stream=True
                )
            except requests.RequestException as exc:
                self._note_error(host, 0)
                attempt += 1
                if attempt > 2:
                    raise
                self._backoff(attempt, None)
                log.warning("network error %s (%s), retry %d", url, exc.__class__.__name__, attempt)
                continue

            code = resp.status_code
            if code in (429, 500, 502, 503, 504):
                self._note_error(host, code)
                attempt += 1
                if attempt > 3:
                    resp.close()
                    if (
                        code == 429
                        and self._consecutive_errors.get(host, 0) >= self.pol.abort_after_errors
                    ):
                        raise HostAborted(f"{host}: repeated 429")
                    raise requests.RequestException(
                        f"{url}: giving up after {attempt} tries (HTTP {code})"
                    )
                self._backoff(attempt, resp.headers.get("Retry-After"))
                resp.close()
                continue

            self._consecutive_errors[host] = 0
            result = FetchResult(
                url=url,
                status=code,
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
                charset=resp.encoding if resp.encoding not in (None, "ISO-8859-1", "") else None,
            )
            if code == 304:
                resp.close()
                result.not_modified = True
                return result
            if code >= 400:
                resp.close()
                return result

            # size-capped body read
            declared = resp.headers.get("Content-Length")
            if declared and int(declared) > self.pol.max_bytes:
                resp.close()
                raise ResponseTooLarge(f"{url}: declared {declared} bytes > cap")
            chunks, total = [], 0
            for chunk in resp.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > self.pol.max_bytes:
                    resp.close()
                    raise ResponseTooLarge(f"{url}: body exceeded {self.pol.max_bytes} bytes")
                chunks.append(chunk)
            result.content = b"".join(chunks)
            return result

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                delay = min(float(retry_after), self.pol.max_backoff_s)
                log.info("honoring Retry-After=%s", retry_after)
                self._sleep(delay)
                return
            except ValueError:
                pass  # HTTP-date form: fall through to exponential
        base = min(2**attempt, self.pol.max_backoff_s)
        delay = self._rng.uniform(0, base)  # full jitter
        log.info("backoff attempt %d: sleeping %.1fs", attempt, delay)
        self._sleep(delay)

    def _note_error(self, host: str, code: int) -> None:
        if code in (429, 403, 0):
            self._consecutive_errors[host] = self._consecutive_errors.get(host, 0) + 1


def strip_tracking(url: str) -> str:
    """Remove per-request tracking params so guid is stable across fetches."""
    keep = []
    for pair in (urlsplit_query(url) or "").split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0].lower()
        if key.startswith("utm_") or key in {
            "fbclid",
            "gclid",
            "ctrack",
            "mc",
            "xtor",
            "s",
            "from",
        }:
            continue
        keep.append(pair)
    base = url.split("?", 1)[0]
    return f"{base}?{'&'.join(keep)}" if keep else base


def urlsplit_query(url: str) -> str:
    return url.split("?", 1)[1] if "?" in url else ""


def absolutize(base: str, href: str) -> str:
    href = (href or "").strip()
    if href.startswith("//"):
        href = urlparse(base).scheme + ":" + href
    return urljoin(base, href)


ControlChar = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_text(s: str) -> str:
    return ControlChar.sub("", s or "").strip()
