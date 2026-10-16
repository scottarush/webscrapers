"""ctime-rss console entry point: discover | build | validate | doctor | once."""

from __future__ import annotations

import argparse
import fcntl
import logging
import logging.handlers
import sys
from logging import StreamHandler
from pathlib import Path

from .config import Settings, load_settings
from .render import validate_feed
from .sources import run_once
from .store import Store

LOCK = Path("/run/lock/ctime-rss.lock")


def setup_logging(settings: Settings) -> None:
    root = logging.getLogger("ctime_rss")
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    root.handlers.clear()
    con = StreamHandler(sys.stderr)
    con.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(con)
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            settings.log_dir / "ctime-rss.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    except OSError:
        root.warning("cannot write logs to %s, stderr only", settings.log_dir)


def _paths(args) -> tuple[Path, Path]:
    cfg = Path(args.config_dir)
    return cfg / "sources.yaml", cfg / "settings.yaml"


def cmd_once(settings: Settings, args) -> int:
    lock_handle = None
    try:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = open(LOCK, "w")
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logging.getLogger("ctime_rss").info("lock held, skipping overlapping run")
        return 0
    try:
        store = Store(settings.state_dir / "state.db")
        status = run_once(settings, store, only=args.feeds or None, fulltext=args.fulltext)
        store.close()
        bad = [s for s, v in status["sources"].items() if not v.get("ok")]
        print(
            f"run ok={status['ok']} requests={status['requests']} "
            f"feeds={len(status['sources'])} problems={bad if bad else 'none'}"
        )
        return 0 if status["ok"] else 1
    finally:
        if lock_handle:
            lock_handle.close()


def cmd_build(settings: Settings, args) -> int:
    """Build feeds from recorded fixtures (offline): --fixture-dir html/json/rss files."""
    fx = Path(args.fixture_dir)
    store = Store(settings.state_dir / "state.db")
    from .parse import parse_body

    n = 0
    for feed in settings.feeds:
        if feed.merge_of or not feed.enabled:
            continue
        cand = [fx / f"{feed.slug}.html", fx / f"{feed.slug}.json", fx / f"{feed.slug}.xml"]
        path = next((c for c in cand if c.exists()), None)
        if path is None:
            print(f"{feed.slug}: no fixture, skipped")
            continue
        raw = path.read_bytes()
        charset = "big5ua" if path.suffix == ".b5" or "big5" in feed.encoding else None
        items = parse_body(raw, charset, feed)
        for it in items:
            store.upsert_item(
                it.url, it.guid, it.title, it.published.isoformat() if it.published else None
            )
        feed.fulltext = args.fulltext
        rss_path, atom_path = _publish(settings, feed, items)
        print(f"{feed.slug}: {len(items)} items -> {rss_path.name}, {atom_path.name}")
        n += 1
    store.close()
    return 0 if n else 1


def _publish(settings, feed, items):
    from .render import atomic_write, render_atom, render_rss

    out = settings.output_dir / "chinatimes"
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
    atomic_write(out / f"{feed.slug}.xml", rss)
    atomic_write(out / f"{feed.slug}.atom.xml", atom)
    return out / f"{feed.slug}.xml", out / f"{feed.slug}.atom.xml"


def cmd_validate(settings: Settings, args) -> int:
    out = settings.output_dir / "chinatimes"
    failed = False
    for feed in settings.feeds:
        for suffix in (".xml", ".atom.xml"):
            path = out / f"{feed.slug}{suffix}"
            if not path.exists():
                print(f"{feed.slug}{suffix}: MISSING")
                failed = True
                continue
            problems = validate_feed(path, feed.max_items)
            if problems:
                failed = True
                print(f"{path.name}: {len(problems)} problems")
                for p in problems[:5]:
                    print(f"  - {p}")
            else:
                print(f"{path.name}: OK")
    return 1 if failed else 0


