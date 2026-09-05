"""Offline tests for parsing, category mapping and transport helpers.

Run with:  python -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.categories import EXT_TO_CAT_MAP, cat_id_matches, torznab_cat_to_site_cat  # noqa: E402
from app.scraper import (  # noqa: E402
    ExtToScraper,
    _TTLCache,
    _parse_int,
    _parse_size,
    _torrent_id_from_url,
)
from app.site_client import SitePool, extract_js_tokens, _looks_like_challenge  # noqa: E402
from app.flaresolverr_client import FlareSolverrClient  # noqa: E402

# One result row in the markup ext.to serves today (August 2026).
ROW_HTML = """
<table class="table table-striped table-hover search-table"><tbody>
<tr>
  <td class="text-left">
    <div class="float-left">
      <a href="/silo-s03e06-1080p-web-h264-cakes-eztv-21152668/" class="torrent-title-link"
         data-tooltip="Silo S03E06 1080p WEB H264-&lt;span&gt;CAKES&lt;/span&gt; EZTV"><b>Silo S03E06</b></a>
      <div class="related-posted">
        Posted by <a class="verify-user-link" href="/user/someone/"><strong>Someone</strong></a>
        in <a href="/tv/"><strong>TV</strong></a>
        - <a href="/tv/episodes-hd/"><strong>Episodes HD</strong></a>
      </div>
    </div>
    <div class="btn-blocks float-right">
      <a class="dwn-btn search-magnet-btn" href="javascript:void(0);" data-id="21152668"></a>
    </div>
  </td>
  <td class="nowrap-td hide-on-mob"><div class="add-block-wrapper">
    <span class="add-block">Size</span><span>2.31 GB</span></div></td>
  <td class="hide-on-mob"><div class="add-block-wrapper">
    <span class="add-block">Files</span><span>3</span></div></td>
  <td class="nowrap-td hide-on-mob"><div class="add-block-wrapper">
    <span class="add-block">Age</span><span title="13 July 2026">4 weeks ago</span></div></td>
  <td class="hide-on-mob"><div class="add-block-wrapper">
    <span class="add-block">Seeds</span><span class="text-success">142</span></div></td>
  <td class="hide-on-mob"><div class="add-block-wrapper">
    <span class="add-block">Leechs</span><span class="text-danger">17</span></div></td>
