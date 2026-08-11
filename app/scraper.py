"""
ext.to HTML scraper (verified against the live site, August 2026).

Scraping strategy
─────────────────
1. Fetch the browse/search page through :class:`SitePool` — plain HTTP when
   the host allows it, FlareSolverr when Cloudflare challenges us, next mirror
   when the host is down.
2. Parse every result row with the current CSS selectors.
3. Harvest ``window.searchPageToken`` + the ``csrf-token`` meta tag from that
   same page and resolve magnet links through /ajax/getSearchMagnet.php.

HTML structure (verified August 2026):
  table.table-striped > tbody > tr
    td[1]
      a.torrent-title-link[href, data-tooltip]  → title + detail URL
      div.related-posted > a                    → category hrefs
      a.search-magnet-btn[data-id]              → numeric torrent ID
    td[2]  span:not(.add-block)  → size text
    td[3]  span:not(.add-block)  → file count
    td[4]  span[title]           → exact date in title attr
    td[5]  span.text-success     → seeders
    td[6]  span.text-danger      → leechers
    td[7]  a.source-link-tor     → original tracker

Magnet API (verified August 2026):
  POST /ajax/getSearchMagnet.php
  Fields: torrent_id, hash, name, timestamp, hmac, sessid
  hmac   = SHA256(f"{torrent_id}|{timestamp}|{searchPageToken}")
  sessid = the csrf-token meta value
  Returns { success, hash, url, error }

  The token is *not* bound to the torrents shown on the page it came from —
  any torrent_id can be resolved with it — but it *is* bound to the PHP
  session cookie, so the POST must reuse the cookie jar of the host that
  served the page.  /ajax/getTorrentMagnet.php only accepts a detail page's
  ``window.pageToken`` and is therefore no longer used.
"""

import hashlib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlsplit

from bs4 import BeautifulSoup, Tag

from .categories import EXT_TO_CAT_MAP, cat_id_matches, torznab_cat_to_site_cat
from .site_client import SiteClient, SitePool, SiteUnavailable, extract_js_tokens

logger = logging.getLogger(__name__)

_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
]

_RESULTS_PER_PAGE = 50
_MAGNET_API_PATH = "/ajax/getSearchMagnet.php"
# ext.to allows 60 magnet calls per PHP session, then answers "Too many
# requests. Please wait a moment." until a new session starts.  Rotate a few
# calls early so a batch never walks into that wall.
_SESSION_MAGNET_BUDGET = 55
# Abort bulk magnet resolution after this many failures.  Rotation normally
# prevents them, so this many in one batch means something else is wrong.
_ENRICH_FAILURE_LIMIT = 6
# How long bulk resolution stays disabled after that.  Individual grabs keep
# working during the pause – they retry with a fresh session of their own.
_ENRICH_PAUSE_SECONDS = 120

