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
| Fast path             | Plain HTTP when a mirror allows it; a browser only when challenged   |
| Solver choice         | FlareSolverr or [Byparr](https://github.com/ThePhaseless/Byparr), auto-detected |
| Magnet links          | Resolved during search, concurrently, and cached                     |
| Never times out \*arr | Answers within `SEARCH_DEADLINE`, from a stale cache if need be      |
| Warm feeds            | RSS/test feeds and the Cloudflare clearance are refreshed in the background |
| One solve per burst   | Concurrent requests that hit a challenge share a single browser solve |
| Full category support | All ext.to categories mapped to Torznab IDs, filtered server-side    |
| TV search             | Season search returns the show page's complete-season packs; episode search the episode's releases; wrong show / season / episode filtered out |
| Free-text seasons     | `Show S04`, `Show Season 4 complete` get the same season-pack handling |
| IMDb search           | `imdbid=tt1234567` passes `imdb_id` directly to ext.to               |
| Adult content toggle  | `INCLUDE_ADULT=false` hides XXX results                              |
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
      # Optional: route both the direct client and the FlareSolverr browser
      # through the same HTTP(S) proxy.
      # - PROXY_URL=http://127.0.0.1:1080
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
| `FLARESOLVERR_URL` | `http://flaresolverr:8191` | FlareSolverr or Byparr instance; empty disables it |
| `PROXY_URL` | _(empty)_ | Optional HTTP(S) proxy for both the direct client and the FlareSolverr browser. Both must use the same egress IP. The URL must be reachable from both the app and the FlareSolverr container; if FlareSolverr is on a bridge, use the bridge gateway, not `127.0.0.1`. FlareSolverr only; Byparr uses its own `PROXY_SERVER`. |
| `FLARESOLVERR_BACKEND` | `auto` | `flaresolverr`, `byparr`, or `auto` (probes `GET /` once: Byparr redirects to `/docs`) |
| `HTTP_TIMEOUT` | `20` | Seconds before a plain HTTP request gives up |
| `MIRROR_COOLDOWN` | `300` | Seconds a failed mirror is skipped |
| `RESOLVE_MAGNETS` | `auto` | Resolve magnets during search: `auto` (direct transport only), `always`, `never` |
| `MAGNET_WORKERS` | `12` | Concurrent magnet lookups on the direct transport |
| `MAGNET_WORKERS_FLARESOLVERR` | `3` | Concurrent magnet lookups through FlareSolverr |
| `MAGNET_MAX_RESOLVE` | `30` | Magnets resolved per search before the rest go lazy |
| `MAGNET_TIME_BUDGET` | `5` | Max seconds a search waits for magnets; slower lookups finish in the background and the rows keep their `t=download` link |
| `MAGNET_CACHE_TTL` | `3600` | Seconds a resolved magnet stays cached |
| `PAGE_CACHE_TTL` | `300` | Seconds a fetched search page is answered from cache without asking the site (`0` disables) |
| `PAGE_CACHE_STALE` | `86400` | Seconds an older cached page may still be served, only when the site fails or overruns `SEARCH_DEADLINE` |
| `SEARCH_DEADLINE` | `25` | Most seconds a search waits on the site; past it the answer comes from the cache (empty if nothing is cached) and the fetch finishes in the background. `0` waits forever |
| `KEEPALIVE_INTERVAL` | `600` | Seconds between background refreshes of the keyword-less feeds \*arr apps poll (RSS sync, connection test); also re-solves an expired Cloudflare clearance before a real search needs it. `0` disables |
| `API_KEY` | _(empty)_ | Optional key required on all requests |
| `PORT` | `5000` | Port the proxy listens on |
| `HOST` | `0.0.0.0` | Bind address |
| `FLARESOLVERR_TIMEOUT` | `60000` | Max ms FlareSolverr waits per page. A Turnstile solve measured 27-57 s from a VPN exit; use `120000` when FlareSolverr egresses through a VPN |
| `FLARESOLVERR_TABS_TILL_VERIFY` | `0` | Leave at `0`. Any other value switches FlareSolverr into its Turnstile-*token* mode, which is meant for pages with an embedded widget; ext.to's challenge page is solved by FlareSolverr's normal challenge loop, which clicks the checkbox itself. FlareSolverr only |
| `FLARESOLVERR_SESSION_IDLE` | `300` | Seconds of inactivity before the FlareSolverr browser session is released (0 = keep forever). FlareSolverr only |
| `FLARESOLVERR_SESSION_TTL` | `60` | Minutes after which FlareSolverr itself expires our session (`session_ttl_minutes`), so a session orphaned by a crash cannot live forever (0 = disabled). FlareSolverr only |
| `INCLUDE_ADULT` | `true` | Include XXX categories in results |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

`GET /healthz` reports which mirror is active, which transport it uses,
which mirrors are in cooldown, how old each mirror's `cf_clearance` is
(`clearance_age_s`) and when a plain request last got through
(`last_direct_ok_s`).  When a clearance stops working the log says how long
it lasted (`cf_clearance worked for N min`).

---

## Speed: why \*arr apps used to time out, and what the proxy does about it

Measured September 2026: every mirror (`extto.com`, `ext.to`, `ext2.to`)
answers a fresh visitor with Cloudflare's **interactive** challenge – a
"Verify you are human" Turnstile checkbox – on every path.  Once a browser has
passed it, the resulting `cf_clearance` cookie works for the proxy's plain
HTTP requests as long as they carry the same User Agent and come from the
same IP; a search page then costs ~1 s.  Solving is the expensive part:

| Solve | Time |
| --- | --- |
| Browser that clicks the checkbox straight away | ~6 s |
| Waiting for the checkbox to pass on its own | never (timeout) |
| Byparr behind a VPN (see below) | 25-60 s |

Sonarr, Radarr, Lidarr and Prowlarr give up on an indexer request after
**100 s**, and every timeout or 5xx pushes the indexer into an escalating
back-off (1 min, 5 min, 15 min … 24 h).  An empty answer does not.  So the
proxy now makes sure a request never has to wait for a solve:

* **Deadline.** A search that has not finished after `SEARCH_DEADLINE`
  seconds is answered from the page cache – stale if need be, empty if there
  is nothing – and the fetch carries on in the background.  The \*arr app's
  next query (they retry and re-poll constantly) is then a cache hit.
* **Stale cache.** Pages stay usable for `PAGE_CACHE_STALE` seconds after they
  stop being fresh, and are served whenever the site fails.
* **Background keeper.** Every `KEEPALIVE_INTERVAL` seconds the keyword-less
  pages the \*arr apps poll for RSS sync and for the connection test are
  re-fetched.  That keeps those answers instant and, because it goes through
  the same HTTP session, notices an expired clearance and re-solves it in the
  background rather than inside somebody's search.
* **One solve per burst.** Requests that find the clearance expired queue
  behind a single solve and then retry with its cookies, instead of each
  launching its own browser one after the other.
* **Identical searches share one fetch** (Sonarr and Radarr asking the same
  thing at once, or a retry arriving while the first try is still running).

### Byparr: the 30-60 s solves are an IP lookup, not the challenge

Byparr's browser looks up its own public IP (`api.ipify.org`, then
`icanhazip.com`, then `checkip.amazonaws.com`, ~10 s timeout each) before every
launch.  Behind a VPN that blocks or slows those hosts this alone takes
25-46 s per request, while the solve itself takes 3-7 s
([Byparr#416](https://github.com/ThePhaseless/Byparr/issues/416)).  Until
that is fixed upstream, resolving **all three** hosts to nowhere cuts a solve
to a few seconds (blocking only the first made it *slower* in that report,
and the workaround must not be used when Byparr's own `PROXY_SERVER` is set):

```yaml
  byparr:
    image: ghcr.io/thephaseless/byparr:latest
    extra_hosts:
      - "api.ipify.org:127.0.0.1"
      - "icanhazip.com:127.0.0.1"
      - "checkip.amazonaws.com:127.0.0.1"
    environment:
      - TZ=Europe/Amsterdam   # the VPN exit's timezone: the lookup normally sets it
```

### FlareSolverr

Use a current release (Turnstile handling landed in 3.5.0).  Keep
`FLARESOLVERR_TABS_TILL_VERIFY=0` – see the table above.  On slow hardware
FlareSolverr's own `BROWSER_WAIT_TIMEOUT=3` avoids re-solve loops.

---

## Performance & memory

Measured September 2026 against the live site (amd64, Docker):

| What | Cost |
| --- | --- |
| Search page fetch (plain HTTP with a valid clearance) | ~1.0 s, of which ~0.75 s is the site's own time-to-first-byte for its 1 MB page |
| Search answered from the page cache | ~3 ms |
| Parsing one 50-row page | ~55 ms |
| Resolving 25 magnets (12 workers) | ~0.5 s |
| Cloudflare solve (browser that clicks the checkbox) | ~6 s, once per clearance |
| Proxy container, idle | ~45 MiB |
| FlareSolverr container, idle, no browser session | ~40 MiB |
| FlareSolverr container with an open session | ~285-355 MiB |

The mirrors reject plain HTTP even with a browser TLS fingerprint
(`curl_cffi` impersonating Chrome, Firefox or Safari), so a challenge solver is
required.  A browser fingerprint buys nothing once a clearance is held either:
plain `requests` with the solver's User Agent is accepted just the same.  The
solver's cost is kept low instead:

* When a challenge is solved, the resulting `cf_clearance` cookie is imported
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

### Byparr instead of FlareSolverr

[Byparr](https://github.com/ThePhaseless/Byparr) speaks the same `/v1` request
shape and is picked up automatically (`FLARESOLVERR_BACKEND=auto`), or pin it
with `FLARESOLVERR_BACKEND=byparr`.  Differences that matter:

* Byparr has no browser sessions: every solve launches a fresh Firefox and
  closes it again, so `FLARESOLVERR_SESSION_IDLE` / `FLARESOLVERR_SESSION_TTL`
  / `FLARESOLVERR_TABS_TILL_VERIFY` do nothing.  The solved `cf_clearance`
  still lands in the proxy's own HTTP session and is reused for every
  request after that.
* Byparr cannot POST through the browser.  When a magnet API call is
  challenged, the proxy solves `/browse/` once to refresh its cookies and
  retries the POST directly.  For this to work the proxy and Byparr must
  egress from the same IP (Cloudflare binds the cookie to it) – run them on
  the same host or in the same pod.
* Byparr errors arrive as HTTP 408/502 instead of `status: "error"`; the
  proxy retries once and then marks the mirror down like any other failure.

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
        ├─ 0. page cache (fresh → answer; site slow/down → stale answer)
        │
        ├─ 1. plain HTTPS GET /browse/?q=…      ← fast path (~1 s)
        │      └─ Cloudflare challenge?  → FlareSolverr solves it once,
        │         its cookies + User Agent are reused for the fast path;
        │         the background keeper usually got there first
        │      └─ host down?              → next mirror, failed host cooled down
        │
        ├─ 2. parse rows with lxml
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
