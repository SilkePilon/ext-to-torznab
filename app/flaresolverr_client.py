"""
FlareSolverr HTTP client with persistent session support.
The session is created on first use and automatically recreated if it expires.
"""

import json
import logging
import re
import threading
from typing import Optional

import requests

# Pre-compiled: strips the <html><body><pre>…</pre></body></html> wrapper
# that FlareSolverr adds around JSON API responses.
_RE_PRE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.DOTALL)

logger = logging.getLogger(__name__)


class FlareSolverrError(Exception):
    """Raised when FlareSolverr returns an error or is unreachable."""


class FlareSolverrClient:
    """
    Thin wrapper around the FlareSolverr REST API.

    Parameters
    ----------
    base_url : str
        Base URL of the FlareSolverr instance (e.g. ``http://localhost:8191``).
    timeout_ms : int
        Maximum time in milliseconds FlareSolverr should wait for a page to
        load.  Also used (+ 15 s buffer) as the HTTP connect/read timeout.
    """

    def __init__(self, base_url: str, timeout_ms: int = 60_000) -> None:
        self._api_url = base_url.rstrip("/") + "/v1"
        self._timeout_ms = timeout_ms
        self._http_timeout = timeout_ms / 1_000 + 15
        self._session_id: Optional[str] = None
        # Serialise session creation/destruction so concurrent threads can't
        # race and create multiple browser sessions simultaneously.
        self._session_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _post(self, payload: dict) -> dict:
        try:
            resp = requests.post(
                self._api_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self._http_timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError as exc:
            raise FlareSolverrError(
                f"Cannot connect to FlareSolverr at {self._api_url}: {exc}"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise FlareSolverrError(
                f"FlareSolverr request timed out after {self._http_timeout:.0f}s"
            ) from exc
        except requests.exceptions.HTTPError as exc:
            raise FlareSolverrError(
                f"FlareSolverr HTTP error: {exc.response.status_code} {exc.response.text[:200]}"
            ) from exc

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def create_session(self) -> str:
        """Create a new browser session and cache its ID."""
        with self._session_lock:
            # Double-checked: another thread may have already created it.
            if self._session_id:
                return self._session_id
            result = self._post({"cmd": "sessions.create"})
            session_id = result.get("session")
            if not session_id:
                raise FlareSolverrError("FlareSolverr did not return a session ID")
            self._session_id = session_id
            logger.info("FlareSolverr session created: %s", self._session_id)
            return self._session_id

    def destroy_session(self) -> None:
        """Destroy the cached browser session."""
        if not self._session_id:
            return
        try:
            self._post({"cmd": "sessions.destroy", "session": self._session_id})
            logger.info("FlareSolverr session destroyed: %s", self._session_id)
        except FlareSolverrError as exc:
            logger.warning("Failed to destroy FlareSolverr session: %s", exc)
        finally:
            self._session_id = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_page(self, url: str) -> str:
        """Fetch *url* and return rendered HTML (backwards-compat wrapper)."""
        html, _cookies, _ua = self.get_page_with_cookies(url)
        return html

    def get_page_with_cookies(self, url: str) -> tuple:
        """
        Fetch *url* through FlareSolverr.

        Returns (html, cookies_dict, user_agent).
        Automatically creates/recreates the browser session as needed.
        """
        if not self._session_id:
            self.create_session()

        return self._do_get_with_cookies(url)

    def _do_get_with_cookies(self, url: str, retry: bool = True) -> tuple:
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": self._timeout_ms,
            "session": self._session_id,
        }
        try:
            result = self._post(payload)
        except FlareSolverrError:
            if retry:
                logger.warning("FlareSolverr error; recreating session and retrying")
                with self._session_lock:
                    self._session_id = None
                self.create_session()
                payload["session"] = self._session_id
                result = self._post(payload)
            else:
                raise

        status = result.get("status")
        if status != "ok":
            msg = result.get("message", "Unknown error")
            if retry and "session" in msg.lower():
                logger.warning("Session error from FlareSolverr; recreating session")
                with self._session_lock:
                    self._session_id = None
                self.create_session()
                return self._do_get_with_cookies(url, retry=False)
            raise FlareSolverrError(f"FlareSolverr returned status={status!r}: {msg}")

        solution = result.get("solution", {})
        http_status = solution.get("status", 200)
        if http_status >= 400:
            raise FlareSolverrError(
                f"ext.to returned HTTP {http_status} for {url}"
            )

        html = solution.get("response", "")
        user_agent = solution.get("userAgent", "")

        # Convert cookie list to a plain dict for requests.Session
        cookies: dict = {}
        for c in solution.get("cookies", []):
            name = c.get("name", "")
            value = c.get("value", "")
            if name:
                cookies[name] = value

        return html, cookies, user_agent

    def post_form(self, url: str, post_data: str) -> dict:
        """
        POST form-encoded *post_data* to *url* through FlareSolverr.

        Routes the request through the persistent browser session so it
        carries the same IP and cookies that solved the Cloudflare challenge.
        Automatically recreates the session on failure (same retry logic as
        GET requests).

        Returns the parsed JSON body of the response.
        """
        if not self._session_id:
            self.create_session()
        return self._do_post_form(url, post_data)

    def _do_post_form(self, url: str, post_data: str, retry: bool = True) -> dict:
        payload = {
            "cmd": "request.post",
            "url": url,
            "postData": post_data,
            "maxTimeout": self._timeout_ms,
            "session": self._session_id,
        }
        try:
            result = self._post(payload)
        except FlareSolverrError:
            if retry:
                logger.warning("FlareSolverr POST error; recreating session and retrying")
                with self._session_lock:
                    self._session_id = None
                self.create_session()
                payload["session"] = self._session_id
                result = self._post(payload)
            else:
                raise

        status = result.get("status")
        if status != "ok":
            msg = result.get("message", "Unknown error")
            if retry and "session" in msg.lower():
                logger.warning("Session error on POST; recreating session")
                with self._session_lock:
                    self._session_id = None
                self.create_session()
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