# Pre-compiled regex patterns used in hot paths (parsed 50× per search page).
_RE_HTML_TAGS      = re.compile(r"<[^>]+>")
_RE_INFOHASH       = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_RE_REL_TIME       = re.compile(r"(\d+)([smhdwMy])")
_RE_WORD_TIME      = re.compile(
    r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s*(?:ago)?",
    re.IGNORECASE,
)
_RE_SIZE           = re.compile(r"([\d.,]+)\s*(B|KB|MB|GB|TB)\b", re.IGNORECASE)
_RE_MAGNET_HASH    = re.compile(r"btih:([0-9a-fA-F]{40,64})")
_RE_GUID_INFOHASH  = re.compile(r"/t/(?:[^/]+-)?([0-9a-fA-F]{40,64})/")
# Numeric torrent ID at the end of an ext.to detail URL slug, e.g.
# https://ext.to/some-title-12345678/  →  12345678
_RE_URL_TORRENT_ID = re.compile(r"-(\d+)/?$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_hmac(torrent_id: int, timestamp: int, page_token: str) -> str:
    return hashlib.sha256(f"{torrent_id}|{timestamp}|{page_token}".encode()).hexdigest()


def _magnet_post_body(torrent_id: int, page_token: str, csrf_token: str) -> Dict[str, str]:
    ts = int(time.time())
    return {
        "torrent_id": str(torrent_id),
        "hash": "",
        "name": "",
        "timestamp": str(ts),
        "hmac": _compute_hmac(torrent_id, ts, page_token),
        "sessid": csrf_token,
    }


def _build_magnet(infohash: str, title: str) -> str:
    dn = quote_plus(title)
    tr_params = "".join(f"&tr={quote_plus(t)}" for t in _TRACKERS)
    return f"magnet:?xt=urn:btih:{infohash.lower()}&dn={dn}{tr_params}"


def _parse_size(raw: str) -> int:
    m = _RE_SIZE.match(raw.strip())
    if not m:
        return 0
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(value * mult.get(m.group(2).upper(), 1))


def _parse_int(raw: str) -> int:
    raw = raw.strip().replace(",", "").replace(".", "")
    try:
        return int(float(raw))
    except (ValueError, AttributeError):
        return 0


def _parse_date(raw: str) -> str:
    raw = raw.strip()
    now = datetime.now(timezone.utc)

    m = _RE_REL_TIME.fullmatch(raw)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "s": timedelta(seconds=n), "m": timedelta(minutes=n),
            "h": timedelta(hours=n),   "d": timedelta(days=n),
            "w": timedelta(weeks=n),   "M": timedelta(days=n * 30),
            "y": timedelta(days=n * 365),
        }.get(unit, timedelta(0))
        return (now - delta).strftime("%a, %d %b %Y %H:%M:%S +0000")

    m = _RE_WORD_TIME.match(raw)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {
            "second": timedelta(seconds=n), "minute": timedelta(minutes=n),
            "hour":   timedelta(hours=n),   "day":    timedelta(days=n),
            "week":   timedelta(weeks=n),   "month":  timedelta(days=n * 30),
            "year":   timedelta(days=n * 365),
        }.get(unit, timedelta(0))
        return (now - delta).strftime("%a, %d %b %Y %H:%M:%S +0000")

    for fmt in (
        "%d %B %Y", "%B %d, %Y", "%d %b %Y",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d-%m-%Y", "%m/%d/%Y", "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).strftime(
                "%a, %d %b %Y %H:%M:%S +0000"
            )
        except ValueError:
            pass

    return now.strftime("%a, %d %b %Y %H:%M:%S +0000")


def _torrent_id_from_url(url: str) -> Optional[int]:
    m = _RE_URL_TORRENT_ID.search(urlsplit(url).path.rstrip("/"))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


class _Counter:
    """Thread-safe counter used to abort a batch once the site throttles us."""

    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    @property
    def value(self) -> int:
        with self._lock:
            return self._value

    def increment(self) -> int:
        with self._lock:
            self._value += 1
            return self._value


class _TTLCache:
    """Small thread-safe TTL cache (no external dependency needed)."""

    def __init__(self, ttl: int, max_entries: int = 4096) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._data: Dict[object, Tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key):
        if self._ttl <= 0:
            return None
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            expires, value = entry
            if expires < time.time():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key, value) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            if len(self._data) >= self._max:
                now = time.time()
                stale = [k for k, (exp, _v) in self._data.items() if exp < now]
                for k in stale:
                    self._data.pop(k, None)
                if len(self._data) >= self._max:
                    self._data.clear()
            self._data[key] = (time.time() + self._ttl, value)


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

