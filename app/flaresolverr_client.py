"""
Challenge-solver HTTP client (FlareSolverr and Byparr) with persistent
session support where the backend offers it.

Two backends speak the FlareSolverr ``/v1`` request shape:

``flaresolverr``
    The original.  A browser session is created on first use, recreated if
    it expires, and torn down again once it has been idle for a while: an
    open session keeps a Chrome tab alive inside the FlareSolverr container
    (~250 MB resident), while the ``cf_clearance`` cookies it produced live
    on in our own HTTP session, so an idle browser buys nothing.  Supports
    ``request.post``.

``byparr``
    `Byparr <https://github.com/ThePhaseless/Byparr>`_ accepts the same JSON
    but ignores ``cmd``, ``session`` and ``postData``: every call is one page
    load in a throw-away Firefox.  No sessions to manage, and no browser
    POST – callers must refresh cookies with a page fetch and POST directly.
"""

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

import requests

# Pre-compiled: strips the <html><body><pre>…</pre></body></html> wrapper
# that FlareSolverr adds around JSON API responses.
_RE_PRE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.DOTALL)

BACKENDS = ("auto", "flaresolverr", "byparr")

logger = logging.getLogger(__name__)


class FlareSolverrError(Exception):
    """Raised when the solver returns an error or is unreachable."""


