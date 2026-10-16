# chinatimes-rss — Final Report

Date: 2026-10-16 · Host: aiserver · Operator agent: Hermes (devpc edit-mirror via mutagen session `webscrapers`)

## 1. Path taken

Official feeds all verified dead before any scraper code:

- `chinatimes.com/rss`, `/rss/newest`, `/backend/RSS/newest.xml`, `/backend/rss/newest.xml`,
  `/rss/newest.xml` → HTTP 404
- `wantchinatimes.com/rss/newest.xml` → HTTP 522
- Homepage announces **no** `<link rel="alternate" type="application/rss+xml">`

**Path: server-rendered HTML listings** (`kind: html`, YAML-driven selectors).
Listings are static, paginate with `?page=N`, carry `time[datetime]`
(Asia/Taipei), `p.intro` summaries, and absolute image URLs. The RSS and JSON
renderers are implemented and offline-tested; switching a source when the site
(re-)publishes feeds is a `sources.yaml` edit, never a code edit.

## 2. Sources (all robots=allowed, kind=html)

| slug    | section      | URL                                       |
|---------|--------------|-------------------------------------------|
| realtime| 即時新聞     | /realtimenews/                             |
| focal   | 重點聞       | /money/focus/  (/focal/ is 404)            |
| china   | 兩岸         | /chinese/total/                            |
| world   | 國際         | /world/total/                              |
| money   | 財經         | /money/total/                              |
| sports  | 體育         | /sports/total/                             |
| showbiz | 娛樂         | /star/total/                               |
| opinion | 言論         | /opinion/total/                            |
| all     | 全部         | merged from the eight above (no own fetch) |

## 3. Subscribe URLs (approved nginx addition, port 8080, Tailscale)

```
http://100.111.235.2:8080/feeds/chinatimes/<slug>.xml
http://100.111.235.2:8080/feeds/chinatimes/<slug>.atom.xml
slug ∈ realtime focal china world money sports showbiz opinion all
```

## 4. Verification

- `make check` on aiserver: **All checks passed** — ruff, ruff format, 24 offline tests,
  fixture build, validate.
- `ctime-rss validate` (live feeds): **18/18 OK** (RSS + Atom).
- `doctor`: all 9 sources HTTP 200/304, 0 robots skips, 0 failing runs.
- Served-content round-trip through feedparser: RSS 38 entries / Atom 200 entries,
  `bozo: False`, dates `+0800`.

## 5. Timers & politeness

- User timer `ctime-rss.timer` (10 min, `RandomizedDelaySec=90`, linger enabled) → `once`.
- Root timer `ctime-rss-sync.timer` (10 min) → rsync staging `~/public/feeds` → `/srv/feeds` (0644/0755, root-owned tree).
- Last run: 15 requests, ok; repeat runs return 304 (conditional GET with ETag).
- Enforced: 1 req/s/host, 2–4 s jitter, ≤ 40 req/run, 400 KB cap, full-jitter
  backoff honoring `Retry-After`, abort after 3 consecutive 429/403, robots cache 6 h,
  honest UA with contact.

## 6. Issues found & fixed / notes

- Site reordering during page-walk produced duplicate guids across pages →
  cross-page dedupe added + regression test `test_page_overlap_deduped_across_pages`.
- nginx serves `.xml` as `text/xml` (mime.types mapping wins over `default_type`).
  Valid XML, all feed readers cope; for strict `application/rss+xml` remove the
  `xml` line from `/etc/nginx/mime.types` and reload.
- `wantchinatimes.com` returns 522 → path dropped (compliant response; no evasion).
- `state/compliance.json`: clean, zero disallowed skips.

## 7. Repo

- Commit `e8c4973 chinatimes-rss: self-hosted RSS/Atom publisher for chinatimes.com` (36 files).
- Tree: `chinatimes-rss/{src/ctime_rss/{cli,http,sources,parse,render,store,config,models}.py,
  config/, config.test/, fixtures/, tests/, docs/, deploy/{systemd,nginx}}`.
- Runtime on aiserver: venv at `~/projects/webscrapers/chinatimes-rss/.venv`,
  state `~/.local/state/ctime-rss`, logs `~/.local/log/ctime-rss`,
  config `~/server-config` (settings) → sources.yaml symlinked from the repo.