class ExtToScraper:
    """Scrapes search results and resolves magnet links across mirrors."""

    def __init__(
        self,
        pool: SitePool,
        include_adult: bool = True,
        resolve_magnets: str = "auto",
        magnet_workers: int = 8,
        magnet_workers_flaresolverr: int = 3,
        magnet_cache_ttl: int = 3600,
        page_cache_ttl: int = 120,
        magnet_max_resolve: int = 30,
    ) -> None:
        self._pool = pool
        self._include_adult = include_adult
        self._resolve_mode = resolve_magnets
        self._magnet_workers = max(1, magnet_workers)
        self._magnet_workers_fs = max(1, magnet_workers_flaresolverr)
        self._magnet_cache = _TTLCache(magnet_cache_ttl)
        self._page_cache = _TTLCache(page_cache_ttl, max_entries=64)
        self._refresh_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._max_resolve = max(0, magnet_max_resolve)
        self._enrich_paused_until = 0.0

    @property
    def pool(self) -> SitePool:
        return self._pool

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    def search(
        self,
        query: str = "",
        categories: Optional[List[int]] = None,
        imdbid: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        offset: int = 0,
        limit: int = 25,
    ) -> List[Dict]:
        effective_query = query.strip()
        if effective_query and season is not None:
            if episode is not None:
                effective_query = f"{effective_query} S{season:02d}E{episode:02d}"
            else:
                effective_query = f"{effective_query} S{season:02d}"

        start_page = (offset // _RESULTS_PER_PAGE) + 1
        pages_needed = max(1, -(-limit // _RESULTS_PER_PAGE))

        all_results: List[Dict] = []
        client: Optional[SiteClient] = None

        for page in range(start_page, start_page + pages_needed):
            params = self._build_params(effective_query, page, imdbid, categories)
            try:
                html, client = self._fetch_page("/browse/", params)
            except SiteUnavailable as exc:
                logger.error("Search page fetch failed: %s", exc)
                break

            page_results = self._parse_html(html, client)
            all_results.extend(page_results)
            if len(page_results) < _RESULTS_PER_PAGE:
                break

        # Category filter.  ext.to filters top-level categories server-side via
        # cat=N, but sub-category requests (e.g. 5040 "TV/HD") still need to be
        # narrowed here.
        if categories and all_results:
            filtered = [
                r for r in all_results
                if any(
                    cat_id_matches(rc, fc)
                    for rc in r.get("categories", [8000])
                    for fc in categories
                )
            ]
            # Keep the unfiltered set if our mapping recognised nothing –
            # returning zero results would look like an outage to *arr apps.
            if filtered:
                all_results = filtered

        local_offset = offset % _RESULTS_PER_PAGE if offset > 0 else 0
        results = all_results[local_offset: local_offset + limit]

        if client is not None and self._should_resolve(client):
            self._enrich_with_magnets(results, client)

        return results

    def _build_params(
        self,
        query: str,
        page: int,
        imdbid: Optional[str],
        categories: Optional[List[int]],
    ) -> Dict[str, str]:
        params: Dict[str, str] = {"sort": "age", "order": "desc"}
        if imdbid:
            params["imdb_id"] = imdbid if imdbid.startswith("tt") else f"tt{imdbid}"
        elif query:
            params["q"] = query

        # A single top-level category can be pushed down to the site itself.
        site_cats = {
            c for c in (torznab_cat_to_site_cat(cat) for cat in (categories or [])) if c
        }
        if len(site_cats) == 1:
            params["cat"] = str(site_cats.pop())
        elif not query and not imdbid:
            # Keywordless request with no usable category (Prowlarr/Sonarr's
            # connection test): /browse/ without q or cat redirects to
            # /advanced/, so ask for the newest TV releases instead.
            params["cat"] = "2"

        if self._include_adult:
            params["with_adult"] = "1"
        if page > 1:
            params["page"] = str(page)
        return params

    def _fetch_page(self, path: str, params: Dict[str, str]) -> Tuple[str, SiteClient]:
        key = (path, tuple(sorted(params.items())))
        cached = self._page_cache.get(key)
        if cached is not None:
            html, client = cached
            logger.debug("Page cache hit: %s %s", path, params)
            return html, client

        logger.info("Fetching %s", self._pool.active.url(path, params))
        html, client = self._pool.get(path, params)
        self._page_cache.set(key, (html, client))
        return html, client

    # -----------------------------------------------------------------------
    # Parsing
    # -----------------------------------------------------------------------

    def _parse_html(self, html: str, client: Optional[SiteClient] = None) -> List[Dict]:
        if not html:
            return []
        if "522: Connection timed out" in html or "524: A timeout occurred" in html:
            logger.warning("Mirror returned a Cloudflare error page")
            return []

        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("table.table-striped")
        if not table:
            # A genuine zero-hit search renders the filter sidebar with a
            # "No results" button and no table – not a scraping failure.
            if "No results" in html:
                logger.info("Search returned no results")
            else:
                logger.warning("Result table not found. Snippet: %s", html[:400].replace("\n", " "))
            return []

        base = (client or self._pool.active).base
        results = []
        for row in table.select("tbody > tr"):
            try:
                item = self._parse_row(row, base)
                if item:
                    results.append(item)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed row: %s", exc)

        logger.debug("Parsed %d results", len(results))
        return results

    def _parse_row(self, row: Tag, base: str) -> Optional[Dict]:
        td1 = row.select_one("td:nth-child(1)")
        if not td1:
            return None

        title_link = td1.select_one("a.torrent-title-link")
        if not title_link:
            return None

        # data-tooltip holds the full untruncated title; search hit highlighting
        # wraps matches in <span>, so strip tags.
        title = _RE_HTML_TAGS.sub("", title_link.get("data-tooltip", "")).strip()
        if not title:
            title = title_link.get_text(strip=True)
        if not title:
            return None

        details_href = title_link.get("href", "") or "/"
        details_path = details_href if details_href.startswith("/") else urlsplit(details_href).path

        # Category: build a lookup key from the related-posted links.
        # Links are [uploader, parent-cat, sub-cat?]; uploader links point at
        # /user/… and are skipped.  Key = parent + sub concatenated.
        related = td1.select_one("div.related-posted")
        cat_id = 8000
        if related:
            cat_links = [
                a.get("href", "")
                for a in related.find_all("a")
                if a.get("href", "").startswith("/")
                and not a.get("href", "").startswith("/user/")
            ]
            if len(cat_links) >= 2:
                key = cat_links[0] + cat_links[1]
                cat_id = EXT_TO_CAT_MAP.get(key, EXT_TO_CAT_MAP.get(cat_links[0], 8000))
            elif len(cat_links) == 1:
                cat_id = EXT_TO_CAT_MAP.get(cat_links[0], 8000)

        magnet_btn = td1.select_one("a.search-magnet-btn")
        torrent_id = None
        if magnet_btn:
            try:
                torrent_id = int(magnet_btn.get("data-id", ""))
            except (ValueError, TypeError):
                torrent_id = None
        if torrent_id is None:
            torrent_id = _torrent_id_from_url(details_path)

        size = 0
        td2 = row.select_one("td:nth-child(2)")
        if td2:
            val = td2.select_one("span:not(.add-block)")
            if val:
                size = _parse_size(val.get_text(strip=True))

        files = 1
        td3 = row.select_one("td:nth-child(3)")
        if td3:
            val = td3.select_one("span:not(.add-block)")
            if val:
                files = max(1, _parse_int(val.get_text(strip=True)))

        # Date – prefer the exact date carried in the title attribute
        pub_date = ""
        td4 = row.select_one("td:nth-child(4)")
        if td4:
            span_t = td4.select_one("span[title]")
            pub_date = _parse_date(span_t["title"] if span_t else td4.get_text(strip=True))

        seeders = 0
        td5 = row.select_one("td:nth-child(5)")
        if td5:
            val = td5.select_one("span.text-success")
            if val:
                seeders = _parse_int(val.get_text(strip=True))

        leechers = 0
        td6 = row.select_one("td:nth-child(6)")
        if td6:
            val = td6.select_one("span.text-danger")
            if val:
                leechers = _parse_int(val.get_text(strip=True))

        details_url = base.rstrip("/") + details_path

        return {
            "title": title,
            "guid": details_url,
            "details_url": details_url,
            "details_path": details_path,
            "download_url": details_url,
            "magnet_url": "",
            "infohash": "",
            "torrent_id": torrent_id,
            "categories": [cat_id],
            "size": size,
            "files": files,
            "pub_date": pub_date,
            "seeders": seeders,
            "leechers": leechers,
            "peers": seeders + leechers,
        }

    # -----------------------------------------------------------------------
    # Magnet resolution
    # -----------------------------------------------------------------------

    def _should_resolve(self, client: SiteClient) -> bool:
        if self._resolve_mode == "never":
            return False
        if self._resolve_mode == "always":
            return True
        # "auto": only worth it on the fast transport – FlareSolverr serialises
        # every POST through one browser and would blow the *arr search timeout.
        return client.transport == "direct"

    def _refresh_lock(self, client: SiteClient) -> threading.Lock:
        with self._locks_guard:
            return self._refresh_locks.setdefault(client.base, threading.Lock())

    def _tokens_for(
        self,
        client: SiteClient,
        stale_after: Optional[float] = None,
        rotate_session: bool = False,
    ) -> Tuple[str, str]:
        """Return usable ``(page_token, csrf_token)`` for *client*.

        Tokens come from any search page, work for any torrent_id, and are
        bound to the client's cookie jar.  Pass *stale_after* (a marker from
        :meth:`SiteClient.tokens_marker`) to demand tokens newer than the ones
        that just failed; concurrent workers then share a single refresh
        instead of each fetching their own page.  With *rotate_session* the
        PHP session is dropped first, which resets the site's per-session
        magnet quota.
        """
        def usable() -> Optional[Tuple[str, str]]:
            token, csrf = client.cached_tokens()
            if token and csrf and (stale_after is None or client.tokens_marker() > stale_after):
                return token, csrf
            return None

        got = usable()
        if got:
            return got

        with self._refresh_lock(client):
            got = usable()
            if got:
                return got
            if rotate_session:
                client.rotate_site_session()
            client.invalidate_tokens()
            try:
                client.get("/browse/", {"cat": "2", "sort": "age", "order": "desc"})
            except SiteUnavailable as exc:
                logger.warning("Token refresh failed on %s: %s", client.host, exc)
                return "", ""
            token, csrf = client.cached_tokens()
            if not (token and csrf):
                logger.warning("No page tokens found on %s", client.host)
            return token, csrf

    def _request_magnet(
        self,
        client: SiteClient,
        torrent_id: int,
        title: str = "",
        attempts: int = 3,
    ) -> Optional[str]:
        """Resolve one magnet link, retrying through session rotation + backoff.

        The API rejects a call both when the page token expired and when the
        PHP session used up its magnet quota ("Too many requests. Please wait a
        moment."), so a single attempt is unreliable — this is what used to make
        the first grab of a release fail while a retry a moment later succeeded.
        Each retry starts a fresh site session, which resets that quota.
        """
        stale_after: Optional[float] = None
        rotate = False
        delay = 0.4

        for attempt in range(attempts):
            marker = client.tokens_marker()
            # Rotate a step before the quota runs out instead of eating a
            # guaranteed failure at call 61.
            if client.api_calls >= _SESSION_MAGNET_BUDGET:
                rotate = True
            token, csrf = self._tokens_for(
                client,
                stale_after=marker if rotate else stale_after,
                rotate_session=rotate,
            )
            if not token or not csrf:
                return None
            rotate = False

            try:
                data = client.post_json(_MAGNET_API_PATH, _magnet_post_body(torrent_id, token, csrf))
            except SiteUnavailable as exc:
                logger.debug("Magnet API unreachable id=%s: %s", torrent_id, exc)
                return None

            if data.get("success"):
                magnet = data.get("url") or ""
                infohash = (data.get("hash") or "").lower()
                if not magnet and _RE_INFOHASH.fullmatch(infohash):
                    magnet = _build_magnet(infohash, title)
                if magnet:
                    self._magnet_cache.set(torrent_id, magnet)
                    return magnet
                return None

            error = str(data.get("error", "")) or "no error text"
            if attempt + 1 >= attempts:
                logger.debug("Magnet API gave up on id=%s: %s", torrent_id, error)
                return None

            logger.debug("Magnet API retry %d for id=%s (%s)", attempt + 1, torrent_id, error)
            stale_after = marker
            rotate = True  # the session, not just the token, is likely spent
            time.sleep(delay)
            delay *= 2

        return None

    def _enrich_with_magnets(self, results: List[Dict], client: SiteClient) -> None:
        """Resolve magnet links for *results* concurrently.

        Handing *arr apps a real magnet in the search response removes the
        second round-trip through t=download, which is where intermittent
        "grab failed" errors came from.
        """
        pending = []
        for item in results:
            torrent_id = item.get("torrent_id")
            if not torrent_id:
                continue
            cached = self._magnet_cache.get(torrent_id)
            if cached:
                self._apply_magnet(item, cached)
            else:
                pending.append(item)

        if not pending:
            return

        if time.time() < self._enrich_paused_until:
            logger.info("Bulk magnet resolution paused (site throttling); links stay lazy")
            return

        if len(pending) > self._max_resolve:
            # *arr apps grab at most a couple of releases per search, so
            # resolving every row would spend the rate budget for nothing.
            logger.info(
                "Resolving magnets for the first %d of %d results; the rest resolve on grab",
                self._max_resolve, len(pending),
            )
            pending = pending[: self._max_resolve]

        # Warm the token cache once so the workers don't all miss and each
        # trigger their own page fetch.
        token, csrf = self._tokens_for(client)
        if not token or not csrf:
            logger.warning("No magnet tokens available on %s; leaving links lazy", client.host)
            return

        workers = self._magnet_workers if client.transport == "direct" else self._magnet_workers_fs
        started = time.time()
        failures = _Counter()

        def worker(item: Dict) -> None:
            # ext.to throttles bursts of magnet requests.  Once enough calls
            # fail, stop hammering it: the remaining items keep their lazy
            # t=download link, which resolves fine one at a time.
            if failures.value >= _ENRICH_FAILURE_LIMIT:
                return
            magnet = self._request_magnet(client, item["torrent_id"], item.get("title", ""), attempts=2)
            if magnet:
                self._apply_magnet(item, magnet)
            else:
                failures.increment()

        with ThreadPoolExecutor(max_workers=min(workers, len(pending))) as pool:
            list(pool.map(worker, pending))

        resolved = sum(1 for r in results if r.get("magnet_url"))
        logger.info(
            "Resolved %d/%d magnet links in %.2fs (%s, %d workers)",
            resolved, len(results), time.time() - started, client.host, workers,
        )
        if failures.value >= _ENRICH_FAILURE_LIMIT:
            self._enrich_paused_until = time.time() + _ENRICH_PAUSE_SECONDS
            logger.warning(
                "Magnet API throttled after %d failures; pausing bulk resolution for %ds",
                failures.value, _ENRICH_PAUSE_SECONDS,
            )

    @staticmethod
    def _apply_magnet(item: Dict, magnet: str) -> None:
        item["magnet_url"] = magnet
        item["download_url"] = magnet
        m = _RE_MAGNET_HASH.search(magnet)
        if m:
            item["infohash"] = m.group(1).lower()

    # -----------------------------------------------------------------------
    # On-demand magnet fetch (used by t=download)
    # -----------------------------------------------------------------------

    def fetch_magnet_for_guid(self, guid: str) -> Optional[str]:
        """Return a magnet URL for *guid* (a detail page URL, or a magnet).

        Order of attempts:
        1. guid already is a magnet                       → return as-is
        2. guid embeds an infohash (/t/<hash>/)           → build magnet
        3. torrent ID from the URL slug + cached tokens   → one POST
        4. detail page load to recover the torrent ID     → one POST
        """
        if not guid:
            return None
        if guid.startswith("magnet:"):
            return guid

        m = _RE_GUID_INFOHASH.search(guid)
        if m:
            return f"magnet:?xt=urn:btih:{m.group(1).lower()}"

        torrent_id = _torrent_id_from_url(guid)
        if torrent_id:
            cached = self._magnet_cache.get(torrent_id)
            if cached:
                logger.info("t=download: cache hit for id=%s", torrent_id)
                return cached

        # Torrent IDs are the same on every mirror, so a guid pointing at a
        # host that has since gone down can still be resolved elsewhere.
        errors = []
        candidates = self._pool.candidates()

        if torrent_id:
            for client in candidates:
                magnet = self._request_magnet(client, torrent_id)
                if magnet:
                    logger.info("t=download: resolved id=%s via %s", torrent_id, client.host)
                    return magnet
                errors.append(f"{client.host}: magnet API returned nothing for id={torrent_id}")

        # Fall back to reading the ID off the detail page itself.
        path = urlsplit(guid).path or "/"
        for client in candidates:
            try:
                html = client.get(path)
            except SiteUnavailable as exc:
                errors.append(str(exc))
                continue

            found = self._torrent_id_from_detail_page(html)
            if not found or found == torrent_id:
                errors.append(f"{client.host}: no usable torrent id on detail page")
                continue
            magnet = self._request_magnet(client, found)
            if magnet:
                logger.info("t=download: resolved id=%s (detail page) via %s", found, client.host)
                return magnet
            errors.append(f"{client.host}: detail page id={found} did not resolve")

        logger.warning("t=download: could not resolve %s (%s)", guid, "; ".join(errors))
        return None

    @staticmethod
    def _torrent_id_from_detail_page(html: str) -> Optional[int]:
        soup = BeautifulSoup(html, "html.parser")
        for selector in (
            "a.search-magnet-btn[data-id]",
            "a.download-btn-magnet[data-id]",
            "a.detail-magnet-link[data-id]",
            "[data-id]",
        ):
            el = soup.select_one(selector)
            if el:
                try:
                    return int(el.get("data-id", ""))
                except (ValueError, TypeError):
                    continue
        return None