class FlareSolverrClient:
    """
    Thin wrapper around the FlareSolverr REST API (also spoken by Byparr).

    Parameters
    ----------
    base_url : str
        Base URL of the solver instance (e.g. ``http://localhost:8191``).
    timeout_ms : int
        Maximum time in milliseconds the solver should wait for a page to
        load.  Also used (+ 15 s buffer) as the HTTP connect/read timeout.
    tabs_till_verify : int
        Turnstile bypass hint forwarded to FlareSolverr (0 = not sent).
    session_idle : float
        Seconds without a request after which the browser session is
        destroyed.  0 keeps it forever.  FlareSolverr only.
    reaper_interval : float
        How often the idle check runs.
    session_ttl_minutes : int
        Forwarded to ``sessions.create`` so FlareSolverr itself expires a
        session we somehow lost track of (0 = not sent).  Defence in depth:
        every orphaned session is a ~300 MB Chromium.
    backend : str
        ``"flaresolverr"``, ``"byparr"`` or ``"auto"``.  ``auto`` probes
        ``GET <base_url>/`` once, lazily: Byparr answers with a redirect to
        its ``/docs`` (or a JSON ``msg`` naming itself); FlareSolverr answers
        with a JSON ``msg``.  Anything unrecognised is treated as FlareSolverr.
    """

    def __init__(
        self,
        base_url: str,
        timeout_ms: int = 60_000,
        tabs_till_verify: int = 0,
        session_idle: float = 300.0,
        reaper_interval: float = 30.0,
        session_ttl_minutes: int = 0,
        backend: str = "auto",
    ) -> None:
        backend = (backend or "auto").strip().lower()
        if backend not in BACKENDS:
            raise ValueError(
                f"Unknown solver backend {backend!r}; expected one of {', '.join(BACKENDS)}"
            )
        self._base_url = base_url.rstrip("/")
        self._api_url = self._base_url + "/v1"
        self._timeout_ms = timeout_ms
        self._http_timeout = timeout_ms / 1_000 + 15
        self._tabs_till_verify = tabs_till_verify
        self._session_ttl_minutes = max(0, session_ttl_minutes)
        self._session_id: Optional[str] = None
        self._backend: Optional[str] = None if backend == "auto" else backend
        # Keep-alive to the solver container instead of a new TCP
        # connection per command.
        self._http = requests.Session()
        # Serialise session creation/destruction so concurrent threads can't
        # race and create multiple browser sessions simultaneously.
        self._session_lock = threading.Lock()
        # One browser session is one Chromium tab.  Two request.get calls
        # navigating it at the same time abort each other's challenge solve
        # ("Timeout after 60 seconds" for both), so commands run one at a time.
        # Byparr launches a browser per call; serialising there keeps its
        # memory bounded to one Firefox at a time.
        # Acquired before _session_lock, never the other way round.
        self._cmd_lock = threading.Lock()

        self._session_idle = max(0.0, session_idle)
        self._reaper_interval = max(0.01, reaper_interval)
        self._last_used = time.monotonic()
        self._in_flight = 0
        self._stop = threading.Event()
        self._reaper: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Backend detection
    # ------------------------------------------------------------------

    @property
    def backend(self) -> str:
        """``"flaresolverr"`` or ``"byparr"`` – probed on first use in auto mode."""
        if self._backend is None:
            with self._session_lock:
                if self._backend is None:
                    self._backend = self._detect_backend()
        return self._backend

    def _detect_backend(self) -> str:
        try:
            resp = self._http.get(self._base_url + "/", timeout=10, allow_redirects=False)
        except requests.RequestException as exc:
            raise FlareSolverrError(
                f"Cannot connect to solver at {self._base_url}: {exc}"
            ) from exc
        detected = "flaresolverr"
        if 300 <= resp.status_code < 400:
            # Byparr: GET / is a 301 to /docs.
            detected = "byparr"
        else:
            try:
                msg = str((resp.json() or {}).get("msg", ""))
            except ValueError:
                msg = ""
            if "byparr" in msg.lower():
                detected = "byparr"
        logger.info("Solver at %s detected as %s", self._base_url, detected)
        return detected

    @property
    def supports_sessions(self) -> bool:
        return self.backend == "flaresolverr"

    @property
    def supports_post(self) -> bool:
        """Whether ``request.post`` can be routed through the browser."""
        return self.backend == "flaresolverr"

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _post(self, payload: dict) -> dict:
        try:
            resp = self._http.post(
                self._api_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self._http_timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError as exc:
            raise FlareSolverrError(
                f"Cannot connect to solver at {self._api_url}: {exc}"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise FlareSolverrError(
                f"Solver request timed out after {self._http_timeout:.0f}s"
            ) from exc
        except requests.exceptions.HTTPError as exc:
            # Byparr reports failures this way: 408 on a timed-out solve, 502
            # when the target is unreachable, with a JSON "detail" body.
            raise FlareSolverrError(
                f"Solver HTTP error: {exc.response.status_code} {exc.response.text[:200]}"
            ) from exc

    # ------------------------------------------------------------------
    # Session management (FlareSolverr only)
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def touch(self) -> None:
        """Mark the session as used right now (defers the idle teardown)."""
        self._last_used = time.monotonic()

    def create_session(self) -> Optional[str]:
        """Create a browser session (if none is cached) and return its ID.

        Returns ``None`` on a backend without sessions.
        """
        with self._session_lock:
            return self._create_locked()

    def _create_locked(self) -> Optional[str]:
        """``sessions.create`` – caller must hold ``_session_lock``."""
        # Double-checked: another thread may have already created it.
        if self._session_id:
            return self._session_id
        if not self.supports_sessions:
            return None
        payload = {"cmd": "sessions.create"}
        if self._session_ttl_minutes > 0:
            payload["session_ttl_minutes"] = self._session_ttl_minutes
        result = self._post(payload)
        session_id = result.get("session")
        if not session_id:
            raise FlareSolverrError("FlareSolverr did not return a session ID")
        self._session_id = session_id
        self.touch()
        logger.info("FlareSolverr session created: %s", self._session_id)
        self._start_reaper()
        return self._session_id

    def destroy_session(self) -> None:
        """Destroy the cached browser session."""
        with self._session_lock:
            self._destroy_locked()

    def _destroy_locked(self) -> None:
        """``sessions.destroy`` for the cached session – caller must hold ``_session_lock``."""
        if not self._session_id:
            return
        try:
            self._post({"cmd": "sessions.destroy", "session": self._session_id})
            logger.info("FlareSolverr session destroyed: %s", self._session_id)
        except FlareSolverrError as exc:
            logger.warning("Failed to destroy FlareSolverr session: %s", exc)
        finally:
            self._session_id = None

    def _replace_session(self, failed_id: Optional[str]) -> Optional[str]:
        """Destroy *failed_id* (if it is still the current session) and return a fresh one.

        Merely forgetting a broken session ID leaks the Chromium behind it:
        FlareSolverr keeps the tab alive until told otherwise, and one leak
        per transport error fills a 2 GiB container in a few hours.  The
        ``failed_id`` guard keeps two workers that failed on the same session
        from destroying each other's replacement.

        On a session-less backend there is nothing to replace; returns ``None``.
        """
        with self._session_lock:
            if self._session_id and self._session_id == failed_id:
                logger.warning("Replacing broken FlareSolverr session %s", failed_id)
                self._destroy_locked()
            return self._create_locked()

    def close(self) -> None:
        """Stop the idle reaper and destroy the session (application shutdown)."""
        self._stop.set()
        if self._reaper is not None:
            self._reaper.join(timeout=2)
        if self._session_id:
            self.destroy_session()

    # -- idle teardown -------------------------------------------------

    def _start_reaper(self) -> None:
        if self._session_idle <= 0 or self._reaper is not None:
            return
        self._reaper = threading.Thread(
            target=self._reap_loop, name="flaresolverr-reaper", daemon=True
        )
        self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._stop.wait(self._reaper_interval):
            with self._session_lock:
                if (
                    self._session_id
                    and self._in_flight == 0
                    and time.monotonic() - self._last_used >= self._session_idle
                ):
                    logger.info(
                        "FlareSolverr session idle for %.0fs, releasing browser",
                        self._session_idle,
                    )
                    self._destroy_locked()

    @contextmanager
    def _using_session(self) -> Iterator[None]:
        """Hold the session open for the duration of one command."""
        with self._session_lock:
            self._in_flight += 1
        try:
            if not self._session_id:
                self.create_session()
            yield
        finally:
            with self._session_lock:
                self._in_flight -= 1
                self.touch()

    def _get_payload(self, cmd: str, url: str) -> dict:
        payload = {"cmd": cmd, "url": url, "maxTimeout": self._timeout_ms}
        if self._session_id:
            payload["session"] = self._session_id
        if self._tabs_till_verify > 0 and self.supports_sessions:
            payload["tabs_till_verify"] = self._tabs_till_verify
        return payload

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_page(self, url: str) -> str:
        """Fetch *url* and return rendered HTML (backwards-compat wrapper)."""
        html, _cookies, _ua = self.get_page_with_cookies(url)
        return html

    def get_page_with_cookies(self, url: str) -> tuple:
        """
        Fetch *url* through the solver.

        Returns ``(html, cookies, user_agent)`` where *cookies* is a list of
        ``{"name", "value", "domain", "path"}`` dicts.  Domains are kept
        because the solver may have been redirected (``search.extto.com`` →
        ``extto.com``) and Cloudflare scopes ``cf_clearance`` to the host it
        was issued for; re-homing it onto the requested host silently loses
        it on the very next redirect.
        Automatically creates/recreates the browser session as needed.
        """
        with self._using_session(), self._cmd_lock:
            return self._do_get_with_cookies(url)

    def _do_get_with_cookies(self, url: str, retry: bool = True) -> tuple:
        payload = self._get_payload("request.get", url)
        failed_id = payload.get("session")
        try:
            result = self._post(payload)
        except FlareSolverrError:
            if retry:
                logger.warning("Solver error; %s and retrying",
                               "replacing session" if failed_id else "no session to replace")
                replacement = self._replace_session(failed_id)
                if replacement:
                    payload["session"] = replacement
                result = self._post(payload)
            else:
                raise

        status = result.get("status")
        if status != "ok":
            msg = result.get("message", "Unknown error")
            if retry and failed_id and "session" in msg.lower():
                logger.warning("Session error from FlareSolverr; replacing session")
                self._replace_session(failed_id)
                return self._do_get_with_cookies(url, retry=False)
            raise FlareSolverrError(f"Solver returned status={status!r}: {msg}")

        solution = result.get("solution", {})
        http_status = solution.get("status", 200)
        if http_status >= 400:
            raise FlareSolverrError(
                f"ext.to returned HTTP {http_status} for {url}"
            )

        html = solution.get("response", "")
        user_agent = solution.get("userAgent", "")

        cookies = [
            {
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain") or "",
                "path": c.get("path") or "/",
            }
            for c in solution.get("cookies", [])
            if c.get("name")
        ]

        return html, cookies, user_agent

    def post_form(self, url: str, post_data: str) -> dict:
        """
        POST form-encoded *post_data* to *url* through the browser.

        Routes the request through the persistent browser session so it
        carries the same IP and cookies that solved the Cloudflare challenge.
        Automatically recreates the session on failure (same retry logic as
        GET requests).

        Raises :class:`FlareSolverrError` on a backend without
        ``request.post`` (Byparr); callers should refresh cookies with
        :meth:`get_page_with_cookies` and POST directly instead.

        Returns the parsed JSON body of the response.
        """
        if not self.supports_post:
            raise FlareSolverrError(
                f"{self.backend} backend does not support request.post"
            )
        with self._using_session(), self._cmd_lock:
            return self._do_post_form(url, post_data)

    def _do_post_form(self, url: str, post_data: str, retry: bool = True) -> dict:
        payload = self._get_payload("request.post", url)
        payload.pop("tabs_till_verify", None)
        payload["postData"] = post_data
        failed_id = payload.get("session")
        try:
            result = self._post(payload)
        except FlareSolverrError:
            if retry:
                logger.warning("FlareSolverr POST error; replacing session and retrying")
                replacement = self._replace_session(failed_id)
                if replacement:
                    payload["session"] = replacement
                result = self._post(payload)
            else:
                raise

        status = result.get("status")
        if status != "ok":
            msg = result.get("message", "Unknown error")
            if retry and failed_id and "session" in msg.lower():
                logger.warning("Session error on POST; replacing session")
                self._replace_session(failed_id)
                return self._do_post_form(url, post_data, retry=False)
            raise FlareSolverrError(f"FlareSolverr POST returned status={status!r}: {msg}")

        solution = result.get("solution", {})
        http_status = solution.get("status", 200)
        if http_status >= 400:
            raise FlareSolverrError(f"POST to {url} returned HTTP {http_status}")

        # Strip the <html><body><pre>…</pre></body></html> wrapper FlareSolverr
        # adds around JSON API responses.
        raw = solution.get("response", "{}")
        m = _RE_PRE.search(raw)
        if m:
            raw = m.group(1).strip()

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"success": False, "error": f"Non-JSON response: {raw[:200]}"}
