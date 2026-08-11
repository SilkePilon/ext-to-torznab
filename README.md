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
| `MAGNET_WORKERS` | `8` | Concurrent magnet lookups on the direct transport |
| `MAGNET_WORKERS_FLARESOLVERR` | `3` | Concurrent magnet lookups through FlareSolverr |
| `MAGNET_MAX_RESOLVE` | `30` | Magnets resolved per search before the rest go lazy |
| `MAGNET_CACHE_TTL` | `3600` | Seconds a resolved magnet stays cached |
| `PAGE_CACHE_TTL` | `120` | Seconds a fetched search page stays cached (`0` disables) |
| `API_KEY` | _(empty)_ | Optional key required on all requests |
| `PORT` | `5000` | Port the proxy listens on |
| `HOST` | `0.0.0.0` | Bind address |
| `FLARESOLVERR_TIMEOUT` | `60000` | Max ms FlareSolverr waits per page |
| `FLARESOLVERR_TABS_TILL_VERIFY` | `0` | Tabs-till-verify hint for Cloudflare Turnstile bypass (0 = disabled; try `3` if ext.to serves challenges) |
| `INCLUDE_ADULT` | `true` | Include XXX categories in results |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

`GET /healthz` reports which mirror is active, which transport it uses, and
which mirrors are in cooldown.

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
