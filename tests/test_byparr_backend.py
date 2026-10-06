"""Byparr backend: no browser sessions, no request.post.

Byparr (https://github.com/ThePhaseless/Byparr) speaks the FlareSolverr
``/v1`` request shape but ignores ``cmd``/``session``/``postData``: every call
is a fresh page load in a throw-away browser.  The client must therefore never
send ``sessions.create`` to it, and form POSTs have to be re-routed through a
direct request after refreshing the clearance cookies.
"""

import copy
import os
import sys
import threading
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.flaresolverr_client import FlareSolverrClient, FlareSolverrError  # noqa: E402
from app.site_client import SiteClient, SiteUnavailable  # noqa: E402


def _solution(**extra):
    body = {"status": 200, "response": "<html>ok</html>", "cookies": [], "userAgent": "UA"}
    body.update(extra)
    return {"status": "ok", "message": "Success", "solution": body}


class _FakePost:
    def __init__(self, script):
        self.script = list(script)
        self.posted = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.posted.append(copy.deepcopy(json))
        action = self.script.pop(0) if self.script else _solution()
        if isinstance(action, Exception):
            raise action
        resp = mock.Mock(spec=requests.Response)
        resp.status_code = 200
        resp.json.return_value = action
        resp.raise_for_status.return_value = None
        return resp


def _probe_response(status_code, json_body=None):
    resp = mock.Mock(spec=requests.Response)
    resp.status_code = status_code
    if json_body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body
    return resp


class BackendDetectionTest(unittest.TestCase):
    def _client(self, backend, probe):
        client = FlareSolverrClient("http://solver.test:8191", session_idle=0, backend=backend)
        client._http = mock.Mock()
        client._http.get.side_effect = probe
        client._http.post.side_effect = _FakePost([])
        return client

    def test_redirect_on_root_means_byparr(self):
        client = self._client("auto", [_probe_response(301)])
        self.assertEqual(client.backend, "byparr")
        client._http.get.assert_called_once()
        self.assertFalse(client._http.get.call_args.kwargs.get("allow_redirects", True))

    def test_byparr_json_means_byparr(self):
        client = self._client("auto", [_probe_response(200, {"msg": "Byparr is working!"})])
        self.assertEqual(client.backend, "byparr")

    def test_flaresolverr_json_means_flaresolverr(self):
        client = self._client("auto", [_probe_response(200, {"msg": "FlareSolverr is ready!"})])
        self.assertEqual(client.backend, "flaresolverr")

    def test_unknown_answer_defaults_to_flaresolverr(self):
        client = self._client("auto", [_probe_response(200)])
        self.assertEqual(client.backend, "flaresolverr")

    def test_detection_result_is_cached(self):
        client = self._client("auto", [_probe_response(301)])
        client.backend
        client.backend
        self.assertEqual(client._http.get.call_count, 1)

    def test_failed_probe_is_not_cached(self):
        client = self._client(
            "auto",
            [requests.exceptions.ConnectionError("down"), _probe_response(301)],
        )
        with self.assertRaises(FlareSolverrError):
            client.backend
        self.assertEqual(client.backend, "byparr")

    def test_explicit_backend_skips_probe(self):
        for name in ("byparr", "flaresolverr"):
            client = self._client(name, [_probe_response(301)])
            self.assertEqual(client.backend, name)
            client._http.get.assert_not_called()

    def test_invalid_backend_rejected(self):
        with self.assertRaises(ValueError):
            FlareSolverrClient("http://solver.test:8191", backend="chrome")


