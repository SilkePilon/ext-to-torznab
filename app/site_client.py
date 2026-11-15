"""
Transport layer for ext.to and its mirrors.

Two transports are supported per host:

``direct``
    A plain :class:`requests.Session` with keep-alive and a browser-like User
    Agent.  Roughly 10× faster than FlareSolverr (~0.3 s vs ~3-10 s per
    request) and the mirror ``extto.com`` normally serves everything this way.

``flaresolverr``
    A headless Chrome round-trip used only when the host answers with a
    Cloudflare challenge.  Cookies (``cf_clearance``) and the browser User
    Agent returned by FlareSolverr are imported into the direct session
    afterwards, so subsequent requests usually go back to the fast path.

:class:`SitePool` layers mirror failover on top: hosts are tried in order,
a failing host is put into cooldown, and the last host that worked becomes
sticky so healthy requests cost a single attempt.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter

from .flaresolverr_client import FlareSolverrClient, FlareSolverrError

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

# Body markers that identify a Cloudflare interstitial rather than real content.
_CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "challenge-platform",
    "cf_chl_opt",
    "Just a moment",
    "Enable JavaScript and cookies to continue",
    "Checking your browser before accessing",
)
# Cloudflare origin-side errors – the mirror is up but its backend is not.
_CF_ORIGIN_ERRORS = (520, 521, 522, 523, 524, 525, 526)

# Chromium's own "This site can't be reached" page.  FlareSolverr hands it
# back as a successful solution (there was no challenge to solve), so it
# has to be recognised here or it parses as a legitimate empty result page.
_BROWSER_ERROR_MARKERS = ("main-frame-error", "interstitial-wrapper", "chrome-error://")

# Cookies that identify the ext.to PHP session (dropped when its magnet API
# quota runs out).  Cloudflare's cf_clearance is deliberately not in here.
_SITE_SESSION_COOKIES = ("PHPSESSID",)

# After a solver round-trip the direct POST is retried once.  If it is
# challenged again this soon after a solve, the cookies are simply not being
# honoured (wrong egress IP, UA mismatch) and solving again would only burn
# another browser launch per magnet lookup.
_RESOLVE_BACKOFF = 60.0

_RE_PRE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.DOTALL)


class SiteUnavailable(Exception):
    """Raised when a host could not be reached through any transport."""


def _looks_like_browser_error(body: str) -> bool:
    head = body[:6000]
    return any(marker in head for marker in _BROWSER_ERROR_MARKERS)


def _looks_like_challenge(status: int, body: str, headers=None) -> bool:
    if status in (403, 503) or status in _CF_ORIGIN_ERRORS:
        return True
    # Cloudflare labels every interstitial it serves, whatever the status.
    if headers is not None and headers.get("cf-mitigated"):
        return True
    head = body[:4000]
    return any(marker in head for marker in _CHALLENGE_MARKERS)


class SiteClient:
    """A single ext.to host (main site or mirror) with adaptive transport."""

    def __init__(
        self,
        base_url: str,
        flaresolverr: Optional[FlareSolverrClient] = None,
        timeout: int = 20,
        prefer_direct: bool = True,
        proxy_url: str = "",
        pool_size: int = 32,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.host = urlsplit(self.base).netloc
        self._fs = flaresolverr
        self._timeout = timeout
        self._prefer_direct = prefer_direct
        self._proxy_url = (proxy_url or "").strip()

        self._session = requests.Session()
        # Use an explicit proxy only; do not pick up ambient HTTP_PROXY values.
        self._session.trust_env = False
        if self._proxy_url:
            self._session.proxies.update({
                "http": self._proxy_url,
                "https": self._proxy_url,
            })
        self._session.headers.update({
            "User-Agent": _DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        # Sized for several concurrent searches each running MAGNET_WORKERS
        # lookups; an overflowing pool discards keep-alive connections and
        # pays a fresh TLS handshake per magnet POST.
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        # Transport state
        self._direct_blocked_until = 0.0
        self._direct_failures = 0

        # Cached page tokens (searchPageToken + csrf-token) harvested from the
        # last search page we fetched.  Any torrent_id can be resolved with
        # them, so grabs cost one POST instead of a page load + POST.
        self._tokens: Tuple[str, str] = ("", "")
        self._tokens_at = 0.0
        self._token_lock = threading.Lock()

        # API POSTs made with the current PHP session (ext.to allows 60 magnet
        # calls per session before answering "Too many requests").
        self._api_calls = 0

        # Single-flight for cookie refreshes on a solver without request.post:
        # concurrent magnet workers that all hit a 403 must share one solve.
        # Also shared by page fetches, so a burst of requests that all find
        # the clearance expired waits for one browser solve, not one each.
        self._solve_lock = threading.Lock()
        self._last_solve_at = 0.0

        # When the current cf_clearance arrived, and when a direct request
        # last got through.  Diagnostics only: tells how long a clearance
        # actually lasts on this host.
        self._clearance_at = 0.0
        self._direct_ok_at = 0.0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def transport(self) -> str:
        """``"direct"`` or ``"flaresolverr"`` – whichever will be used next."""
        if self._prefer_direct and time.time() >= self._direct_blocked_until:
            return "direct"
        return "flaresolverr"

    def clearance_status(self) -> Dict[str, Optional[int]]:
        now = time.time()
        return {
            "clearance_age_s": int(now - self._clearance_at) if self._clearance_at else None,
            "last_direct_ok_s": int(now - self._direct_ok_at) if self._direct_ok_at else None,
        }

    def url(self, path: str, params: Optional[Dict[str, str]] = None) -> str:
        full = path if path.startswith("http") else urljoin(self.base + "/", path.lstrip("/"))
        if params:
            full += ("&" if "?" in full else "?") + urlencode(params)
        return full

    # ------------------------------------------------------------------
    # Tokens
    # ------------------------------------------------------------------

    def remember_tokens(self, html: str) -> None:
        """Cache searchPageToken + csrf-token found in *html* (if any)."""
        token, csrf = extract_js_tokens(html)
        if token and csrf:
            with self._token_lock:
                self._tokens = (token, csrf)
                self._tokens_at = time.time()

    def cached_tokens(self, max_age: float = 600.0) -> Tuple[str, str]:
        with self._token_lock:
            if self._tokens[0] and time.time() - self._tokens_at <= max_age:
                return self._tokens
        return "", ""

    def tokens_marker(self) -> float:
        """Timestamp of the current tokens – used to collapse concurrent refreshes."""
        with self._token_lock:
            return self._tokens_at

    def invalidate_tokens(self) -> None:
        with self._token_lock:
            self._tokens = ("", "")
            self._tokens_at = 0.0

    def rotate_site_session(self) -> None:
        """Drop the site's PHP session cookie and the tokens tied to it.

        ext.to caps how many magnet API calls one PHP session may make; once
        that cap is hit every call answers "Invalid request. Please refresh the
        page." until a new session is started.  Cloudflare cookies are kept so
        this never costs another challenge solve.
        """
        # Match by name only: the solver hands cookies back scoped to the
        # domain that set them (extto.com, .extto.com), not to self.host.
        for c in list(self._session.cookies):
            if c.name in _SITE_SESSION_COOKIES:
                try:
                    self._session.cookies.clear(domain=c.domain, path=c.path, name=c.name)
                except KeyError:
                    pass
        self.invalidate_tokens()
        with self._token_lock:
            self._api_calls = 0
        logger.info("[%s] rotated site session", self.host)

    @property
    def api_calls(self) -> int:
        """API POSTs made with the current PHP session."""
        with self._token_lock:
            return self._api_calls

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def get(self, path: str, params: Optional[Dict[str, str]] = None) -> str:
        """Fetch a page and return its HTML, escalating to FlareSolverr if needed."""
        url = self.url(path, params)

        noticed_at = time.time()
        if self.transport == "direct":
            html = self._try_direct(url)
            if html is not None:
                return html

        html = self._solve_get(url, noticed_at)
        self.remember_tokens(html)
        return html

    def _try_direct(self, url: str) -> Optional[str]:
        html = self._direct_get(url)
        if html is not None:
            self._direct_failures = 0
            self._direct_ok_at = time.time()
            self.remember_tokens(html)
        return html

    def _solve_get(self, url: str, noticed_at: float) -> str:
        """Fetch *url* through the solver – unless a solve already covers us.

        A solve takes seconds to a minute, and every request that arrives in
        the meantime is challenged as well.  They queue here; when one gets
        the lock after somebody else's solve finished, the fresh cookies are
        tried directly first, so a burst of N requests costs one browser run.
        """
        with self._solve_lock:
            if self._last_solve_at >= noticed_at and self.transport == "direct":
                html = self._try_direct(url)
                if html is not None:
                    return html
            html = self._flaresolverr_get(url)
            self._last_solve_at = time.time()
            return html

    def _direct_get(self, url: str) -> Optional[str]:
        try:
            resp = self._session.get(url, timeout=self._timeout, allow_redirects=True)
        except requests.RequestException as exc:
            logger.info("[%s] direct GET failed: %s", self.host, exc)
            self._note_direct_failure()
            return None

        if _looks_like_challenge(resp.status_code, resp.text, resp.headers):
            if self._clearance_at and self._direct_ok_at > self._clearance_at:
                logger.info(
                    "[%s] direct GET challenged (HTTP %s); cf_clearance worked for %.0f min",
                    self.host, resp.status_code, (self._direct_ok_at - self._clearance_at) / 60,
                )
            else:
                logger.info("[%s] direct GET challenged (HTTP %s)", self.host, resp.status_code)
            self._note_direct_failure()
            return None
        if resp.status_code >= 400:
            logger.info("[%s] direct GET HTTP %s", self.host, resp.status_code)
            self._note_direct_failure()
            return None
        return resp.text

    def _flaresolverr_get(self, url: str) -> str:
        if not self._fs:
            raise SiteUnavailable(f"{self.host}: direct request failed and FlareSolverr is not configured")
        try:
            html, cookies, user_agent = self._fs.get_page_with_cookies(url)
        except FlareSolverrError as exc:
            raise SiteUnavailable(f"{self.host}: {exc}") from exc
        if _looks_like_browser_error(html):
            raise SiteUnavailable(f"{self.host}: browser could not load the page (network error)")
        self._import_browser_state(cookies, user_agent)
        return html

    def _note_direct_failure(self) -> None:
        """Back off from the direct transport after repeated challenges."""
        self._direct_failures += 1
        if self._direct_failures >= 2:
            # Long enough to avoid thrashing, short enough to re-probe often.
            self._direct_blocked_until = time.time() + 300
            logger.info("[%s] using FlareSolverr for the next 5 min", self.host)

    def _import_browser_state(self, cookies, user_agent: str) -> None:
        """Copy the solved Cloudflare cookies + UA into the direct session.

        ``cf_clearance`` is bound to the User Agent that solved the challenge,
        so both must be adopted together for direct requests to be accepted.

        *cookies* is a list of ``{"name", "value", "domain", "path"}`` dicts
        (a plain ``{name: value}`` dict is accepted for older callers).  The
        cookie's own domain is kept: ``search.extto.com`` redirects to
        ``extto.com``, and a ``cf_clearance`` pinned to ``search.extto.com``
        never reaches the host that actually checks it.
        """
        if user_agent:
            self._session.headers["User-Agent"] = user_agent
        if isinstance(cookies, dict):
            cookies = [{"name": n, "value": v} for n, v in cookies.items()]
        for c in cookies or []:
            self._session.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain") or self.host,
                path=c.get("path") or "/",
            )
        if any(c["name"] == "cf_clearance" for c in cookies or []):
            self._clearance_at = time.time()
        if cookies:
            # Give the fast path another go now that we hold clearance cookies.
            self._direct_blocked_until = 0.0
            self._direct_failures = 0

    # ------------------------------------------------------------------
    # POST (JSON APIs)
    # ------------------------------------------------------------------

    def post_json(self, path: str, data: Dict[str, str]) -> dict:
        """POST form data and return the decoded JSON body."""
        url = self.url(path)
        _token, csrf = self.cached_tokens(max_age=3600)
        with self._token_lock:
            self._api_calls += 1

        if self.transport == "direct":
            result = self._direct_post(url, data, csrf)
            if result is not None:
                return result

        if not self._fs:
            raise SiteUnavailable(f"{self.host}: direct POST failed and no solver is configured")

        if not getattr(self._fs, "supports_post", True):
            # Byparr cannot POST through the browser.  Solve a page instead:
            # that imports fresh clearance cookies + UA and re-opens the
            # direct transport, then the POST is retried directly.  The
            # solved cookies are valid for our own requests because both
            # egress from the same IP (see byparr.yaml in the homelab).
            self._refresh_clearance(noticed_at=time.time())
            result = self._direct_post(url, data, csrf)
            if result is not None:
                return result
            raise SiteUnavailable(f"{self.host}: direct POST still challenged after a fresh solve")

        try:
            return self._fs.post_form(url, urlencode(data))
        except FlareSolverrError as exc:
            raise SiteUnavailable(f"{self.host}: {exc}") from exc

    def _refresh_clearance(self, noticed_at: float) -> None:
        """Solve one page to refresh clearance cookies – at most once per burst.

        *noticed_at* is when the caller saw its direct request fail.  A solve
        that finished after that already covers the caller, so it just
        retries.  A solve that finished shortly *before* it evidently did not
        help, so the caller gives up instead of launching another browser.
        """
        with self._solve_lock:
            if self._last_solve_at >= noticed_at:
                return
            since = noticed_at - self._last_solve_at
            if since < _RESOLVE_BACKOFF:
                raise SiteUnavailable(
                    f"{self.host}: still challenged {since:.0f}s after a fresh solve; "
                    "solved cookies are not being honoured"
                )
            self._flaresolverr_get(self.url("/browse/"))
            self._last_solve_at = time.time()

    def _direct_post(self, url: str, data: Dict[str, str], csrf: str) -> Optional[dict]:
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.base + "/browse/",
            "Origin": self.base,
        }
        if csrf:
            # main.min.js installs an XHR hook that adds this to same-origin
            # requests; send it so we look like the real front-end.
            headers["X-Csrf-Token"] = csrf
        try:
            resp = self._session.post(url, data=data, headers=headers, timeout=self._timeout)
        except requests.RequestException as exc:
            logger.info("[%s] direct POST failed: %s: %s", self.host, type(exc).__name__, exc)
            self._note_direct_failure()
            return None

        if _looks_like_challenge(resp.status_code, resp.text, resp.headers):
            logger.info("[%s] direct POST challenged (HTTP %s)", self.host, resp.status_code)
            self._note_direct_failure()
            return None
        if resp.status_code >= 400:
            logger.info(
                "[%s] direct POST HTTP %s: %s", self.host, resp.status_code,
                resp.text[:160].replace("\n", " "),
            )
            self._note_direct_failure()
            return None

        body = resp.text
        m = _RE_PRE.search(body)
        if m:
            body = m.group(1).strip()
        try:
            return json.loads(body)
        except ValueError:
            logger.info("[%s] non-JSON POST response (HTTP %s): %s", self.host, resp.status_code, body[:160].replace("\n", " "))
            return None


# ---------------------------------------------------------------------------
# Token extraction (shared by SiteClient and the scraper)
# ---------------------------------------------------------------------------

_RE_SEARCH_TOKEN = re.compile(r"window\.searchPageToken\s*=\s*['\"]([a-fA-F0-9]+)['\"]")
_RE_PAGE_TOKEN = re.compile(r"window\.pageToken\s*=\s*['\"]([a-fA-F0-9]+)['\"]")
_RE_CSRF_META = re.compile(
    r'<meta\s[^>]*name=["\']csrf-token["\']\s[^>]*content=["\']([a-fA-F0-9]+)["\']'
)
_RE_CSRF_WINDOW = re.compile(r"window\.csrfToken\s*=\s*['\"]([a-fA-F0-9]+)['\"]")
_RE_CSRF_LOOSE = re.compile(r'csrf-token[^>]*content=["\']([a-fA-F0-9]+)["\']')


def extract_js_tokens(html: str) -> Tuple[str, str]:
    """Return ``(page_token, csrf_token)`` from a search or detail page.

    Search pages set ``window.searchPageToken``; detail pages set
    ``window.pageToken``.  The CSRF value lives in a meta tag and (on detail
    pages) also in ``window.csrfToken``.
    """
    if not html:
        return "", ""

    m = _RE_SEARCH_TOKEN.search(html) or _RE_PAGE_TOKEN.search(html)
    page_token = m.group(1) if m else ""

    m = _RE_CSRF_META.search(html) or _RE_CSRF_WINDOW.search(html) or _RE_CSRF_LOOSE.search(html)
    csrf_token = m.group(1) if m else ""

    return page_token, csrf_token


# ---------------------------------------------------------------------------
# Mirror pool
# ---------------------------------------------------------------------------

class SitePool:
    """Ordered list of mirrors with sticky selection and per-host cooldown."""

    def __init__(
        self,
        base_urls: List[str],
        flaresolverr: Optional[FlareSolverrClient] = None,
        timeout: int = 20,
        prefer_direct: bool = True,
        cooldown: int = 300,
        proxy_url: str = "",
    ) -> None:
        seen = set()
        self.clients: List[SiteClient] = []
        for url in base_urls:
            url = (url or "").strip().rstrip("/")
            if not url or url in seen:
                continue
            seen.add(url)
            self.clients.append(SiteClient(
                url, flaresolverr, timeout, prefer_direct, proxy_url=proxy_url
            ))
        if not self.clients:
            raise ValueError("SitePool needs at least one base URL")

        self._cooldown = cooldown
        self._down_until: Dict[str, float] = {}
        self._active = self.clients[0]
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    @property
    def active(self) -> SiteClient:
        return self._active

    def status(self) -> List[Dict[str, object]]:
        now = time.time()
        return [
            {
                "url": c.base,
                "active": c is self._active,
                "transport": c.transport,
                "cooldown_s": max(0, int(self._down_until.get(c.base, 0) - now)),
                **c.clearance_status(),
            }
            for c in self.clients
        ]

    def candidates(self) -> List[SiteClient]:
        """Active host first, then the rest; hosts in cooldown go last."""
        now = time.time()
        active = self._active
        ordered = [active] + [c for c in self.clients if c is not active]
        ready = [c for c in ordered if self._down_until.get(c.base, 0) <= now]
        cooling = [c for c in ordered if self._down_until.get(c.base, 0) > now]
        return ready + cooling

    def clear_cooldowns(self) -> None:
        """Forget every cooldown (used after a best-effort warm-up fails)."""
        with self._lock:
            self._down_until.clear()

    def _mark_down(self, client: SiteClient) -> None:
        with self._lock:
            self._down_until[client.base] = time.time() + self._cooldown
        logger.warning("Mirror %s marked down for %ds", client.base, self._cooldown)

    def _mark_up(self, client: SiteClient) -> None:
        if client is self._active and client.base not in self._down_until:
            return
        with self._lock:
            self._down_until.pop(client.base, None)
            if self._active is not client:
                logger.info("Switched active mirror to %s", client.base)
                self._active = client

    # ------------------------------------------------------------------

    def get(self, path: str, params: Optional[Dict[str, str]] = None) -> Tuple[str, SiteClient]:
        """Fetch *path* from the first mirror that answers.

        Returns ``(html, client)`` so the caller knows which host (and cookie
        jar) the page came from — magnet tokens are only valid on that host.
        """
        errors = []
        for client in self.candidates():
            try:
                html = client.get(path, params)
            except SiteUnavailable as exc:
                errors.append(str(exc))
                self._mark_down(client)
                continue
            if not html:
                errors.append(f"{client.host}: empty response")
                self._mark_down(client)
                continue
            self._mark_up(client)
            return html, client

        raise SiteUnavailable("all mirrors failed: " + "; ".join(errors))
