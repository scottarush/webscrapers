"""SQLite state: item dedupe, ETag cache, run log, per-source health."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  url TEXT PRIMARY KEY,
  guid TEXT,
  title TEXT,
  first_seen REAL,
  last_seen REAL,
  published TEXT,
  etag TEXT,
  last_modified TEXT
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started REAL,
  finished REAL,
  ok INTEGER,
  requests INTEGER,
  errors_json TEXT,
  note TEXT
);
CREATE TABLE IF NOT EXISTS sources(
  slug TEXT PRIMARY KEY,
  last_success REAL,
  last_status INTEGER,
  consecutive_failures INTEGER DEFAULT 0,
  last_error TEXT
);
"""


@dataclass
class CachedDoc:
    etag: str | None
    last_modified: str | None
    body: bytes | None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- documents (conditional-GET cache) ------------------------------------
    def _body_cache_path(self, url: str) -> Path:
        import hashlib

        h = hashlib.sha256(url.encode()).hexdigest()[:32]
        p = self.path.parent / "doccache"
        p.mkdir(parents=True, exist_ok=True)
        return p / h

    def get_doc(self, url: str) -> CachedDoc:
        row = self.db.execute(
            "SELECT etag, last_modified FROM items WHERE url=?", ("doc:" + url,)
        ).fetchone()
        if not row:
            return CachedDoc(None, None, None)
        body = self._body_cache_path(url)
        return CachedDoc(row[0], row[1], body.read_bytes() if body.exists() else None)

    def put_doc(
        self, url: str, etag: str | None, last_modified: str | None, body: bytes | None = None
    ) -> None:
        self.db.execute(
            "INSERT INTO items(url, guid, first_seen, last_seen, etag, last_modified) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
            "etag=excluded.etag, last_modified=excluded.last_modified",
            ("doc:" + url, None, time.time(), time.time(), etag, last_modified),
        )
        if body is not None:
            self._body_cache_path(url).write_bytes(body)
        self.db.commit()

    # -- items -----------------------------------------------------------------
    def known_urls(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT url FROM items WHERE guid IS NOT NULL")}

    def upsert_item(self, url: str, guid: str, title: str, published: str | None) -> bool:
        """Insert or refresh an item. Returns True if newly seen."""
        existed = self.db.execute("SELECT 1 FROM items WHERE url=?", (url,)).fetchone()
        self.db.execute(
            "INSERT INTO items(url, guid, title, first_seen, last_seen, published) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET last_seen=excluded.last_seen",
            (url, guid, title, time.time(), time.time(), published),
        )
        self.db.commit()
        return existed is None

    def prune_old(self, max_age_days: int) -> int:
        """Drop items older than max_age_days that we never published.

        Everything with a guid was published at least once, so it is kept.
        """
        cutoff = time.time() - max_age_days * 86400
        cur = self.db.execute("DELETE FROM items WHERE guid IS NULL AND last_seen < ?", (cutoff,))
        cur2 = self.db.execute(
            "DELETE FROM items WHERE published IS NOT NULL AND published != '' "
            "AND CAST(SUBSTR(published,1,10) AS DATE) < DATE(?, 'localtime') "
            "AND first_seen < ?",
            (time.strftime("%Y-%m-%d", time.gmtime(cutoff)), cutoff),
        )
        self.db.commit()
        return cur.rowcount + cur2.rowcount

    # -- runs / sources ----------------------------------------------------------
    def start_run(self) -> int:
        cur = self.db.execute("INSERT INTO runs(started, ok) VALUES(?,0)", (time.time(),))
        self.db.commit()
        return cur.lastrowid

    def finish_run(
        self, run_id: int, ok: bool, requests: int, errors: list[str], note: str = ""
    ) -> None:
        self.db.execute(
            "UPDATE runs SET finished=?, ok=?, requests=?, errors_json=?, note=? WHERE id=?",
            (time.time(), int(ok), requests, json.dumps(errors), note, run_id),
        )
        self.db.commit()

    def note_source(
        self, slug: str, ok: bool, status: int | None, error: str | None = None
    ) -> None:
        row = self.db.execute(
            "SELECT consecutive_failures FROM sources WHERE slug=?", (slug,)
        ).fetchone()
        fails = 0 if ok else (row[0] if row else 0) + 1
        self.db.execute(
            "INSERT INTO sources(slug, last_success, last_status, "
            "consecutive_failures, last_error) "
            "VALUES(?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
            "last_success=COALESCE(excluded.last_success, sources.last_success), "
            "last_status=excluded.last_status, "
            "consecutive_failures=excluded.consecutive_failures, "
            "last_error=excluded.last_error",
            (slug, time.time() if ok else None, status, fails, error),
        )
        self.db.commit()

    def source_rows(self) -> list[tuple]:
        return self.db.execute(
            "SELECT s.slug, s.last_success, s.last_status, s.consecutive_failures, s.last_error "
            "FROM sources s ORDER BY s.slug"
        ).fetchall()

    def last_run_requests(self) -> int | None:
        row = self.db.execute(
            "SELECT requests FROM runs WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None


def iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
