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
      a.vide-watch-btn[href]                    → show page, e.g. /the-rookie-s109/
    td[2]  span:not(.add-block)  → size text
    td[3]  span:not(.add-block)  → file count
    td[4]  span[title]           → exact date in title attr
    td[5]  span.text-success     → seeders
    td[6]  span.text-danger      → leechers
    td[7]  a.source-link-tor     → original tracker

Show pages (verified October 2026):
  /<show>/torrents/?s=4&completed=1  → every season-4 pack, incl. collections
  /<show>/torrents/?s=4&e=5          → S04E05 releases plus the packs holding it
  Same table markup, except the title link has no class and there is no
  related-posted block.  The page carries its own searchPageToken.

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
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlsplit

import lxml.html
from lxml.etree import ParserError
from lxml.html import HtmlElement

from .categories import EXT_TO_CAT_MAP, cat_id_matches, torznab_cat_to_site_cat
from .release_match import normalise, query_variants, split_matches, title_matches
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
# Pages read per query variant in a matched (season / episode) search.
_MATCHED_MAX_PAGES = 2
# How long a show's page path (e.g. /the-rookie-s109/) is remembered.
_SHOW_PATH_TTL = 24 * 3600
# Show page path linked from a result row's play button.
_RE_SHOW_PATH = re.compile(r"^/[a-z0-9-]+-s\d+/$")
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

# Detail-page elements that carry the numeric torrent ID, most specific first.
_DETAIL_ID_XPATHS = [
    f'//a[contains(concat(" ", normalize-space(@class), " "), " {cls} ")][@data-id]'
    for cls in ("search-magnet-btn", "download-btn-magnet", "detail-magnet-link")
] + ["//*[@data-id]"]

# Pre-compiled regex patterns used in hot paths (parsed 50× per search page).
_RE_HTML_TAGS      = re.compile(r"<[^>]+>")
# Quality badge show pages append to the tooltip title ("… x264 <span …>720P</span>").
_RE_TITLE_BADGE    = re.compile(r'<span class="imdbSearchButton">.*?</span>')
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


def _has_class(el: HtmlElement, cls: str) -> bool:
    return cls in (el.get("class") or "").split()


def _find(el: HtmlElement, tag: str, cls: Optional[str] = None) -> Optional[HtmlElement]:
    """First descendant <tag> (with class *cls*, if given)."""
    for child in el.iter(tag):
        if child is not el and (cls is None or _has_class(child, cls)):
            return child
    return None


def _text(el: HtmlElement) -> str:
    return el.text_content().strip()


def _value_span(td: HtmlElement) -> Optional[HtmlElement]:
    """First <span> in a cell that is not the mobile "Size"/"Files" label."""
    for span in td.iter("span"):
        if not _has_class(span, "add-block"):
            return span
    return None


def _parse_document(html: str) -> Optional[HtmlElement]:
    try:
        return lxml.html.document_fromstring(html)
    except ValueError:
        # A str with an XML encoding declaration has to go in as bytes.
        return lxml.html.document_fromstring(html.encode("utf-8"))
    except ParserError:
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