class ByparrGetTest(unittest.TestCase):
    def _client(self, script):
        client = FlareSolverrClient("http://solver.test:8191", session_idle=0, backend="byparr")
        fake = _FakePost(script)
        client._http = mock.Mock()
        client._http.post.side_effect = fake
        return client, fake

    def test_get_sends_no_session_commands(self):
        client, fake = self._client([
            _solution(cookies=[{"name": "cf_clearance", "value": "abc"}]),
        ])
        html, cookies, ua = client.get_page_with_cookies("https://ext.to/browse/")
        self.assertEqual(html, "<html>ok</html>")
        self.assertEqual(
            cookies, [{"name": "cf_clearance", "value": "abc", "domain": "", "path": "/"}]
        )
        self.assertEqual(ua, "UA")
        self.assertEqual([p["cmd"] for p in fake.posted], ["request.get"])
        self.assertNotIn("session", fake.posted[0])
        self.assertNotIn("tabs_till_verify", fake.posted[0])
        self.assertIsNone(client.session_id)

    def test_transport_error_is_retried_once_without_sessions(self):
        client, fake = self._client([
            requests.exceptions.Timeout("t1"),
            _solution(),
        ])
        html, _c, _u = client.get_page_with_cookies("https://ext.to/browse/")
        self.assertEqual(html, "<html>ok</html>")
        self.assertEqual([p["cmd"] for p in fake.posted], ["request.get", "request.get"])

    def test_second_transport_error_raises(self):
        client, _fake = self._client([
            requests.exceptions.Timeout("t1"),
            requests.exceptions.Timeout("t2"),
        ])
        with self.assertRaises(FlareSolverrError):
            client.get_page_with_cookies("https://ext.to/browse/")

    def test_http_error_body_is_surfaced(self):
        client = FlareSolverrClient("http://solver.test:8191", session_idle=0, backend="byparr")
        resp = mock.Mock(spec=requests.Response)
        resp.status_code = 502
        resp.text = '{"detail":"Could not reach the target"}'
        err = requests.exceptions.HTTPError(response=resp)
        client._http = mock.Mock()
        client._http.post.side_effect = err
        with self.assertRaises(FlareSolverrError) as ctx:
            client.get_page_with_cookies("https://ext.to/browse/")
        self.assertIn("502", str(ctx.exception))

    def test_post_form_is_unsupported(self):
        client, fake = self._client([])
        self.assertFalse(client.supports_post)
        with self.assertRaises(FlareSolverrError):
            client.post_form("https://ext.to/ajax/x.php", "a=1")
        self.assertEqual(fake.posted, [])

    def test_close_without_session_is_a_noop(self):
        client, fake = self._client([])
        client.close()
        self.assertEqual(fake.posted, [])


class FlareSolverrModeStillUsesSessions(unittest.TestCase):
    def test_flaresolverr_backend_creates_session(self):
        client = FlareSolverrClient("http://solver.test:8191", session_idle=0, backend="flaresolverr")
        fake = _FakePost([{"status": "ok", "session": "s1"}, _solution()])
        client._http = mock.Mock()
        client._http.post.side_effect = fake
        client.get_page_with_cookies("https://ext.to/browse/")
        self.assertEqual([p["cmd"] for p in fake.posted], ["sessions.create", "request.get"])
        self.assertTrue(client.supports_post)

    def test_auto_detection_during_first_session_create_does_not_deadlock(self):
        # create_session holds _session_lock and asks for the backend; the
        # probe must not take that same lock again.
        client = FlareSolverrClient("http://solver.test:8191", session_idle=0, backend="auto")
        fake = _FakePost([{"status": "ok", "session": "s1"}, _solution()])
        client._http = mock.Mock()
        client._http.get.side_effect = [_probe_response(200, {"msg": "FlareSolverr is ready!"})]
        client._http.post.side_effect = fake
        worker = threading.Thread(
            target=client.get_page_with_cookies, args=("https://ext.to/browse/",), daemon=True,
        )
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "first solver call deadlocked")
        self.assertEqual([p["cmd"] for p in fake.posted], ["sessions.create", "request.get"])


class _FakeSolver:
    """Solver double for SiteClient: records page fetches, hands out cookies."""

    def __init__(self, supports_post=False, fail=False):
        self.supports_post = supports_post
        self.fail = fail
        self.fetched = []

    def get_page_with_cookies(self, url):
        self.fetched.append(url)
        if self.fail:
            raise FlareSolverrError("solver down")
        return (
            "<html>solved</html>",
            [{"name": "cf_clearance", "value": "fresh", "domain": ".ext.to", "path": "/"}],
            "SolverUA",
        )

    def post_form(self, url, data):
        raise AssertionError("post_form must not be used")


def _http_response(status, text):
    resp = mock.Mock(spec=requests.Response)
    resp.status_code = status
    resp.text = text
    resp.headers = {}
    return resp


