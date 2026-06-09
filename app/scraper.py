"""
ext.to HTML scraper (June 2026).

Scraping strategy
─────────────────
1. Fetch browse/search page via FlareSolverr (solves Cloudflare challenge).
2. Parse every result row with current CSS selectors.
3. Extract window.searchPageToken + csrf-token from the inline JS / meta tag.
4. POST /ajax/getSearchMagnet.php through FlareSolverr (same browser session)
   to get the infohash for each result.

HTML structure (verified June 2026):
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

Magnet API (verified June 2026):
  POST /ajax/getSearchMagnet.php  (search pages)
  POST /ajax/getTorrentMagnet.php (detail pages)
  Fields: torrent_id, hash, name, timestamp, hmac, sessid
  hmac  = SHA256(torrent_id + '|' + timestamp + '|' + searchPageToken)
  Returns { success, hash, url, error }
"""

import hashlib
import logging
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urlencode

from bs4 import BeautifulSoup, Tag

from .categories import CAT_ID_TO_BROWSE_PATH, EXT_TO_CAT_MAP, cat_id_matches
from .flaresolverr_client import FlareSolverrClient, FlareSolverrError

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
_DETAIL_MAGNET_API_PATH = "/ajax/getTorrentMagnet.php"
# How many magnet API requests to fire concurrently.
# FlareSolverr serialises POSTs through its single browser; above ~3 workers
# the internal queue grows and responses slow from ~0.35 s to ~1.3 s each.
_MAGNET_WORKERS = 3

