# ext-to-torznab

A **Dockerised Torznab API proxy** that scrapes [ext.to](https://ext.to) through
[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) (to bypass Cloudflare)
and exposes the results as a standards-compliant
[Torznab](https://torznab.github.io/spec-1.3-draft/) feed.

Works natively with **Sonarr, Lidarr, Radarr, Prowlarr**, and any other
application that supports the Newznab/Torznab API.

---

## Features

| Feature               | Details                                                           |
| --------------------- | ----------------------------------------------------------------- |
| Torznab-compliant API | `caps`, `search`, `tvsearch`, `movie`, `music`, `book`            |
| Full category support | All ext.to categories mapped to Torznab IDs                       |
| TV search             | Query + season + episode auto-formatted (`S01E03`)                |
| IMDb search           | `imdbid=tt1234567` passes `imdb_id` directly to ext.to            |
| Magnet links          | Resolved on-demand via FlareSolverr — search returns instantly    |
| Adult content toggle  | `INCLUDE_ADULT=false` hides XXX results                           |
| Optional API key      | Protect the proxy with `API_KEY` environment variable             |
| Pagination            | Full `offset` / `limit` support                                   |
| Docker-ready          | Single `docker compose up -d`                                     |

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
      - EXT_TO_URL=https://ext.to
      - API_KEY=
      - INCLUDE_ADULT=true
      - FLARESOLVERR_TIMEOUT=60000
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
```

---

## Add to Sonarr / Radarr / Lidarr

1. **Settings → Indexers → Add → Torznab**
2. **URL:** `http://<your-host>:5000`
3. **API Key:** leave blank (or match the value set in `API_KEY`)
4. Click **Test** — should show a green tick.

---

## Configuration

| Variable               | Default                    | Description                              |
| ---------------------- | -------------------------- | ---------------------------------------- |
| `FLARESOLVERR_URL`     | `http://flaresolverr:8191` | URL of the FlareSolverr instance         |
| `EXT_TO_URL`           | `https://ext.to`           | ext.to base URL (set a mirror if needed) |
| `API_KEY`              | _(empty)_                  | Optional key required on all requests    |
| `PORT`                 | `5000`                     | Port the proxy listens on                |
| `HOST`                 | `0.0.0.0`                  | Bind address                             |
| `FLARESOLVERR_TIMEOUT` | `60000`                    | Max ms FlareSolverr waits per page       |
| `INCLUDE_ADULT`        | `true`                     | Include XXX categories in results        |
| `LOG_LEVEL`            | `INFO`                     | Logging verbosity                        |

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
        │  POST /v1  { cmd: "request.get", url: "https://ext.to/browse/?q=..." }
        ▼
  FlareSolverr
        │  Chrome headless – solves Cloudflare challenge
        ▼
  ext.to  (HTML parsed with BeautifulSoup)
        ▼
  Torznab RSS XML  →  returned to *arr
```

Magnet links are resolved **on demand**: search results are returned immediately
and the magnet URL is fetched only when the *arr app actually grabs a release
(via the `t=download` endpoint). This keeps search response times fast even on
a slow FlareSolverr instance.

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
