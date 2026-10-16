# chinatimes-rss (ctime-rss)

Self-hosted RSS 2.0 + Atom publisher for 中國時報 (chinatimes.com), written by
the `chinatimes-rss` brief. Runs on **aiserver** as a systemd timer, publishes
static XML under `https://aiserver.lan/feeds/chinatimes/`.

## Why this path

China Times publishes **no working feeds** (verified 2026-10-16):

- All 6 official RSS candidates → `404` (chinatimes.com) or `522` (wantchinatimes.com).
- Homepage announces no `<link rel="alternate" type="application/rss+xml">`.
- Section listings are **server-rendered static HTML** (no JS hydration), paginate
  with `?page=N`, and `robots.txt` (`User-agent: *`, only `Disallow: /cdn-cgi/rum`
  and `/ads/`) allows every path used.

So `sources.yaml` declares `kind: html` feeds with YAML selectors. The RSS and
JSON parsers exist and are tested — if the site ever publishes feeds again,
adding a section or switching a source to `kind: rss` is a YAML edit only.

## Feed URLs (once deployed)

| slug | RSS | Atom |
|---|---|---|
| realtime 即時 | `/feeds/chinatimes/realtime.xml` | `realtime.atom.xml` |
| focal 重點聞 | `focal.xml` | `focal.atom.xml` |
| china 兩岸 | `china.xml` | `china.atom.xml` |
| world 國際 | `world.xml` | `world.atom.xml` |
| money 財經 | `money.xml` | `money.atom.xml` |
| sports 體育 | `sports.xml` | `sports.atom.xml` |
| showbiz 娛樂 | `showbiz.xml` | `showbiz.atom.xml` |
| opinion 言論 | `opinion.xml` | `opinion.atom.xml` |
| all 全部 | `all.xml` | `all.atom.xml` |

## Setup (aiserver)

```bash
sudo useradd -r -s /usr/sbin/nologin ctime-rss
sudo mkdir -p /opt/ctime-rss /srv/feeds/chinatimes /var/lib/ctime-rss /var/log/ctime-rss
sudo rsync -a --exclude .venv --exclude 'out-*' --exclude 'state-*' --exclude 'logs-*' \
     ./ /opt/ctime-rss/
cd /opt/ctime-rss
sudo python3 -m venv .venv && sudo .venv/bin/pip install -e .
sudo chown -R ctime-rss:ctime-rss /opt/ctime-rss /var/lib/ctime-rss /var/log/ctime-rss
sudo chown root:root /srv/feeds && sudo mkdir -p /srv/feeds/chinatimes
sudo cp deploy/systemd/ctime-rss.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ctime-rss.timer
sudo systemctl start ctime-rss.service        # first run now
systemctl list-timers | grep ctime
```

nginx (existing LAN server block): include `deploy/nginx/feeds.conf`.

`/srv/feeds` is written by user `ctime-rss`; nginx only reads it.

## Ops runbook

- **Force a run:** `sudo systemctl start ctime-rss.service`
- **Logs:** `journalctl -u ctime-rss -n 40 --no-pager` or
  `/var/log/ctime-rss/ctime-rss.log` (rotated 1 MB × 3, never response bodies).
- **Health table:** `sudo -u ctime-rss /opt/ctime-rss/.venv/bin/ctime-rss \
   --config-dir /opt/ctime-rss/config doctor`
- **Probe sources on demand:** same path, `discover` subcommand.
- **Add a section:** copy a feed block in `config/sources.yaml`, set `slug`,
  `title`, `site_url`, `url`, run `make check`, restart timer. **Never a code edit.**
- **Fix a broken selector:** site changed markup → update `html:` selectors in
  `sources.yaml` (all selectors live there), run `make check`,
  `systemctl start ctime-rss.service`. Confirm with `doctor`.
- **Reset state:** `rm -f /var/lib/ctime-rss/state.db /var/lib/ctime-rss/status.json`
  then force a run. First-run flood is bounded: `<pubDate>` always comes from
  the source, `first_seen` is tracked separately in sqlite.

## Politeness & compliance (hard limits, enforced in code)

- UA `ChinaTimesRSS/1.0 (+https://aiserver.lan/feeds/README; contact: abuse@aiserver.lan)`
  — single honest UA, no rotation, no spoofing, no proxies, no cookies.
- ≤ 1 req/s/host, host concurrency 1, 2–4 s jitter, ≤ 40 requests/run,
  conditional GET (ETag/Last-Modified), exponential backoff + full jitter on
  429/5xx honoring `Retry-After`, **abort the run after 3 consecutive 429/403**.
- 400 KB hard cap per response (aborts above).
- robots.txt fetched once per 6 h with the real UA; every used path's
  allow/disallow decision is logged; disallowed paths are skipped and recorded
  in `/var/lib/ctime-rss/state/compliance.json` and surfaced by `doctor`.
- Content: title/link/timestamp/images + ≤ 1500 chars of text per item, source
  link and attribution on every item. No paywall/login/DRM touching.

**If the site blocks a path:** the run stops using that path, records it, and
`doctor` shows it. Compliant responses, in order: lengthen the interval → drop
the path/section → ask the operator. **Never evade.** (Public mirrors such as
RSS-bridge instances exist as a last-resort fallback note only; not used.)

## Dev

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
make check     # ruff + ruff format --check + pytest (offline, fixtures only) + validate
```

`pytest` performs **zero network calls**; fixtures/ (≤4 KB, ≤5 items) drive the
HTML, RSS and JSON pipelines.