class SiteClientPostWithoutBrowserPostTest(unittest.TestCase):
    def test_challenged_post_resolves_then_retries_direct(self):
        solver = _FakeSolver()
        client = SiteClient("https://ext.to", flaresolverr=solver, prefer_direct=True)
        client._session = mock.Mock()
        client._session.headers = {}
        client._session.cookies = mock.Mock()
        client._session.post.side_effect = [
            _http_response(403, "Just a moment"),
            _http_response(200, '{"success": true, "magnet": "magnet:?x"}'),
        ]

        result = client.post_json("/ajax/x.php", {"a": "1"})

        self.assertEqual(result, {"success": True, "magnet": "magnet:?x"})
        self.assertEqual(solver.fetched, ["https://ext.to/browse/"])
        self.assertEqual(client._session.post.call_count, 2)
        self.assertEqual(client._session.headers["User-Agent"], "SolverUA")
        client._session.cookies.set.assert_called_with(
            "cf_clearance", "fresh", domain=".ext.to", path="/"
        )
        self.assertEqual(client.transport, "direct")

    def test_second_direct_failure_after_resolve_is_unavailable(self):
        solver = _FakeSolver()
        client = SiteClient("https://ext.to", flaresolverr=solver, prefer_direct=True)
        client._session = mock.Mock()
        client._session.headers = {}
        client._session.cookies = mock.Mock()
        client._session.post.side_effect = [
            _http_response(403, "Just a moment"),
            _http_response(403, "Just a moment"),
        ]
        with self.assertRaises(SiteUnavailable):
            client.post_json("/ajax/x.php", {"a": "1"})
        self.assertEqual(client._session.post.call_count, 2)

    def test_solver_failure_is_unavailable(self):
        solver = _FakeSolver(fail=True)
        client = SiteClient("https://ext.to", flaresolverr=solver, prefer_direct=True)
        client._session = mock.Mock()
        client._session.headers = {}
        client._session.cookies = mock.Mock()
        client._session.post.side_effect = [_http_response(403, "Just a moment")]
        with self.assertRaises(SiteUnavailable):
            client.post_json("/ajax/x.php", {"a": "1"})

    def test_blocked_direct_still_resolves_first(self):
        """With direct backed off, a POST-less solver must not be asked to POST."""
        solver = _FakeSolver()
        client = SiteClient("https://ext.to", flaresolverr=solver, prefer_direct=True)
        client._session = mock.Mock()
        client._session.headers = {}
        client._session.cookies = mock.Mock()
        client._session.post.side_effect = [
            _http_response(200, '{"success": true}'),
        ]
        client._direct_blocked_until = float("inf")
        self.assertEqual(client.transport, "flaresolverr")

        result = client.post_json("/ajax/x.php", {"a": "1"})

        self.assertEqual(result, {"success": True})
        self.assertEqual(solver.fetched, ["https://ext.to/browse/"])
        self.assertEqual(client._session.post.call_count, 1)

    def test_flaresolverr_backend_keeps_browser_post(self):
        class PostingSolver(_FakeSolver):
            def __init__(self):
                super().__init__(supports_post=True)
                self.posted = []

            def post_form(self, url, data):
                self.posted.append((url, data))
                return {"success": True, "via": "browser"}

        solver = PostingSolver()
        client = SiteClient("https://ext.to", flaresolverr=solver, prefer_direct=True)
        client._session = mock.Mock()
        client._session.headers = {}
        client._session.cookies = mock.Mock()
        client._session.post.side_effect = [_http_response(403, "Just a moment")]

        result = client.post_json("/ajax/x.php", {"a": "1"})

        self.assertEqual(result["via"], "browser")
        self.assertEqual(solver.fetched, [])
        self.assertEqual(len(solver.posted), 1)


class ClearanceRefreshSingleFlightTest(unittest.TestCase):
    def _client(self, solver, posts):
        client = SiteClient("https://ext.to", flaresolverr=solver, prefer_direct=True)
        client._session = mock.Mock()
        client._session.headers = {}
        client._session.cookies = mock.Mock()
        client._session.post.side_effect = posts
        return client

    def test_solve_finished_after_failure_is_reused(self):
        import time as _time

        solver = _FakeSolver()
        client = self._client(solver, [
            _http_response(403, "Just a moment"),
            _http_response(200, '{"success": true}'),
        ])
        # Another worker solved a moment after this one will notice its 403.
        client._last_solve_at = _time.time() + 5
        result = client.post_json("/ajax/x.php", {"a": "1"})
        self.assertEqual(result, {"success": True})
        self.assertEqual(solver.fetched, [])

    def test_recent_unhelpful_solve_short_circuits(self):
        import time as _time

        solver = _FakeSolver()
        client = self._client(solver, [_http_response(403, "Just a moment")])
        client._last_solve_at = _time.time() - 10
        with self.assertRaises(SiteUnavailable) as ctx:
            client.post_json("/ajax/x.php", {"a": "1"})
        self.assertIn("not being honoured", str(ctx.exception))
        self.assertEqual(solver.fetched, [])
        self.assertEqual(client._session.post.call_count, 1)

    def test_old_solve_is_refreshed(self):
        import time as _time

        solver = _FakeSolver()
        client = self._client(solver, [
            _http_response(403, "Just a moment"),
            _http_response(200, '{"success": true}'),
        ])
        client._last_solve_at = _time.time() - 600
        client.post_json("/ajax/x.php", {"a": "1"})
        self.assertEqual(solver.fetched, ["https://ext.to/browse/"])
        self.assertGreater(client._last_solve_at, _time.time() - 5)

    def test_concurrent_workers_share_one_solve(self):
        import threading
        import time as _time

        class SlowSolver(_FakeSolver):
            def get_page_with_cookies(self, url):
                _time.sleep(0.2)
                return super().get_page_with_cookies(url)

        solver = SlowSolver()
        client = SiteClient("https://ext.to", flaresolverr=solver, prefer_direct=True)
        client._session = mock.Mock()
        client._session.headers = {}
        client._session.cookies = mock.Mock()
        # Every first attempt is challenged, every retry succeeds.
        calls = {"n": 0}
        lock = threading.Lock()

        def post(url, data=None, headers=None, timeout=None):
            with lock:
                calls["n"] += 1
            return (
                _http_response(200, '{"success": true}')
                if client._last_solve_at
                else _http_response(403, "Just a moment")
            )

        client._session.post.side_effect = post
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(client.post_json("/ajax/x.php", {"a": "1"})))
            for _ in range(5)
        ]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(results), 5)
        self.assertEqual(len(solver.fetched), 1, "workers must share a single solve")