def cmd_doctor(settings: Settings, args) -> int:
    store = Store(settings.state_dir / "state.db")
    rows = {r[0]: r for r in store.source_rows()}
    status = {}
    status_path = settings.state_dir / "status.json"
    if status_path.exists():
        import json

        status = json.loads(status_path.read_text(encoding="utf-8"))
    src_status = status.get("sources", {})
    print(
        f"{'slug':<12} {'kind':<5} {'robots':<11} {'http':<5} {'age_s':>7} "
        f"{'parsed':>6} {'pub':>5} skips"
    )
    worst = 0
    for feed in settings.feeds:
        r = rows.get(feed.slug)
        last_status = r[2] if r else "-"
        age = f"{__import__('time').time() - r[1]:.0f}" if r and r[1] else "-"
        fails = r[3] if r else 0
        st = src_status.get(feed.slug, {})
        skip = "disallowed" if "disallowed" in str(st.get("error", "")) else ""
        print(
            f"{feed.slug:<12} {feed.kind:<5} {feed.robots:<11} {str(last_status):<5} "
            f"{age:>7} {st.get('parsed', '-'):>6} {st.get('published', '-'):>5} {skip}"
        )
        worst = max(worst, fails)
    bad = [
        f.slug for f in settings.feeds if rows.get(f.slug) and rows[f.slug][3] >= 2 and f.enabled
    ]
    requests = store.last_run_requests()
    print(
        f"last run requests: {requests}; enabled feeds: "
        f"{sum(1 for f in settings.feeds if f.enabled)}"
    )
    store.close()
    if bad:
        print(f"FAIL: sources failing >=2 consecutive runs: {bad}")
        return 1
    return 0


def cmd_discover(settings: Settings, args) -> int:
    """Re-check every enabled non-merge source: status, items parseable, robots."""
    import requests as _rq

    from .http import PoliteSession
    from .parse import parse_body

    session = PoliteSession(settings.user_agent, settings.politeness)
    print(f"{'slug':<12} {'kind':<5} {'http':<5} {'bytes':>7} {'items':>5} robots")
    worst = 0
    for feed in settings.feeds:
        if feed.merge_of or not feed.enabled or not feed.url:
            continue
        try:
            r = session.get(feed.url)
            items = parse_body(r.content, r.charset, feed) if r.status == 200 else []
            print(
                f"{feed.slug:<12} {feed.kind:<5} {r.status:<5} {len(r.content):>7} "
                f"{len(items):>5} {feed.robots}"
            )
            if r.status != 200 or not items:
                worst = 1
        except (_rq.RequestException, PermissionError, ValueError, RuntimeError) as exc:
            print(f"{feed.slug:<12} {feed.kind:<5} FAIL  {exc.__class__.__name__}")
            worst = 1
    return worst


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="ctime-rss", description="Self-hosted China Times RSS publisher"
    )
    p.add_argument("--config-dir", default="config")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("once", help="one full refresh (used by systemd timer)")
    o.add_argument("--feeds", nargs="*", default=None)
    o.add_argument("--fulltext", action="store_true", help="add content:encoded <=1500 chars")

    b = sub.add_parser("build", help="offline build from fixtures")
    b.add_argument("--fixture-dir", required=True)
    b.add_argument("--fulltext", action="store_true")

    sub.add_parser("validate", help="validate published feeds")
    sub.add_parser("doctor", help="source health table")
    sub.add_parser("discover", help="probe all enabled sources")

    args = p.parse_args(argv)
    sources_path, settings_path = _paths(args)
    settings = load_settings(sources_path, settings_path)
    if args.verbose:
        settings.log_level = "DEBUG"
    setup_logging(settings)

    return {
        "once": cmd_once,
        "build": cmd_build,
        "validate": cmd_validate,
        "doctor": cmd_doctor,
        "discover": cmd_discover,
    }[args.cmd](settings, args)


if __name__ == "__main__":
    sys.exit(main())
