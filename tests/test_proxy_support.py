"""Proxy support for the direct site client and the FlareSolverr browser."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.flaresolverr_client import FlareSolverrClient
from app.site_client import SiteClient, SitePool


class ProxySupportTest(unittest.TestCase):
    PROXY = "http://127.0.0.1:1080"

    def test_site_client_uses_proxy(self):
        client = SiteClient("https://ext.to", proxy_url=self.PROXY)
        self.assertFalse(client._session.trust_env)
        self.assertEqual(client._session.proxies["http"], self.PROXY)
        self.assertEqual(client._session.proxies["https"], self.PROXY)

    def test_site_pool_passes_proxy(self):
        pool = SitePool(["https://ext.to"], proxy_url=self.PROXY)
        self.assertEqual(pool.clients[0]._session.proxies["https"], self.PROXY)

    def test_flaresolverr_forwards_proxy_to_browser(self):
        client = FlareSolverrClient(
            "http://fs.test:8191",
            session_idle=0,
            backend="flaresolverr",
            proxy_url=self.PROXY,
        )
        payload = client._get_payload("request.get", "https://ext.to/")
        self.assertEqual(payload["proxy"], {"url": self.PROXY})
        self.assertFalse(client._http.trust_env)

    def test_flaresolverr_omits_proxy_for_byparr(self):
        client = FlareSolverrClient(
            "http://fs.test:8191",
            session_idle=0,
            backend="byparr",
            proxy_url=self.PROXY,
        )
        payload = client._get_payload("request.get", "https://ext.to/")
        self.assertNotIn("proxy", payload)

    def test_no_proxy_config_is_a_noop(self):
        client = SiteClient("https://ext.to")
        self.assertEqual(client._session.proxies, {})
        fs = FlareSolverrClient(
            "http://fs.test:8191", session_idle=0, backend="flaresolverr"
        )
        self.assertNotIn("proxy", fs._get_payload("request.get", "https://ext.to/"))


if __name__ == "__main__":
    unittest.main()