# Pre-compiled regex patterns used in hot paths (parsed 50× per search page).
_RE_HTML_TAGS     = re.compile(r"<[^>]+>")
_RE_INFOHASH      = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_RE_REL_TIME      = re.compile(r"(\d+)([smhdwMy])")
_RE_WORD_TIME     = re.compile(
    r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s*(?:ago)?",
    re.IGNORECASE,
)
_RE_SEARCH_TOKEN  = re.compile(r"window\.searchPageToken\s*=\s*['\"]([a-fA-F0-9]+)['\"]")
_RE_PAGE_TOKEN    = re.compile(r"window\.pageToken\s*=\s*['\"]([a-fA-F0-9]+)['\"]")
_RE_CSRF          = re.compile(
    r'<meta\s[^>]*name=["\']csrf-token["\']\s[^>]*content=["\']([a-fA-F0-9]+)["\']'
)
_RE_CSRF_FALLBACK = re.compile(r'csrf-token[^>]*content=["\']([a-fA-F0-9]+)["\']')
_RE_SIZE          = re.compile(r"([\d.,]+)\s*(B|KB|MB|GB|TB)\b", re.IGNORECASE)
_RE_GUID_INFOHASH = re.compile(r"/t/(?:[^/]+-)?([0-9a-fA-F]{40,64})/")
# Numeric torrent ID at the end of an ext.to detail URL slug, e.g.
# https://ext.to/some-title-12345678/  →  12345678
_RE_URL_TORRENT_ID = re.compile(r"-(\d+)/?$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_hmac(torrent_id: int, timestamp: int, page_token: str) -> str:
    data = f"{torrent_id}|{timestamp}|{page_token}"
    return hashlib.sha256(data.encode()).hexdigest()


def _build_magnet_post(torrent_id: int, page_token: str, csrf_token: str) -> str:
    """Build URL-encoded POST body for the ext.to magnet API."""
    ts = int(time.time())
    return urlencode({
        "torrent_id": torrent_id,
        "hash": "",
        "name": "",
        "timestamp": ts,
        "hmac": _compute_hmac(torrent_id, ts, page_token),
        "sessid": csrf_token,
    })


def _build_magnet(infohash: str, title: str) -> str:
    dn = quote_plus(title)
    tr_params = "".join(f"&tr={quote_plus(t)}" for t in _TRACKERS)
    return f"magnet:?xt=urn:btih:{infohash.lower()}&dn={dn}{tr_params}"


def _parse_size(raw: str) -> int:
    raw = raw.strip()
    m = _RE_SIZE.match(raw)
    if not m:
        return 0
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    unit = m.group(2).upper()
    mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(value * mult.get(unit, 1))


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
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except ValueError:
            pass

    return now.strftime("%a, %d %b %Y %H:%M:%S +0000")


def _extract_js_tokens(html: str):
    """Extract page token + CSRF token from search or detail page HTML.

    Search pages set ``window.searchPageToken``; detail pages set
    ``window.pageToken``.  Both are tried so this works for either page type.
    """
    m = _RE_SEARCH_TOKEN.search(html)
    page_token = m.group(1) if m else ""
    if not page_token:
        m = _RE_PAGE_TOKEN.search(html)
        page_token = m.group(1) if m else ""

    m = _RE_CSRF.search(html)
    if not m:
        m = _RE_CSRF_FALLBACK.search(html)
    csrf_token = m.group(1) if m else ""

    return page_token, csrf_token


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

class ExtToScraper:

    def __init__(
        self,
        base_url: str,
        flaresolverr: FlareSolverrClient,
        include_adult: bool = True,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._fs = flaresolverr
        self._include_adult = include_adult

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

        # For keywordless searches use ext.to category pages (e.g. /tv/, /movies/)
        # instead of /browse/?age=0 which reliably returns 522.
        if not effective_query and not imdbid:
            return self._browse_by_categories(categories or [], offset, limit)

        start_page = (offset // _RESULTS_PER_PAGE) + 1
        pages_needed = max(1, -(-limit // _RESULTS_PER_PAGE))

        all_results: List[Dict] = []
        last_html = ""
        last_url = ""

        for page in range(start_page, start_page + pages_needed):
            url = self._build_url(effective_query, page, imdbid)
            logger.info("Fetching ext.to page %d: %s", page, url)
            try:
                html, _cookies, _ua = self._fs.get_page_with_cookies(url)
            except FlareSolverrError as exc:
                logger.error("FlareSolverr error: %s", exc)
                break

            last_html = html
            last_url = url
            page_results = self._parse_html(html)
            all_results.extend(page_results)

            if len(page_results) < _RESULTS_PER_PAGE:
                break

        local_offset = offset % _RESULTS_PER_PAGE if offset > 0 else 0
        return all_results[local_offset: local_offset + limit]

    def _browse_by_categories(
        self, categories: List[int], offset: int, limit: int
    ) -> List[Dict]:
        """
        Fetch latest torrents for an empty-query request using ext.to category
        pages (e.g. /tv/, /movies/).  Falls back to /other/ when no matching
        browse path is found.  This avoids the /browse/?age=0 endpoint that
        returns 522.
        """
        # Collect distinct top-level browse paths for the requested categories
        paths_seen: set = set()
        browse_paths: List[str] = []
        for cat in (categories or []):
            # Check exact, then parent
            path = CAT_ID_TO_BROWSE_PATH.get(cat)
            if not path:
                parent = (cat // 1000) * 1000
                path = CAT_ID_TO_BROWSE_PATH.get(parent)
            if path and path not in paths_seen:
                paths_seen.add(path)
                browse_paths.append(path)

        if not browse_paths:
            browse_paths = ["/tv/", "/movies/"]  # sensible default for Sonarr/Radarr test

        all_results: List[Dict] = []
        last_html = ""
        last_url = ""

        for path in browse_paths:
            page_param = (offset // _RESULTS_PER_PAGE) + 1
            url = self._base + path
            if page_param > 1:
                url += f"?page={page_param}"
            logger.info("Keywordless browse: %s", url)
            try:
                html, _cookies, _ua = self._fs.get_page_with_cookies(url)
            except FlareSolverrError as exc:
                logger.error("FlareSolverr error browsing %s: %s", path, exc)
                continue
            last_html = html
            last_url = url
            page_results = self._parse_html(html)
            all_results.extend(page_results)
            if len(all_results) >= limit:
                break

        if categories:
            all_results = [
                r for r in all_results
                if any(
                    cat_id_matches(rc, fc)
                    for rc in r.get("categories", [8000])
                    for fc in categories
                )
            ]

        local_offset = offset % _RESULTS_PER_PAGE if offset > 0 else 0
        return all_results[local_offset: local_offset + limit]

    def _build_url(self, query: str, page: int, imdbid: Optional[str]) -> str:
        params: Dict[str, str] = {"sort": "age", "order": "desc"}
        if imdbid:
            params["imdb_id"] = imdbid if imdbid.startswith("tt") else f"tt{imdbid}"
        else:
            params["q"] = query
        if self._include_adult:
            params["with_adult"] = "1"
        if page > 1:
            params["page"] = str(page)
        return f"{self._base}/browse/?{urlencode(params)}"

    def _parse_html(self, html: str) -> List[Dict]:
        if not html:
            return []
        if "522: Connection timed out" in html or "524: A timeout occurred" in html:
            logger.warning("ext.to returned a Cloudflare error page")
            return []

        soup = BeautifulSoup(html, "lxml")
        table = soup.select_one("table.table-striped")
        if not table:
            logger.warning("Result table not found. Snippet: %s", html[:400].replace("\n", " "))
            return []

        results = []
        for row in table.select("tbody > tr"):
            try:
                item = self._parse_row(row)
                if item:
                    results.append(item)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping malformed row: %s", exc)

        logger.debug("Parsed %d results", len(results))
        return results

    def _parse_row(self, row: Tag) -> Optional[Dict]:
        td1 = row.select_one("td:nth-child(1)")
        if not td1:
            return None

        title_link = td1.select_one("a.torrent-title-link")
        if not title_link:
            return None

        # data-tooltip has the full untruncated title (may contain HTML tags from search highlight)
        title = _RE_HTML_TAGS.sub("", title_link.get("data-tooltip", "")).strip()
        if not title:
            title = title_link.get_text(strip=True)
        if not title:
            return None

        details_href = title_link.get("href", "")
        details_url = (
            self._base + details_href
            if details_href and not details_href.startswith("http")
            else details_href or self._base
        )

        # Category: build lookup key from related-posted links.
        # Links are: [user, parent-cat, sub-cat?]
        # User links appear as /user/name/ OR as relative ?user_nick=... params.
        # Category links are always absolute paths starting with /.
        # Key = parent_href alone  OR  parent_href + sub_href (concatenated).
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
                # e.g. "/tv/" + "/tv/season-packs/" → "/tv//tv/season-packs/"
                key = cat_links[0] + cat_links[1]
                cat_id = EXT_TO_CAT_MAP.get(key, EXT_TO_CAT_MAP.get(cat_links[0], 8000))
            elif len(cat_links) == 1:
                cat_id = EXT_TO_CAT_MAP.get(cat_links[0], 8000)

        # Torrent numeric ID for magnet API
        magnet_btn = td1.select_one("a.search-magnet-btn")
        torrent_id = None
        if magnet_btn:
            try:
                torrent_id = int(magnet_btn.get("data-id", ""))
            except (ValueError, TypeError):
                pass

        # Size
        td2 = row.select_one("td:nth-child(2)")
        size = 0
        if td2:
            val = td2.select_one("span:not(.add-block)")
            if val:
                size = _parse_size(val.get_text(strip=True))

        # Files
        td3 = row.select_one("td:nth-child(3)")
        files = 1
        if td3:
            val = td3.select_one("span:not(.add-block)")
            if val:
                files = max(1, _parse_int(val.get_text(strip=True)))

        # Date – prefer exact date from title attribute
        td4 = row.select_one("td:nth-child(4)")
        pub_date = ""
        if td4:
            span_t = td4.select_one("span[title]")
            pub_date = _parse_date(span_t["title"] if span_t else td4.get_text(strip=True))

        # Seeders
        td5 = row.select_one("td:nth-child(5)")
        seeders = 0
        if td5:
            val = td5.select_one("span.text-success")
            if val:
                seeders = _parse_int(val.get_text(strip=True))

        # Leechers
        td6 = row.select_one("td:nth-child(6)")
        leechers = 0
        if td6:
            val = td6.select_one("span.text-danger")
            if val:
                leechers = _parse_int(val.get_text(strip=True))

        return {
            "title": title,
            "guid": details_url,
            "details_url": details_url,
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
    # Token helpers
    # -----------------------------------------------------------------------

    def _fetch_fresh_tokens(self, page_url: str):
        """Re-fetch *page_url* through FlareSolverr and return fresh tokens.

        Returns ``(page_token, csrf_token)`` — both empty strings on failure.
        """
        try:
            html, _cookies, _ua = self._fs.get_page_with_cookies(page_url)
        except FlareSolverrError as exc:
            logger.debug("Token refresh: page fetch failed: %s", exc)
            return "", ""
        token, csrf = _extract_js_tokens(html)
        if token and csrf:
            logger.debug("Token refresh: got fresh tokens from %s", page_url)
        else:
            logger.debug("Token refresh: tokens not found in page %s", page_url)
        return token, csrf

    @staticmethod
    def _looks_like_token_error(data: dict) -> bool:
        """Return True when the API response indicates an expired / invalid token."""
        err = str(data.get("error", "")).lower()
        return not data.get("success") and any(
            kw in err for kw in ("invalid", "expired", "token", "hmac", "auth", "forbidden", "blocked")
        )

    # -----------------------------------------------------------------------
    # Magnet enrichment
    # -----------------------------------------------------------------------

    def _enrich_with_magnets(self, results: List[Dict], page_url: str, page_html: str) -> None:
        """Fetch magnet links concurrently via /ajax/getSearchMagnet.php.

        Token strategy
        ──────────────
        1. Extract tokens from the already-fetched *page_html* (free – no extra
           request needed).
        2. Fire up to _MAGNET_WORKERS requests in parallel using a thread pool.
        3. If any response looks like a token-expiry error, one thread refreshes
           the tokens (holding a lock so others wait), then all affected requests
           are retried once with the new credentials.
        """
        page_token, csrf_token = _extract_js_tokens(page_html)
        if not page_token or not csrf_token:
            logger.warning("Missing searchPageToken/csrf-token; magnet links unavailable")
            return

        api_url = self._base + _MAGNET_API_PATH

        # Shared mutable token state + lock so only one thread refreshes at a time.
        token_state = {"page_token": page_token, "csrf_token": csrf_token}
        token_lock = threading.Lock()
        refresh_done = threading.Event()  # set once a refresh has succeeded

        def _get_tokens():
            return token_state["page_token"], token_state["csrf_token"]

        def _do_refresh():
            """Refresh tokens if not already done this batch.  Thread-safe."""
            with token_lock:
                if refresh_done.is_set():
                    return  # another thread already refreshed
                new_tok, new_csrf = self._fetch_fresh_tokens(page_url)
                if new_tok and new_csrf:
                    token_state["page_token"] = new_tok
                    token_state["csrf_token"] = new_csrf
                    refresh_done.set()
                    logger.debug("Concurrent token refresh succeeded")
                else:
                    logger.warning("Concurrent token refresh failed")

        def fetch_one(item: Dict) -> None:
            torrent_id = item.get("torrent_id")
            if not torrent_id:
                return

            tok, csrf = _get_tokens()
            try:
                data = self._fs.post_form(api_url, _build_magnet_post(torrent_id, tok, csrf))
            except FlareSolverrError as exc:
                logger.debug("Magnet API error id=%s: %s", torrent_id, exc)
                return

            # Token expired → refresh once and retry
            if self._looks_like_token_error(data):
                logger.info(
                    "Token error for id=%s (%s) – refreshing tokens and retrying",
                    torrent_id, data.get("error", ""),
                )
                _do_refresh()
                tok, csrf = _get_tokens()
                if not tok:
                    return
                try:
                    data = self._fs.post_form(api_url, _build_magnet_post(torrent_id, tok, csrf))
                except FlareSolverrError as exc:
                    logger.debug("Magnet API retry error id=%s: %s", torrent_id, exc)
                    return

            if not data.get("success"):
                logger.debug("Magnet API fail id=%s: %s", torrent_id, data.get("error"))
                return

            infohash = data.get("hash", "").lower()
            magnet_url = data.get("url", "")

            if not magnet_url and infohash:
                if _RE_INFOHASH.fullmatch(infohash):
                    magnet_url = _build_magnet(infohash, item["title"])
                else:
                    infohash = ""

            if magnet_url:
                # Each item dict is only written by one thread (keyed by torrent_id),
                # so no lock needed for the item itself.
                item["magnet_url"] = magnet_url
                item["download_url"] = magnet_url
                item["infohash"] = infohash
                if infohash:
                    item["guid"] = f"https://ext.to/t/{infohash}/"

        with ThreadPoolExecutor(max_workers=_MAGNET_WORKERS) as pool:
            pool.map(fetch_one, results)

        success_count = sum(1 for r in results if r.get("magnet_url"))
        logger.info("Fetched %d/%d magnet links", success_count, len(results))

    # -----------------------------------------------------------------------
    # On-demand magnet fetch (used by t=download)
    # -----------------------------------------------------------------------

    def fetch_magnet_for_guid(self, guid: str) -> Optional[str]:
        """Return a magnet URL for the given GUID.

        Called by the ``t=download`` Torznab endpoint so Radarr/Sonarr can
        grab a specific release without the bulk-enrichment timing issues.

        Strategy
        --------
        1. guid already is a magnet  → return as-is
        2. guid is an infohash URL   → construct minimal magnet
        3. guid is an ext.to detail URL → fetch detail page for fresh tokens,
           call /ajax/getTorrentMagnet.php, return magnet
        """
        if not guid:
            return None

        if guid.startswith("magnet:"):
            return guid

        # Infohash-style GUID: https://ext.to/t/<title>-<hash>/ or /t/<hash>/
        m = _RE_GUID_INFOHASH.search(guid)
        if m:
            return f"magnet:?xt=urn:btih:{m.group(1).lower()}"

        # Detail page URL → fetch page, extract torrent_id + fresh tokens
        if not guid.startswith("http"):
            return None

        logger.info("t=download: fetching detail page %s", guid)
        try:
            html, _cookies, _ua = self._fs.get_page_with_cookies(guid)
        except FlareSolverrError as exc:
            logger.error("t=download: detail page fetch failed: %s", exc)
            return None

        # Extract torrent ID from the URL slug (e.g. /some-title-123456/)
        # This is more reliable than parsing the page's button element, which
        # uses a different selector on detail pages vs. search result rows.
        torrent_id = None
        m = _RE_URL_TORRENT_ID.search(guid.rstrip("/"))
        if m:
            try:
                torrent_id = int(m.group(1))
            except ValueError:
                pass

        # Fallback: button element (works on some page variants)
        if not torrent_id:
            soup = BeautifulSoup(html, "lxml")
            magnet_btn = soup.select_one("a.search-magnet-btn[data-id]")
            if magnet_btn:
                try:
                    torrent_id = int(magnet_btn.get("data-id", ""))
                except (ValueError, TypeError):
                    pass

        if not torrent_id:
            logger.warning("t=download: could not find torrent_id on %s", guid)
            return None

        page_token, csrf_token = _extract_js_tokens(html)
        if not page_token or not csrf_token:
            logger.warning("t=download: missing tokens on detail page %s", guid)
            return None

        # Use the detail-page endpoint; fall back to the search endpoint
        post_data = _build_magnet_post(torrent_id, page_token, csrf_token)

        for api_path in (_DETAIL_MAGNET_API_PATH, _MAGNET_API_PATH):
            api_url = self._base + api_path
            try:
                data = self._fs.post_form(api_url, post_data)
            except FlareSolverrError as exc:
                logger.debug("t=download: API %s error: %s", api_path, exc)
                continue

            if data.get("success"):
                infohash = data.get("hash", "").lower()
                magnet_url = data.get("url", "")
                if not magnet_url and _RE_INFOHASH.fullmatch(infohash):
                    magnet_url = _build_magnet(infohash, "")
                if magnet_url:
                    logger.info("t=download: got magnet for id=%s", torrent_id)
                    return magnet_url
            else:
                logger.debug("t=download: API %s failed: %s", api_path, data.get("error"))

        return None
