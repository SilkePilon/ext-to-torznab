# ext-to-torznab

A **Dockerised Torznab API proxy** that scrapes [ext.to](https://ext.to) (and its
mirrors) and exposes the results as a standards-compliant
[Torznab](https://torznab.github.io/spec-1.3-draft/) feed.

Requests go out over plain HTTP whenever a mirror allows it and fall back to
[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) only when Cloudflare
challenges them — the solved cookies are then reused, so the fast path returns.

Works natively with **Sonarr, Lidarr, Radarr, Prowlarr**, and any other
application that supports the Newznab/Torznab API.

---

## Features

| Feature               | Details                                                              |
| --------------------- | -------------------------------------------------------------------- |
| Torznab-compliant API | `caps`, `search`, `tvsearch`, `movie`, `music`, `book`               |
| Mirror failover       | `ext.to` → `extto.com` → `ext2.to`, sticky pick + per-host cooldown  |
| Fast path             | Plain HTTP when a mirror allows it; FlareSolverr only when challenged |
| Magnet links          | Resolved during search, concurrently, and cached                     |
| Full category support | All ext.to categories mapped to Torznab IDs, filtered server-side    |
| TV search             | Query + season + episode auto-formatted (`S01E03`)                   |
| IMDb search           | `imdbid=tt1234567` passes `imdb_id` directly to ext.to               |
| Adult content toggle  | `INCLUDE_ADULT=false` hides XXX results                              |
| Turnstile bypass      | `FLARESOLVERR_TABS_TILL_VERIFY` hint for Cloudflare Turnstile        |
| Optional API key      | Protect the proxy with `API_KEY` environment variable                |
| Pagination            | Full `offset` / `limit` support                                      |
| Docker-ready          | Single `docker compose up -d`                                        |

---

## Quick Start

### Option A — GitHub Container Registry (recommended)

No build step required. Pull the pre-built image directly from
[GitHub Packages](https://github.com/SilkePilon/ext-to-torznab/pkgs/container/ext-to-torznab).

Create a `docker-compose.yml` and run `docker compose up -d`:

```yaml
services:
  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: flaresolverr
    environment:
      - LOG_LEVEL=info
    ports:
      - "8191:8191"
    restart: unless-stopped

  ext-to-torznab:
    image: ghcr.io/silkepilon/ext-to-torznab:latest
    container_name: ext-to-torznab
    depends_on:
      - flaresolverr
    ports:
      - "5000:5000"
    environment:
      - FLARESOLVERR_URL=http://flaresolverr:8191
      - EXT_TO_URLS=https://extto.com,https://ext.to,https://ext2.to
      - PREFER_DIRECT=true
      - RESOLVE_MAGNETS=auto
      - API_KEY=
      - INCLUDE_ADULT=true
      - FLARESOLVERR_TIMEOUT=60000
      - FLARESOLVERR_TABS_TILL_VERIFY=0
      - LOG_LEVEL=INFO
    restart: unless-stopped
```

### Option B — Build from source

```bash
git clone https://github.com/SilkePilon/ext-to-torznab
cd ext-to-torznab
docker compose up -d
```

This starts **two containers**:

| Container        | Default port | Description                                          |
| ---------------- | ------------ | ---------------------------------------------------- |
| `flaresolverr`   | 8191         | Chrome headless proxy (solves Cloudflare challenges) |
| `ext-to-torznab` | 5000         | Torznab API                                          |

---

## Verify

```bash
curl "http://localhost:5000/api?t=caps"
curl "http://localhost:5000/api?t=search&q=ubuntu"
curl "http://localhost:5000/healthz"      # active mirror + transport
```

Offline tests (parsing, category mapping, mirror failover):

```bash
python -m unittest discover -s tests
```

---

## Add to Sonarr / Radarr / Lidarr

1. **Settings → Indexers → Add → Torznab**
2. **URL:** `http://<your-host>:5000`
3. **API Key:** leave blank (or match the value set in `API_KEY`)
4. Click **Test** — should show a green tick.

---

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `EXT_TO_URLS` | `https://extto.com,https://ext.to,https://ext2.to` | Mirrors tried in order, with failover |
| `EXT_TO_URL` | _(unset)_ | Single base URL; when set it is tried before `EXT_TO_URLS` |
| `PREFER_DIRECT` | `true` | Try plain HTTP before FlareSolverr |
| `FLARESOLVERR_URL` | `http://flaresolverr:8191` | FlareSolverr instance; empty disables it |
| `HTTP_TIMEOUT` | `20` | Seconds before a plain HTTP request gives up |
| `MIRROR_COOLDOWN` | `300` | Seconds a failed mirror is skipped |
| `RESOLVE_MAGNETS` | `auto` | Resolve magnets during search: `auto` (direct transport only), `always`, `never` |
| `MAGNET_WORKERS` | `12` | Concurrent magnet lookups on the direct transport |
| `MAGNET_WORKERS_FLARESOLVERR` | `3` | Concurrent magnet lookups through FlareSolverr |
| `MAGNET_MAX_RESOLVE` | `30` | Magnets resolved per search before the rest go lazy |
| `MAGNET_TIME_BUDGET` | `5` | Max seconds a search waits for magnets; slower lookups finish in the background and the rows keep their `t=download` link |
| `MAGNET_CACHE_TTL` | `3600` | Seconds a resolved magnet stays cached |
| `PAGE_CACHE_TTL` | `120` | Seconds a fetched search page stays cached (`0` disables) |
| `API_KEY` | _(empty)_ | Optional key required on all requests |
| `PORT` | `5000` | Port the proxy listens on |
| `HOST` | `0.0.0.0` | Bind address |
| `FLARESOLVERR_TIMEOUT` | `60000` | Max ms FlareSolverr waits per page. A Turnstile solve measured 27-57 s from a VPN exit; use `120000` when FlareSolverr egresses through a VPN |
| `FLARESOLVERR_TABS_TILL_VERIFY` | `0` | Tabs-till-verify hint for Cloudflare Turnstile bypass (0 = disabled; try `3` if ext.to serves challenges) |
| `FLARESOLVERR_SESSION_IDLE` | `300` | Seconds of inactivity before the FlareSolverr browser session is released (0 = keep forever) |
| `FLARESOLVERR_SESSION_TTL` | `60` | Minutes after which FlareSolverr itself expires our session (`session_ttl_minutes`), so a session orphaned by a crash cannot live forever (0 = disabled) |
| `INCLUDE_ADULT` | `true` | Include XXX categories in results |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

`GET /healthz` reports which mirror is active, which transport it uses, and
which mirrors are in cooldown.

---

## Performance & memory

Measured September 2026 against the live site (amd64, Docker):

| What | Cost |
| --- | --- |
| Search page fetch (`extto.com`, plain HTTP) | ~1.0 s, of which ~0.75 s is the site's own time-to-first-byte for its 1 MB page |
| Parsing one 50-row page | ~55 ms |
| Resolving 25 magnets (12 workers) | ~0.5 s |
| A challenged mirror (`ext.to`, `ext2.to`) through FlareSolverr | ~12 s per solve |
| Proxy container, idle | ~45 MiB |
| FlareSolverr container, idle, no browser session | ~40 MiB |
| FlareSolverr container with an open session | ~285-355 MiB |

The two challenged mirrors reject plain HTTP even with a Chrome TLS fingerprint
(`curl_cffi`), so a JavaScript challenge solver is still required for them and
FlareSolverr stays in the stack.  Its cost is kept low instead:

* `extto.com` is tried first and normally answers without a challenge, so most
  deployments never open a browser.
* When a challenge *is* solved, the resulting `cf_clearance` cookie is imported
  into the proxy's own HTTP session and the browser session is released after
  `FLARESOLVERR_SESSION_IDLE` seconds, dropping FlareSolverr back to ~40-100 MiB.
* A browser session that errors out (timeout, DNS failure, Turnstile glitch) is
  destroyed before a replacement is created, and every session carries a
  `FLARESOLVERR_SESSION_TTL` so FlareSolverr expires anything the proxy loses
  track of.  Each leaked session is a ~300 MiB Chromium; this is what used to
  OOM-kill FlareSolverr after a few hours.
* Search pages are cached as parsed rows (a few KB each), not as raw HTML.
* Magnet resolution is bounded by `MAGNET_TIME_BUDGET`; anything slower keeps
  resolving in the background and is served from cache on grab.
* The image sets `MALLOC_ARENA_MAX=2`.  Without it glibc gives every
  short-lived worker thread its own heap arena, and the fragmented free space in
  those arenas made the proxy creep from ~45 MiB to an OOM-kill at 256 MiB over
  a day (measured: 8 concurrent searches settle at ~270 MiB unbounded versus
  ~115 MiB with two arenas; `gc.collect()` and `malloc_trim` recover nothing).

If you never need the challenged mirrors, set `FLARESOLVERR_URL=` (empty) and
drop the `flaresolverr` service from the compose file.

---

## Torznab Categories

| ext.to Category   | Torznab ID |
| ----------------- | ---------- |
| Movies            | 2000       |
| Movies / HD       | 2040       |
| Movies / UHD      | 2045       |
| Movies / 3D       | 2060       |
| TV                | 5000       |
| TV / Anime        | 5070       |
| Music             | 3000       |
| Music / MP3       | 3010       |
| Music / Lossless  | 3040       |
| Music / Audiobook | 3030       |
| Books             | 7000       |
| Books / EBook     | 7020       |
| PC                | 4000       |
| PC / Games        | 4050       |
| Console           | 1000       |
| XXX               | 6000       |
| Other             | 8000       |

---

## Architecture

```text
Sonarr / Radarr / Lidarr
        │  GET /api?t=tvsearch&q=...
        ▼
  ext-to-torznab  (FastAPI)
        │
        ├─ 1. plain HTTPS GET /browse/?q=…      ← fast path (~0.3 s)
        │      └─ Cloudflare challenge?  → FlareSolverr solves it once,
        │         its cookies + User Agent are reused for the fast path
        │      └─ host down?              → next mirror, failed host cooled down
        │
        ├─ 2. parse rows with BeautifulSoup
        │
        └─ 3. POST /ajax/getSearchMagnet.php ×N (concurrent) → magnet links
        ▼
  Torznab RSS XML  →  returned to *arr
```

**Magnet links** are resolved during the search itself (up to
`MAGNET_MAX_RESOLVE` per query) and cached, so the \*arr app receives real
`magnet:` links instead of having to call back through `t=download`. Anything
left unresolved still works: `t=download` resolves it on demand, retrying with
a fresh site session on failure.

ext.to allows **60 magnet API calls per PHP session**, then answers "Too many
requests" until a new session starts. The proxy counts its calls and rotates
the session before hitting that ceiling — which is what used to make the first
grab of a release fail while a retry moments later succeeded.

---

## Using a pre-existing FlareSolverr instance

If you already run FlareSolverr separately, override just the URL:

```yaml
# docker-compose.override.yml
services:
  ext-to-torznab:
    environment:
      - FLARESOLVERR_URL=http://192.168.1.10:8191
  flaresolverr:
    profiles: ["disabled"]
```

---

## License

MIT
