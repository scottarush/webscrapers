"""Source registry helpers + the fetch->parse->dedupe->publish run engine."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import FeedConfig, Settings
from .http import BudgetExceeded, HostAborted, PoliteSession, ResponseTooLarge
from .models import Item
from .parse import parse_body
from .render import atomic_write, render_atom, render_rss
from .store import Store

log = logging.getLogger("ctime_rss.sources")


class Compliance:
    """Records robots decisions and blocked paths -> state/compliance.json."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "compliance.json"
        self.entries: list[dict] = []

    def record(self, kind: str, url: str, note: str = "") -> None:
        self.entries.append(
            {"kind": kind, "url": url, "at": datetime.now(UTC).isoformat(), "note": note}
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if self.path.exists():
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8")).get("entries", [])
            except (json.JSONDecodeError, OSError):
                existing = []
        merged = (existing + self.entries)[-500:]
        self.path.write_text(
            json.dumps(
                {"updated": datetime.now(UTC).isoformat(), "entries": merged},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )


class SourceResult:
    def __init__(self, slug: str):
        self.slug = slug
        self.ok = False
        self.status: int | None = None
        self.parsed = 0
        self.published = 0
        self.new = 0
        self.error: str | None = None
        self.not_modified = False
        self.items: list[Item] = []


def run_feed(
    session: PoliteSession,
    store: Store,
    settings: Settings,
    feed: FeedConfig,
    compliance: Compliance,
) -> SourceResult:
    res = SourceResult(feed.slug)
    if not feed.enabled:
        res.error = "disabled"
        return res
    if feed.robots == "disallowed":
        compliance.record(
            "disallowed_skip", feed.url or "", f"feed {feed.slug} config-declared disallowed"
        )
        res.error = "robots disallowed (config)"
        log.warning("feed %s: robots disallowed, skipping", feed.slug)
        return res

    items: list[Item] = []
    fetched_at = datetime.now(UTC)
    if not feed.url:
        res.error = "no url configured"
        return res
    try:
        for page in range(1, feed.pages + 1):
            url = feed.url if page == 1 else f"{feed.url}?page={page}"
            doc = store.get_doc(url)
            result = session.get(url, etag=doc.etag, last_modified=doc.last_modified)
            res.status = result.status
            if result.not_modified:
                res.not_modified = True
                if doc.body:
                    for it in parse_body(doc.body, None, feed, fetched_at):
                        if it.url not in {x.url for x in items}:
                            items.append(it)
                    store.put_doc(url, doc.etag, doc.last_modified)
                continue
            if result.status >= 400:
                raise RuntimeError(f"HTTP {result.status} for {url}")
            store.put_doc(url, result.etag, result.last_modified, result.content)
            for it in parse_body(result.content, result.charset, feed, fetched_at):
                if it.url not in {x.url for x in items}:  # pages can overlap when the
                    items.append(it)                      # site reorders mid-walk
        res.ok = True
    except PermissionError as exc:  # robots disallowed at fetch time
        compliance.record("disallowed_skip", str(exc), f"feed {feed.slug}")
        res.error = "robots disallowed at runtime"
        store.note_source(feed.slug, False, res.status, res.error)
        return res
    except (HostAborted, BudgetExceeded, ResponseTooLarge, RuntimeError, ValueError) as exc:
        res.error = f"{exc.__class__.__name__}: {exc}"
        store.note_source(feed.slug, False, res.status, res.error)
        log.error("feed %s failed: %s", feed.slug, res.error)
        return res

    res.parsed = len(items)
    # dedupe against state: record first_seen separately from published-at
    for it in items:
        is_new = store.upsert_item(
            it.url,
            it.guid,
            it.title,
            it.published.astimezone(UTC).isoformat(timespec="seconds") if it.published else None,
        )
        res.new += int(is_new)
    store.note_source(feed.slug, True, res.status or 200)
    res.published = len(items)
    res.items = items  # type: ignore[attr-defined]
    return res


def publish_feed(settings: Settings, feed: FeedConfig, items: list[Item]) -> tuple[Path, Path]:
    out = settings.output_dir / "chinatimes"
    rss_path = out / f"{feed.slug}.xml"
    atom_path = out / f"{feed.slug}.atom.xml"
    rss = render_rss(
        items,
        feed.title,
        feed.site_url,
        f"{settings.base_url}/chinatimes/{feed.slug}.xml",
        settings.contact,
        feed.max_items,
        feed.fulltext,
        settings.summary_chars,
    )
    atom = render_atom(
        items,
        feed.title,
        feed.site_url,
        f"{settings.base_url}/chinatimes/{feed.slug}.atom.xml",
        settings.contact,
        feed.max_items,
    )
    atomic_write(rss_path, rss)
    atomic_write(atom_path, atom)
    return rss_path, atom_path


def run_once(
    settings: Settings, store: Store, only: list[str] | None = None, fulltext: bool = False
) -> dict:
    """One idempotent refresh. Returns a status dict (also written to status.json)."""
    session = PoliteSession(settings.user_agent, settings.politeness)
    compliance = Compliance(settings.state_dir)
    run_id = store.start_run()
    errors: list[str] = []
    per_source: dict[str, dict] = {}
    parsed_feeds: dict[str, list[Item]] = {}
    started = time.time()

    feeds = [f for f in settings.feeds if (only is None or f.slug in only) and not f.merge_of]
    merged = [f for f in settings.feeds if f.merge_of and (only is None or f.slug in only)]

    aborted = False
    for feed in feeds:
        if aborted:
            per_source[feed.slug] = {"ok": False, "error": "skipped: host aborted"}
            continue
        try:
            r = run_feed(session, store, settings, feed, compliance)
        except HostAborted as exc:
            aborted = True
            errors.append(str(exc))
            per_source[feed.slug] = {"ok": False, "error": str(exc)}
            store.note_source(feed.slug, False, None, str(exc))
            continue
        feed.fulltext = feed.fulltext or fulltext
        per_source[feed.slug] = {
            "ok": r.ok,
            "status": r.status,
            "parsed": r.parsed,
            "published": r.published,
            "new": r.new,
            "not_modified": r.not_modified,
            "error": r.error,
        }
        if r.ok:
            parsed_feeds[feed.slug] = r.items
            try:
                publish_feed(settings, feed, r.items)
            except OSError as exc:
                errors.append(f"{feed.slug}: publish failed: {exc.__class__.__name__}")
        else:
            errors.append(f"{feed.slug}: {r.error}")

    # combined feeds merge already-parsed sources
    for feed in merged:
        combined: list[Item] = []
        for slug in feed.merge_of:
            combined.extend(parsed_feeds.get(slug, []))
        seen: set[str] = set()
        deduped = []
        epoch = datetime.min.replace(tzinfo=UTC)
        for it in sorted(combined, key=lambda i: i.published or epoch, reverse=True):
            if it.guid in seen:
                continue
            seen.add(it.guid)
            deduped.append(it)
        feed.fulltext = feed.fulltext or fulltext
        if parsed_feeds:
            publish_feed(settings, feed, deduped)
        per_source[feed.slug] = {
            "ok": bool(parsed_feeds),
            "merged": len(deduped),
            "published": min(len(deduped), feed.max_items),
        }

    # prune stale state
    pruned = store.prune_old(settings.max_age_days)

    ok = not errors or all("disallowed" in e for e in errors)
    store.finish_run(
        run_id,
        ok,
        session.requests_this_run,
        errors,
        note=f"{len(per_source)} feeds, {pruned} pruned",
    )
    compliance.save()

    status = {
        "last_success": datetime.now(UTC).isoformat(timespec="seconds") if ok else None,
        "started": started,
        "requests": session.requests_this_run,
        "ok": ok,
        "pruned": pruned,
        "sources": per_source,
    }
    (settings.state_dir / "status.json").parent.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    if settings.ping_url and ok:
        try:
            session.session.get(settings.ping_url, timeout=10)
        except Exception as exc:  # noqa: BLE001 - ping is best-effort
            log.warning("ping_url failed: %s", exc.__class__.__name__)
    return status