</tr>
</tbody></table>
"""

NO_RESULTS_HTML = '<html><body><a class="btn show-torrent-btn" href="#">No results</a></body></html>'


def make_scraper() -> ExtToScraper:
    pool = SitePool(["https://extto.com"], flaresolverr=None)
    return ExtToScraper(pool)


class ParseRowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scraper = make_scraper()

    def test_parses_every_field(self):
        (item,) = self.scraper._parse_html(ROW_HTML)
        self.assertEqual(item["title"], "Silo S03E06 1080p WEB H264-CAKES EZTV")
        self.assertEqual(item["torrent_id"], 21152668)
        self.assertEqual(item["size"], int(2.31 * 1024 ** 3))
        self.assertEqual(item["files"], 3)
        self.assertEqual(item["seeders"], 142)
        self.assertEqual(item["leechers"], 17)
        self.assertEqual(item["peers"], 159)
        self.assertEqual(item["pub_date"], "Mon, 13 Jul 2026 00:00:00 +0000")
        self.assertEqual(
            item["guid"], "https://extto.com/silo-s03e06-1080p-web-h264-cakes-eztv-21152668/"
        )

    def test_maps_new_tv_subcategory(self):
        (item,) = self.scraper._parse_html(ROW_HTML)
        self.assertEqual(item["categories"], [5040])

    def test_zero_hit_page_is_not_an_error(self):
        self.assertEqual(self.scraper._parse_html(NO_RESULTS_HTML), [])


class SearchParamsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scraper = make_scraper()

    def test_single_top_level_category_is_pushed_to_the_site(self):
        params = self.scraper._build_params("silo", 1, None, [5030, 5040])
        self.assertEqual(params["cat"], "2")
        self.assertEqual(params["q"], "silo")

    def test_mixed_categories_are_filtered_locally(self):
        params = self.scraper._build_params("silo", 1, None, [2000, 5000])
        self.assertNotIn("cat", params)

    def test_keywordless_request_avoids_the_advanced_redirect(self):
        params = self.scraper._build_params("", 1, None, None)
        self.assertEqual(params["cat"], "2")
        self.assertNotIn("q", params)

    def test_imdb_id_is_normalised(self):
        params = self.scraper._build_params("", 1, "0111161", None)
        self.assertEqual(params["imdb_id"], "tt0111161")


class HelperTest(unittest.TestCase):
    def test_parse_size_units(self):
        self.assertEqual(_parse_size("10.62 MB"), int(10.62 * 1024 ** 2))
        self.assertEqual(_parse_size("1 TB"), 1024 ** 4)
        self.assertEqual(_parse_size("n/a"), 0)

    def test_parse_int_strips_separators(self):
        self.assertEqual(_parse_int("1,234"), 1234)
        self.assertEqual(_parse_int(""), 0)

    def test_torrent_id_from_url(self):
        self.assertEqual(_torrent_id_from_url("https://ext.to/some-title-12345678/"), 12345678)
        self.assertIsNone(_torrent_id_from_url("https://ext.to/browse/"))

    def test_ttl_cache_expires(self):
        cache = _TTLCache(ttl=0)
        cache.set("k", "v")
        self.assertIsNone(cache.get("k"))
        cache = _TTLCache(ttl=60)
        cache.set("k", "v")
        self.assertEqual(cache.get("k"), "v")


class TokenTest(unittest.TestCase):
    def test_search_page_tokens(self):
        html = (
            '<meta name="csrf-token" content="abc123">'
            "<script>window.searchPageToken = 'deadbeef';</script>"
        )
        self.assertEqual(extract_js_tokens(html), ("deadbeef", "abc123"))

    def test_detail_page_tokens(self):
        html = "<script>window.pageToken = 'f00d';window.csrfToken = 'beef';</script>"
        self.assertEqual(extract_js_tokens(html), ("f00d", "beef"))

    def test_missing_tokens(self):
        self.assertEqual(extract_js_tokens("<html></html>"), ("", ""))


class ChallengeDetectionTest(unittest.TestCase):
    def test_cloudflare_statuses(self):
        self.assertTrue(_looks_like_challenge(403, ""))
        self.assertTrue(_looks_like_challenge(522, ""))

    def test_challenge_body(self):
        self.assertTrue(_looks_like_challenge(200, "<title>Just a moment...</title>"))

    def test_normal_page(self):
        self.assertFalse(_looks_like_challenge(200, "<table class='table-striped'>"))


class CategoryTest(unittest.TestCase):
    def test_new_subcategories_are_mapped(self):
        for key, expected in (
            ("/tv//tv/episodes-hd/", 5040),
            ("/tv//tv/episodes-4k-uhd/", 5045),
            ("/tv//tv/episodes-sd/", 5030),
            ("/anime//anime/manga-english/", 7030),
        ):
            self.assertEqual(EXT_TO_CAT_MAP[key], expected, key)

    def test_torznab_to_site_category(self):
        self.assertEqual(torznab_cat_to_site_cat(5040), 2)
        self.assertEqual(torznab_cat_to_site_cat(2000), 1)
        self.assertEqual(torznab_cat_to_site_cat(5070), 7)
        self.assertIsNone(torznab_cat_to_site_cat(6000))

    def test_parent_category_matches_subcategories(self):
        self.assertTrue(cat_id_matches(5040, 5000))
        self.assertFalse(cat_id_matches(5040, 2000))
        self.assertTrue(cat_id_matches(5040, 5040))


class MirrorPoolTest(unittest.TestCase):
    def test_duplicates_and_trailing_slashes_are_collapsed(self):
        pool = SitePool(["https://extto.com/", "https://extto.com", "https://ext.to"])
        self.assertEqual([c.base for c in pool.clients], ["https://extto.com", "https://ext.to"])

    def test_cooling_hosts_are_tried_last(self):
        pool = SitePool(["https://a.test", "https://b.test"])
        pool._mark_down(pool.clients[0])
        self.assertEqual(pool.candidates()[0].base, "https://b.test")


class PageCacheTest(unittest.TestCase):
    """The page cache must hold parsed rows, not megabytes of raw HTML."""

    def test_cache_hit_skips_fetch_and_parse(self):
        scraper = make_scraper()
        client = scraper.pool.active
        calls = []

        def fake_get(path, params=None):
            calls.append((path, params))
            return ROW_HTML, client

        scraper._pool.get = fake_get  # type: ignore[method-assign]
        first, _ = scraper._fetch_results("/browse/", {"q": "silo"})
        second, _ = scraper._fetch_results("/browse/", {"q": "silo"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["torrent_id"], 21152668)
        # Cached value is the parsed list, not the HTML string.
        key = ("/browse/", (("q", "silo"),))
        cached_results, _client = scraper._page_cache.get(key)
        self.assertIsInstance(cached_results, list)

    def test_cached_results_are_not_mutated_by_enrichment(self):
        scraper = make_scraper()
        client = scraper.pool.active
        scraper._pool.get = lambda path, params=None: (ROW_HTML, client)  # type: ignore[method-assign]
        results, _ = scraper._fetch_results("/browse/", {"q": "silo"})
        results[0]["magnet_url"] = "magnet:?xt=urn:btih:abc"
        again, _ = scraper._fetch_results("/browse/", {"q": "silo"})
        self.assertEqual(again[0]["magnet_url"], "")


class MagnetTimeBudgetTest(unittest.TestCase):
    def test_slow_lookups_do_not_block_past_budget(self):
        import time as _time

        scraper = ExtToScraper(
            SitePool(["https://extto.com"]),
            magnet_workers=4,
            magnet_time_budget=0.3,
        )
        client = scraper.pool.active
        client.remember_tokens(
            '<meta name="csrf-token" content="abc123">'
            "<script>window.searchPageToken = 'deadbeef';</script>"
        )

        def slow_post(path, data):
            _time.sleep(1.0)
            return {"success": True, "hash": "a" * 40}

        client.post_json = slow_post  # type: ignore[method-assign]
        items = [
            {"torrent_id": i, "title": f"t{i}", "magnet_url": "", "download_url": "", "infohash": ""}
            for i in range(1, 5)
        ]
        started = _time.time()
        scraper._enrich_with_magnets(items, client)
        elapsed = _time.time() - started
        self.assertLess(elapsed, 0.9, f"enrichment blocked for {elapsed:.2f}s")
        self.assertTrue(all(not i["magnet_url"] for i in items))
        # The workers finish in the background and warm the cache for grabs.
        _time.sleep(1.2)
        self.assertTrue(scraper._magnet_cache.get(1))


class FlareSolverrIdleSessionTest(unittest.TestCase):
    def test_idle_session_is_destroyed(self):
        import time as _time

        client = FlareSolverrClient("http://fs.test:8191", session_idle=0.2, reaper_interval=0.05)
        posted = []

        def fake_post(payload):
            posted.append(payload["cmd"])
            if payload["cmd"] == "sessions.create":
                return {"status": "ok", "session": "s1"}
            return {"status": "ok"}

        client._post = fake_post  # type: ignore[method-assign]
        client.create_session()
        self.assertEqual(client.session_id, "s1")
        _time.sleep(0.6)
        self.assertIsNone(client.session_id)
        self.assertIn("sessions.destroy", posted)
        client.close()

    def test_session_in_use_is_kept(self):
        import time as _time

        client = FlareSolverrClient("http://fs.test:8191", session_idle=0.2, reaper_interval=0.05)
        client._post = lambda payload: {"status": "ok", "session": "s1"}  # type: ignore[method-assign]
        client.create_session()
        for _ in range(4):
            _time.sleep(0.1)
            client.touch()
        self.assertEqual(client.session_id, "s1")
        client.close()


if __name__ == "__main__":
    unittest.main()