class PageSolveSingleFlightTest(unittest.TestCase):
    """Page fetches that all find the clearance expired share one solve."""

    def _client(self, solver):
        client = SiteClient("https://ext.to", flaresolverr=solver, prefer_direct=True)
        client._session = mock.Mock()
        client._session.headers = {}
        client._session.cookies = mock.Mock()
        return client

    def test_concurrent_challenged_gets_share_one_solve(self):
        import threading
        import time as _time

        class SlowSolver(_FakeSolver):
            def get_page_with_cookies(self, url):
                _time.sleep(0.3)
                return super().get_page_with_cookies(url)

        solver = SlowSolver()
        client = self._client(solver)
        # Challenged until a solve has happened, fine afterwards.
        client._session.get.side_effect = lambda url, **kw: (
            _http_response(200, "<html>direct</html>")
            if client._last_solve_at
            else _http_response(403, "Just a moment")
        )
        pages = []
        threads = [
            threading.Thread(target=lambda: pages.append(client.get("/browse/", {"q": "x"})))
            for _ in range(5)
        ]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(solver.fetched), 1, "requests must share a single solve")
        self.assertEqual(sorted(pages), ["<html>direct</html>"] * 4 + ["<html>solved</html>"])

    def test_solve_before_the_failure_does_not_count(self):
        import time as _time

        solver = _FakeSolver()
        client = self._client(solver)
        client._session.get.return_value = _http_response(403, "Just a moment")
        client._last_solve_at = _time.time() - 60
        self.assertEqual(client.get("/browse/"), "<html>solved</html>")
        self.assertEqual(len(solver.fetched), 1)

    def test_clearance_age_is_reported(self):
        solver = _FakeSolver()
        client = self._client(solver)
        self.assertIsNone(client.clearance_status()["clearance_age_s"])
        client._session.get.return_value = _http_response(403, "Just a moment")
        client.get("/browse/")
        self.assertEqual(client.clearance_status()["clearance_age_s"], 0)


class CookieDomainTest(unittest.TestCase):
    def test_cookie_domains_are_preserved(self):
        client = SiteClient("https://search.extto.com", flaresolverr=None)
        client._import_browser_state(
            [
                {"name": "cf_clearance", "value": "v", "domain": ".extto.com", "path": "/"},
                {"name": "PHPSESSID", "value": "p", "domain": "extto.com", "path": "/"},
                {"name": "bare", "value": "b", "domain": "", "path": ""},
            ],
            "UA",
        )
        jar = {(c.name, c.domain, c.path) for c in client._session.cookies}
        self.assertIn(("cf_clearance", ".extto.com", "/"), jar)
        self.assertIn(("PHPSESSID", "extto.com", "/"), jar)
        self.assertIn(("bare", "search.extto.com", "/"), jar)
        self.assertEqual(client._session.headers["User-Agent"], "UA")

    def test_legacy_dict_cookies_land_on_host(self):
        client = SiteClient("https://search.extto.com", flaresolverr=None)
        client._import_browser_state({"cf_clearance": "v"}, "")
        jar = {(c.name, c.domain) for c in client._session.cookies}
        self.assertIn(("cf_clearance", "search.extto.com"), jar)

    def test_rotate_site_session_clears_by_name_across_domains(self):
        client = SiteClient("https://search.extto.com", flaresolverr=None)
        client._import_browser_state(
            [
                {"name": "cf_clearance", "value": "v", "domain": ".extto.com", "path": "/"},
                {"name": "PHPSESSID", "value": "p", "domain": "extto.com", "path": "/"},
            ],
            "UA",
        )
        client.rotate_site_session()
        names = {c.name for c in client._session.cookies}
        self.assertEqual(names, {"cf_clearance"})


if __name__ == "__main__":
    unittest.main()
