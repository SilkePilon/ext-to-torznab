"""FlareSolverr client: failed browser sessions must be destroyed, not leaked.

Every leaked session keeps a ~300 MiB Chromium alive inside FlareSolverr; with
one leak per transport error the container OOMs within hours.
"""

import copy
import os
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.flaresolverr_client import FlareSolverrClient, FlareSolverrError  # noqa: E402


def _ok(session=None, **solution):
    body = {"status": "ok"}
    if session:
        body["session"] = session
    if solution:
        body["solution"] = {"status": 200, "response": "<html>ok</html>", "cookies": [], **solution}
    return body


class _FakeFlareSolverr:
    """Scripted HTTP responses keyed by command; records every posted payload."""

    def __init__(self, script):
        self.script = list(script)
        self.posted = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.posted.append(copy.deepcopy(json))  # retry paths mutate the payload in place
        action = self.script.pop(0) if self.script else _ok(solution=True)
        if isinstance(action, Exception):
            raise action
        resp = mock.Mock(spec=requests.Response)
        resp.status_code = 200
        resp.json.return_value = action
        resp.raise_for_status.return_value = None
        return resp

    def commands(self):
        return [(p["cmd"], p.get("session")) for p in self.posted]


class SessionReplacementTest(unittest.TestCase):
    def _client(self, script):
        client = FlareSolverrClient("http://fs.test:8191", session_idle=0)
        fake = _FakeFlareSolverr(script)
        client._http = mock.Mock()
        client._http.post.side_effect = fake
        return client, fake

    def _assert_old_session_destroyed(self, fake):
        cmds = fake.commands()
        self.assertEqual(cmds[0], ("sessions.create", None))
        # The failing command used s1 ...
        self.assertEqual(cmds[1][1], "s1")
        # ... then s1 is destroyed *before* s2 is created, and s2 is used.
        self.assertEqual(cmds[2], ("sessions.destroy", "s1"))
        self.assertEqual(cmds[3], ("sessions.create", None))
        self.assertEqual(cmds[4][1], "s2")
        self.assertEqual(len(cmds), 5)

    def test_get_transport_error_destroys_old_session(self):
        client, fake = self._client([
            _ok(session="s1"),
            requests.exceptions.Timeout("read timed out"),
            _ok(),                      # sessions.destroy s1
            _ok(session="s2"),
            _ok(solution=True),
        ])
        html, _cookies, _ua = client.get_page_with_cookies("https://ext.to/")
        self.assertEqual(html, "<html>ok</html>")
        self._assert_old_session_destroyed(fake)

    def test_get_session_error_destroys_old_session(self):
        client, fake = self._client([
            _ok(session="s1"),
            {"status": "error", "message": "Error: This session does not exist."},
            _ok(),
            _ok(session="s2"),
            _ok(solution=True),
        ])
        client.get_page_with_cookies("https://ext.to/")
        self._assert_old_session_destroyed(fake)

    def test_post_transport_error_destroys_old_session(self):
        client, fake = self._client([
            _ok(session="s1"),
            requests.exceptions.ConnectionError("boom"),
            _ok(),
            _ok(session="s2"),
            _ok(response='<pre>{"success": true}</pre>'),
        ])
        data = client.post_form("https://ext.to/ajax/x.php", "a=1")
        self.assertTrue(data["success"])
        self._assert_old_session_destroyed(fake)

    def test_post_session_error_destroys_old_session(self):
        client, fake = self._client([
            _ok(session="s1"),
            {"status": "error", "message": "Session s1 was expired"},
            _ok(),
            _ok(session="s2"),
            _ok(response='{"success": true}'),
        ])
        client.post_form("https://ext.to/ajax/x.php", "a=1")
        self._assert_old_session_destroyed(fake)

    def test_destroy_failure_still_creates_fresh_session(self):
        client, fake = self._client([
            _ok(session="s1"),
            requests.exceptions.Timeout("read timed out"),
            requests.exceptions.ConnectionError("destroy failed"),
            _ok(session="s2"),
            _ok(solution=True),
        ])
        client.get_page_with_cookies("https://ext.to/")
        self.assertEqual(client.session_id, "s2")

    def test_concurrent_failure_does_not_destroy_replacement(self):
        """Thread B, failing on s1 after thread A already replaced it with s2,
        must not destroy s2."""
        client, fake = self._client([
            _ok(session="s1"),
            _ok(),              # destroy s1 (thread A)
            _ok(session="s2"),  # create s2 (thread A)
        ])
        client.create_session()
        client._replace_session("s1")
        self.assertEqual(client.session_id, "s2")
        client._replace_session("s1")  # thread B, stale failed_id
        self.assertEqual(client.session_id, "s2")
        self.assertEqual(fake.commands().count(("sessions.destroy", "s2")), 0)
        self.assertEqual(fake.commands().count(("sessions.create", None)), 2)

    def test_commands_on_one_session_are_serialised(self):
        """Concurrent page fetches must not navigate the shared tab at once."""
        import threading
        import time

        client = FlareSolverrClient("http://fs.test:8191", session_idle=0)
        active = []
        overlap = []
        lock = threading.Lock()

        def fake_post(payload):
            if payload["cmd"] == "sessions.create":
                return {"status": "ok", "session": "s1"}
            with lock:
                active.append(1)
                if len(active) > 1:
                    overlap.append(1)
            time.sleep(0.05)
            with lock:
                active.pop()
            return {"status": "ok", "solution": {"status": 200, "response": "ok", "cookies": []}}

        client._post = fake_post  # type: ignore[method-assign]
        threads = [
            threading.Thread(target=client.get_page_with_cookies, args=(f"https://ext.to/{i}",))
            for i in range(6)
        ]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(overlap, [], "request.get commands overlapped on one session")

    def test_session_ttl_is_requested(self):
        client = FlareSolverrClient("http://fs.test:8191", session_idle=0, session_ttl_minutes=45)
        fake = _FakeFlareSolverr([_ok(session="s1")])
        client._http = mock.Mock()
        client._http.post.side_effect = fake
        client.create_session()
        self.assertEqual(fake.posted[0].get("session_ttl_minutes"), 45)

    def test_unrecoverable_error_raises(self):
        client, fake = self._client([
            _ok(session="s1"),
            requests.exceptions.Timeout("t1"),
            _ok(),
            _ok(session="s2"),
            requests.exceptions.Timeout("t2"),
        ])
        with self.assertRaises(FlareSolverrError):
            client.get_page_with_cookies("https://ext.to/")


if __name__ == "__main__":
    unittest.main()