class _PageCache:
    """Parsed search pages, kept past freshness so they can be served stale.

    An entry younger than *fresh* answers without touching the site.  An
    older one (up to *stale*) is still handed out when the site is down or
    too slow to answer before the *arr timeout – old results beat none.
    """

    def __init__(self, fresh: int, stale: int, max_entries: int = 256) -> None:
        self.fresh = max(0, fresh)
        self.stale = max(self.fresh, stale)
        self._max = max_entries
        self._data: "OrderedDict[object, Tuple[float, object]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, max_age: float):
        """Return ``(age, value)`` for an entry at most *max_age* old, else ``None``."""
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            age = time.time() - entry[0]
            if age > self.stale:
                self._data.pop(key, None)
                return None
            if age > max_age:
                return None
            self._data.move_to_end(key)
            return age, entry[1]

    def set(self, key, value) -> None:
        if self.stale <= 0:
            return
        with self._lock:
            self._data[key] = (time.time(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def age(self, key) -> Optional[float]:
        with self._lock:
            entry = self._data.get(key)
            return time.time() - entry[0] if entry else None


def _pick_show_path(rows: List[Dict], title_query: str) -> Optional[str]:
    """Most common show page among rows whose title matches the query."""
    counts: Dict[str, int] = {}
    for row in rows:
        path = row.get("show_path")
        if not path or (title_query and not title_matches(row.get("title", ""), title_query)):
            continue
        counts[path] = counts.get(path, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _unique(items: List[Dict]) -> List[Dict]:
    seen: set = set()
    out = []
    for item in items:
        key = item.get("torrent_id") or item.get("details_path")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


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
        magnet_time_budget: float = 5.0,
        page_cache_stale: int = 0,
    ) -> None:
        self._pool = pool
        self._include_adult = include_adult
        self._resolve_mode = resolve_magnets
        self._magnet_workers = max(1, magnet_workers)
        self._magnet_workers_fs = max(1, magnet_workers_flaresolverr)
        self._magnet_cache = _TTLCache(magnet_cache_ttl)
        self._page_cache = _PageCache(page_cache_ttl, page_cache_stale)
        # Single-flight for page fetches: identical searches arriving while
        # one is on the wire (Sonarr + Radarr, or a retry after a timeout)
        # wait for it instead of fetching – and maybe solving – again.
        self._inflight: Dict[object, Future] = {}
        self._inflight_lock = threading.Lock()
        # Keyword-less pages (RSS sync, connection tests) and when they were
        # last asked for; the background keeper keeps these warm.
        self._feeds: Dict[object, Tuple[str, Dict[str, str], float]] = {}
        self._feeds_lock = threading.Lock()
        self._refresh_locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._max_resolve = max(0, magnet_max_resolve)
        self._time_budget = magnet_time_budget if magnet_time_budget > 0 else None
        self._enrich_paused_until = 0.0
        # Search key (normalised title or IMDb ID) → show page path.
        self._show_paths = _TTLCache(_SHOW_PATH_TTL, max_entries=1024)

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
        match_title: bool = False,
        cached_only: bool = False,
    ) -> List[Dict]:
        """Search the site.

        With *cached_only* nothing goes over the network: pages come from the
        cache, however stale, and magnets are only filled in from the magnet
        cache.  That is the answer given when a live search overruns its
        deadline.
        """
        query = query.strip()
        if season is not None and season >= 1000:
            # Daily shows: Sonarr sends season=YYYY and ep=MM/DD.  Search the
            # year as a keyword; SxxEyy matching does not apply.
            query = f"{query} {season}".strip()
            season = episode = None

        if season is None and not (match_title and query and not imdbid):
            return self._plain_search(query, categories, imdbid, offset, limit, cached_only)
        return self._matched_search(
            query, categories, imdbid, season, episode, offset, limit,
            match_title=match_title, cached_only=cached_only,
        )

    def _plain_search(
        self,
        query: str,
        categories: Optional[List[int]],
        imdbid: Optional[str],
        offset: int,
        limit: int,
        cached_only: bool = False,
    ) -> List[Dict]:
        """One query, results passed through as the site ranks them."""
        start_page = (offset // _RESULTS_PER_PAGE) + 1
        pages_needed = max(1, -(-limit // _RESULTS_PER_PAGE))

        all_results: List[Dict] = []
        client: Optional[SiteClient] = None

        for page in range(start_page, start_page + pages_needed):
            params = self._build_params(query, page, imdbid, categories)
            if cached_only:
                hit = self._cached_results("/browse/", params, max_age=self._page_cache.stale)
                if hit is None:
                    break
                page_results, client = hit
                all_results.extend(page_results)
                if len(page_results) < _RESULTS_PER_PAGE:
                    break
                continue
            if not query and not imdbid:
                with self._feeds_lock:
                    self._feeds[self._page_key("/browse/", params)] = ("/browse/", params, time.time())
            try:
                page_results, client = self._fetch_results("/browse/", params)
            except SiteUnavailable as exc:
                logger.error("Search page fetch failed: %s", exc)
                break

            all_results.extend(page_results)
            if len(page_results) < _RESULTS_PER_PAGE:
                break

        all_results = self._filter_categories(all_results, categories)

        local_offset = offset % _RESULTS_PER_PAGE if offset > 0 else 0
        results = all_results[local_offset: local_offset + limit]

        self._add_magnets(results, client, cached_only)
        return results

    def _add_magnets(
        self, results: List[Dict], client: Optional[SiteClient], cached_only: bool,
    ) -> None:
        if cached_only:
            for item in results:
                cached = self._magnet_cache.get(item.get("torrent_id"))
                if cached:
                    self._apply_magnet(item, cached)
        elif results and client is not None and self._should_resolve(client):
            self._enrich_with_magnets(results, client)

    def _rows(
        self, path: str, params: Dict[str, str], cached_only: bool,
    ) -> Tuple[List[Dict], Optional[SiteClient]]:
        """One results page; from the cache only (however stale) if *cached_only*."""
        if not cached_only:
            return self._fetch_results(path, params)
        hit = self._cached_results(path, params, max_age=self._page_cache.stale)
        return hit if hit is not None else ([], None)

    def _matched_search(
        self,
        query: str,
        categories: Optional[List[int]],
        imdbid: Optional[str],
        season: Optional[int],
        episode: Optional[int],
        offset: int,
        limit: int,
        match_title: bool,
        cached_only: bool = False,
    ) -> List[Dict]:
        """Structured search: keep only releases that match what was asked.

        TV searches go through the show's own page when one can be found:
        ``/<show>/torrents/?s=4&completed=1`` lists every season-4 pack and
        ``?s=4&e=5`` that episode plus the packs containing it, which the
        keyword search (sorted by age, flooded with single episodes) never
        surfaces.  The show path is read off the keyword search results and
        cached.  Without one, several keyword variants are run and filtered.

        Offsets apply to the filtered list.  Wanted releases (packs for a
        season search, the episode for an episode search) come first; the
        fallback kind is only returned when nothing wanted was found, so
        Sonarr still gets something it can grab.
        """
        variants = [query] if imdbid else query_variants(query, season, episode)
        title_query = query if match_title and not imdbid else ""
        wanted: List[Dict] = []
        fallback: List[Dict] = []
        client: Optional[SiteClient] = None

        show_key = (imdbid or normalise(query)) if season is not None else ""
        show_path = self._show_paths.get(show_key) if show_key else None
        pending = list(variants)

        if show_key and show_path is None:
            rows, hits, misses, client = self._search_variant(
                pending.pop(0), imdbid, categories, title_query, season, episode, cached_only,
            )
            wanted.extend(hits)
            fallback.extend(misses)
            show_path = _pick_show_path(rows, title_query)
            if show_path:
                self._show_paths.set(show_key, show_path)

        if show_path:
            rows, show_client = self._show_rows(show_path, season, episode, cached_only)
            if rows:
                client = client or show_client
                if episode is None:
                    # The site's own "complete season" list: trust it as is.
                    wanted.extend(rows)
                else:
                    hits, misses = split_matches(rows, "", season, episode)
                    wanted.extend(hits)
                    fallback.extend(misses)
                pending = []

        if pending:
            def run(variant: str):
                return self._search_variant(
                    variant, imdbid, categories, title_query, season, episode, cached_only,
                )

            with ThreadPoolExecutor(max_workers=len(pending)) as executor:
                for _rows, hits, misses, variant_client in executor.map(run, pending):
                    wanted.extend(hits)
                    fallback.extend(misses)
                    client = client or variant_client

        wanted = _unique(wanted)
        fallback = _unique(fallback)
        matched = wanted or fallback
        matched.sort(key=lambda r: r.get("seeders", 0), reverse=True)

        logger.info(
            "Matched search %r (season=%s, ep=%s, show=%s): %d wanted, %d fallback",
            query, season, episode, show_path or "-", len(wanted), len(fallback),
        )

        results = matched[offset: offset + limit]
        self._add_magnets(results, client, cached_only)
        return results

    def _search_variant(
        self,
        variant: str,
        imdbid: Optional[str],
        categories: Optional[List[int]],
        title_query: str,
        season: Optional[int],
        episode: Optional[int],
        cached_only: bool = False,
    ) -> Tuple[List[Dict], List[Dict], List[Dict], Optional[SiteClient]]:
        """Run one keyword query; return (all rows, wanted, fallback, client)."""
        rows_all: List[Dict] = []
        wanted: List[Dict] = []
        fallback: List[Dict] = []
        client: Optional[SiteClient] = None
        for page in range(1, _MATCHED_MAX_PAGES + 1):
            params = self._build_params(variant, page, imdbid, categories)
            try:
                rows, page_client = self._rows("/browse/", params, cached_only)
            except SiteUnavailable as exc:
                logger.error("Search page fetch failed for %r: %s", variant, exc)
                break
            client = client or page_client
            rows_all.extend(rows)
            rows = self._filter_categories(rows, categories)
            hits, misses = split_matches(rows, title_query, season, episode)
            wanted.extend(hits)
            fallback.extend(misses)
            # A page without a single wanted release means the variant has
            # run out of useful results; don't pay for another.
            if len(rows) < _RESULTS_PER_PAGE or not hits:
                break
        return rows_all, wanted, fallback, client

    def _show_rows(
        self, show_path: str, season: int, episode: Optional[int], cached_only: bool = False,
    ) -> Tuple[List[Dict], Optional[SiteClient]]:
        """Torrents listed on a show page for one season or one episode."""
        params = {"s": str(season)}
        if episode is None:
            params["completed"] = "1"
        else:
            params["e"] = str(episode)
        try:
            rows, client = self._rows(f"{show_path}torrents/", params, cached_only)
        except SiteUnavailable as exc:
            logger.error("Show page fetch failed for %s: %s", show_path, exc)
            return [], None
        for row in rows:
            # Show-page rows carry no category links; they are TV by definition.
            if row.get("categories") == [8000]:
                row["categories"] = [5000]
        return rows, client

    @staticmethod
    def _filter_categories(results: List[Dict], categories: Optional[List[int]]) -> List[Dict]:
        # ext.to filters top-level categories server-side via cat=N, but
        # sub-category requests (e.g. 5040 "TV/HD") still need to be narrowed
        # here.
        if not categories or not results:
            return results
        filtered = [
            r for r in results
            if any(
                cat_id_matches(rc, fc)
                for rc in r.get("categories", [8000])
                for fc in categories
            )
        ]
        # Keep the unfiltered set if our mapping recognised nothing –
        # returning zero results would look like an outage to *arr apps.
        return filtered or results

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

    @staticmethod
    def _page_key(path: str, params: Dict[str, str]) -> Tuple:
        return (path, tuple(sorted(params.items())))

    def _cached_results(
        self, path: str, params: Dict[str, str], max_age: float
    ) -> Optional[Tuple[List[Dict], SiteClient]]:
        hit = self._page_cache.get(self._page_key(path, params), max_age)
        if hit is None:
            return None
        age, (results, client) = hit
        logger.debug("Page cache hit (%.0fs old): %s %s", age, path, params)
        # Copies, so magnet enrichment on one response never leaks into the next.
        return [dict(r) for r in results], client

    def _fetch_results(
        self, path: str, params: Dict[str, str], force: bool = False
    ) -> Tuple[List[Dict], SiteClient]:
        """Fetch and parse one search page, caching the *parsed* rows.

        Caching rows instead of the raw page keeps a cache entry at a few KB
        rather than the ~1 MB of HTML the site serves, and a cache hit skips
        the parse as well as the fetch.  *force* skips the freshness check
        (background refresh).  When every mirror fails, a stale copy is
        returned rather than nothing.
        """
        key = self._page_key(path, params)
        if not force:
            hit = self._cached_results(path, params, max_age=self._page_cache.fresh)
            if hit is not None:
                return hit

        with self._inflight_lock:
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._inflight[key] = future

        if owner:
            try:
                logger.info("Fetching %s", self._pool.active.url(path, params))
                html, client = self._pool.get(path, params)
                results = self._parse_html(html, client)
                self._page_cache.set(key, (results, client))
                future.set_result((results, client))
            except BaseException as exc:
                future.set_exception(exc)
            finally:
                with self._inflight_lock:
                    self._inflight.pop(key, None)
        else:
            logger.debug("Joining in-flight fetch: %s %s", path, params)

        try:
            results, client = future.result()
        except SiteUnavailable:
            stale = self._cached_results(path, params, max_age=self._page_cache.stale)
            if stale is None:
                raise
            logger.warning(
                "Site unavailable; serving cached page (%.0fs old) for %s",
                self._page_cache.age(key) or 0, params,
            )
            return stale
        return [dict(r) for r in results], client

    # -----------------------------------------------------------------------
    # Background upkeep
    # -----------------------------------------------------------------------

    def warm_up(self) -> SiteClient:
        """Fetch the default feed: picks a mirror, solves if needed, fills the cache.

        It is the page a keyword-less TV request (Sonarr's connection test)
        asks for, so that test is answered from the cache afterwards.
        """
        _results, client = self._fetch_results("/browse/", self._default_feed_params(), force=True)
        return client

    def _default_feed_params(self) -> Dict[str, str]:
        return self._build_params("", 1, None, [5000])

    def refresh_feeds(self, max_age: float, keep_for: float = 86400.0, limit: int = 8) -> int:
        """Re-fetch keyword-less pages asked for recently whose copy is *max_age* old.

        This is what *arr RSS syncs and connection tests ask for, over and
        over; refreshing them in the background keeps those answers instant
        and, as a side effect, keeps the Cloudflare clearance warm – an
        expired one gets re-solved here instead of inside a live request.
        With no feed known yet, the default TV feed stands in.  Returns the
        number of pages fetched.
        """
        now = time.time()
        with self._feeds_lock:
            for key, (_p, _params, last) in list(self._feeds.items()):
                if now - last > keep_for:
                    self._feeds.pop(key, None)
            feeds = sorted(self._feeds.items(), key=lambda kv: kv[1][2], reverse=True)[:limit]
        if not feeds:
            params = self._default_feed_params()
            feeds = [(self._page_key("/browse/", params), ("/browse/", params, now))]

        fetched = 0
        for key, (path, params, _last) in feeds:
            age = self._page_cache.age(key)
            if age is not None and age < max_age:
                continue
            try:
                self._fetch_results(path, params, force=True)
                fetched += 1
            except SiteUnavailable as exc:
                logger.warning("Background refresh failed for %s: %s", params, exc)
                break
        return fetched

    # -----------------------------------------------------------------------
    # Parsing
    # -----------------------------------------------------------------------

    def _parse_html(self, html: str, client: Optional[SiteClient] = None) -> List[Dict]:
        if not html:
            return []
        if "522: Connection timed out" in html or "524: A timeout occurred" in html:
            logger.warning("Mirror returned a Cloudflare error page")
            return []

        # lxml builds the whole ~1 MB page in C in ~5 ms; BeautifulSoup took
        # 25-70 ms even when told to keep only the <table> subtrees.
        doc = _parse_document(html)
        table = _find(doc, "table", "table-striped") if doc is not None else None
        if table is None:
            # A genuine zero-hit search renders the filter sidebar with a
            # "No results" button and no table – not a scraping failure.
            if "No results" in html:
                logger.info("Search returned no results")
            else:
                logger.warning("Result table not found. Snippet: %s", html[:400].replace("\n", " "))
            return []

        base = (client or self._pool.active).base
        results = []
        for row in table.xpath("./tbody/tr | ./tr"):
            try:
                item = self._parse_row(row, base)
                if item:
                    results.append(item)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed row: %s", exc)

        logger.debug("Parsed %d results", len(results))
        return results

    def _parse_row(self, row: HtmlElement, base: str) -> Optional[Dict]:
        tds = row.findall("td")
        if not tds:
            return None
        td1 = tds[0]

        title_link = _find(td1, "a", "torrent-title-link")
        if title_link is None:
            # Show pages (/<show>/torrents/) use a plain link inside the
            # float-left block.
            block = _find(td1, "div", "float-left")
            title_link = next((a for a in block.iter("a") if a.get("href")), None) if block is not None else None
        if title_link is None:
            return None

        # data-tooltip holds the full untruncated title; search hit highlighting
        # wraps matches in <span>, so strip tags.
        tooltip = _RE_TITLE_BADGE.sub("", title_link.get("data-tooltip", ""))
        title = _RE_HTML_TAGS.sub("", tooltip).strip()
        if not title:
            title = _text(title_link)
        if not title:
            return None

        details_href = title_link.get("href", "") or "/"
        details_path = details_href if details_href.startswith("/") else urlsplit(details_href).path

        # Category: build a lookup key from the related-posted links.
        # Links are [uploader, parent-cat, sub-cat?]; uploader links point at
        # /user/… and are skipped.  Key = parent + sub concatenated.
        related = _find(td1, "div", "related-posted")
        cat_id = 8000
        if related is not None:
            cat_links = [
                a.get("href", "")
                for a in related.iter("a")
                if a.get("href", "").startswith("/")
                and not a.get("href", "").startswith("/user/")
            ]
            if len(cat_links) >= 2:
                key = cat_links[0] + cat_links[1]
                cat_id = EXT_TO_CAT_MAP.get(key, EXT_TO_CAT_MAP.get(cat_links[0], 8000))
            elif len(cat_links) == 1:
                cat_id = EXT_TO_CAT_MAP.get(cat_links[0], 8000)

        show_path = None
        watch_btn = _find(td1, "a", "vide-watch-btn")
        if watch_btn is not None and _RE_SHOW_PATH.match(watch_btn.get("href", "")):
            show_path = watch_btn.get("href")

        magnet_btn = _find(td1, "a", "search-magnet-btn")
        torrent_id = None
        if magnet_btn is not None:
            try:
                torrent_id = int(magnet_btn.get("data-id", ""))
            except (ValueError, TypeError):
                torrent_id = None
        if torrent_id is None:
            torrent_id = _torrent_id_from_url(details_path)

        def cell(index: int) -> Optional[HtmlElement]:
            return tds[index] if len(tds) > index else None

        size = 0
        td2 = cell(1)
        if td2 is not None:
            val = _value_span(td2)
            if val is not None:
                size = _parse_size(_text(val))

        files = 1
        td3 = cell(2)
        if td3 is not None:
            val = _value_span(td3)
            if val is not None:
                files = max(1, _parse_int(_text(val)))

        # Date – prefer the exact date carried in the title attribute
        pub_date = ""
        td4 = cell(3)
        if td4 is not None:
            span_t = next((sp for sp in td4.iter("span") if sp.get("title") is not None), None)
            pub_date = _parse_date(span_t.get("title") if span_t is not None else _text(td4))

        seeders = 0
        td5 = cell(4)
        if td5 is not None:
            val = _find(td5, "span", "text-success")
            if val is not None:
                seeders = _parse_int(_text(val))

        leechers = 0
        td6 = cell(5)
        if td6 is not None:
            val = _find(td6, "span", "text-danger")
            if val is not None:
                leechers = _parse_int(_text(val))

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
            "show_path": show_path,
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

        def worker(item: Dict) -> Optional[str]:
            # ext.to throttles bursts of magnet requests.  Once enough calls
            # fail, stop hammering it: the remaining items keep their lazy
            # t=download link, which resolves fine one at a time.
            if failures.value >= _ENRICH_FAILURE_LIMIT:
                return None
            # The batch was started because the transport was direct; if a
            # challenge flipped it to FlareSolverr mid-batch, don't push the
            # rest through the browser one POST at a time - that blocks the
            # shared tab for the next real search.  Grabs still resolve lazily.
            if self._resolve_mode == "auto" and client.transport != "direct":
                return None
            magnet = self._request_magnet(client, item["torrent_id"], item.get("title", ""), attempts=2)
            if not magnet:
                failures.increment()
            return magnet

        # Not a ``with`` block: the executor must not wait for stragglers.
        # Lookups still running when the budget is spent finish on their own
        # and land in the magnet cache, so the eventual grab is a cache hit.
        pool = ThreadPoolExecutor(
            max_workers=min(workers, len(pending)), thread_name_prefix="magnet"
        )
        futures = {pool.submit(worker, item): item for item in pending}
        done, still_running = wait(futures, timeout=self._time_budget)
        pool.shutdown(wait=False)

        for future in done:
            try:
                magnet = future.result()
            except Exception as exc:  # noqa: BLE001 – one bad lookup must not sink the response
                logger.warning("Magnet lookup crashed for id=%s: %s", futures[future].get("torrent_id"), exc)
                continue
            if magnet:
                self._apply_magnet(futures[future], magnet)

        resolved = sum(1 for r in results if r.get("magnet_url"))
        logger.info(
            "Resolved %d/%d magnet links in %.2fs (%s, %d workers%s)",
            resolved, len(results), time.time() - started, client.host, workers,
            f", {len(still_running)} still resolving in background" if still_running else "",
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
        doc = _parse_document(html) if html else None
        if doc is None:
            return None
        for xpath in _DETAIL_ID_XPATHS:
            for el in doc.xpath(xpath)[:1]:
                try:
                    return int(el.get("data-id", ""))
                except (ValueError, TypeError):
                    continue
        return None
