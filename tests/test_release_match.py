"""Offline tests for release matching and show-page season / episode search.

Run with:  python -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.release_match import (  # noqa: E402
    classify,
    query_variants,
    split_matches,
    split_query,
    title_matches,
)
from app.scraper import ExtToScraper  # noqa: E402
from app.site_client import SitePool  # noqa: E402


def row(title: str, torrent_id: int, seeds: int = 1, show: str = "", tooltip_extra: str = "") -> str:
    watch = f'<a class="vide-watch-btn" href="{show}"></a>' if show else ""
    return f"""
<tr>
  <td class="text-left">
    <div class="float-left">
      <a href="/t-{torrent_id}/" class="torrent-title-link"
         data-tooltip="{title}{tooltip_extra}"><b>{title}</b></a>
      <div class="related-posted">in <a href="/tv/"><strong>TV</strong></a></div>
    </div>
    <div class="btn-blocks float-right">
      {watch}
      <a class="dwn-btn search-magnet-btn" href="javascript:void(0);" data-id="{torrent_id}"></a>
    </div>
  </td>
  <td><div><span class="add-block">Size</span><span>1 GB</span></div></td>
  <td><div><span class="add-block">Files</span><span>1</span></div></td>
  <td><div><span class="add-block">Age</span><span>1 day ago</span></div></td>
  <td><div><span class="text-success">{seeds}</span></div></td>
  <td><div><span class="text-danger">0</span></div></td>
</tr>"""


def table(*rows: str) -> str:
    return '<table class="table table-striped search-table"><tbody>' + "".join(rows) + "</tbody></table>"


SHOW = "/the-rookie-s109/"
BADGE = ' &lt;span class=&quot;imdbSearchButton&quot;&gt;720P&lt;/span&gt;'

# Keyword search for "The Rookie S04": newest single episodes only.
SEARCH_PAGE = table(
    row("The Rookie S04E22 1080p WEB H264-NTb", 1, 50, SHOW),
    row("The Rookie S04E21 1080p WEB H264-NTb", 2, 40, SHOW),
    row("Rookie Blue S04E01 720p HDTV", 3, 5, "/rookie-blue-s6008/"),
)
# /the-rookie-s109/torrents/?s=4&completed=1
COMPLETED_PAGE = table(
    row("The Rookie S04 COMPLETE 720p AMZN WEBRip x264-GalaxyTV", 10, 131, tooltip_extra=BADGE),
    row("The Rookie Season 1-5 Collection 1080p WEBDL", 11, 42),
    row("The Rookie (2018) Season 4 S04 1080p AMZN WEB-DL", 12, 146),
)
# /the-rookie-s109/torrents/?s=4&e=5
EPISODE_PAGE = table(
    row("The Rookie S04 COMPLETE 720p AMZN WEBRip x264-GalaxyTV", 10, 131),
    row("The Rookie S04E05 A C H 720p DSNP WEB-DL", 20, 30),
    row("The Rookie S04E05 1080p WEB H264-PECULATE", 21, 60),
)


def scraper_with_pages(pages):
    """Scraper whose pool serves *pages*: path → html (or callable)."""
    scraper = ExtToScraper(SitePool(["https://extto.com"], flaresolverr=None), resolve_magnets="never")
    client = scraper.pool.active
    calls = []

    def fake_get(path, params=None):
        calls.append((path, dict(params or {})))
        html = pages.get(path, "")
        return (html(params) if callable(html) else html), client

    scraper._pool.get = fake_get  # type: ignore[method-assign]
    return scraper, calls


class ClassifyTest(unittest.TestCase):
    def test_episodes(self):
        self.assertEqual(classify("Show S04E05 1080p").kind, "episode")
        self.assertEqual(classify("Show S04E05 1080p").episodes, {5})
        self.assertEqual(classify("Show.S04E01E02.1080p").episodes, {1, 2})
        self.assertEqual(classify("Show S04E01-E03 720p").episodes, {1, 2, 3})
        self.assertEqual(classify("Show 4x05 HDTV").seasons, {4})
        self.assertEqual(classify("Show Season 4 Episode 5").episodes, {5})

    def test_resolution_is_not_an_episode(self):
        info = classify("Show 1920x1080 S02")
        self.assertEqual((info.kind, info.seasons), ("season", frozenset({2})))
        self.assertEqual(classify("Show S04E05-1080p").episodes, {5})

    def test_season_packs(self):
        for title, seasons in [
            ("The Rookie S04 COMPLETE 720p", {4}),
            ("The Rookie Season 4 - threesixtyp", {4}),
            ("The.Rookie.S01-S07.web.sdr.1080p", set(range(1, 8))),
            ("The Rookie Season 1-5 Collection 1080p", set(range(1, 6))),
            ("The Rookie - Season 01-04 (2018-2022)[COMPLETE]", {1, 2, 3, 4}),
        ]:
            info = classify(title)
            self.assertEqual(info.kind, "season", title)
            self.assertEqual(info.seasons, seasons, title)

    def test_complete_series_and_unknown(self):
        self.assertEqual(classify("Chernobyl Complete 1080p").kind, "complete")
        self.assertEqual(classify("Some Movie 2019 1080p").kind, "unknown")


class TitleMatchTest(unittest.TestCase):
    def test_words_and_separators(self):
        self.assertTrue(title_matches("The.Rookie.S04.1080p", "The Rookie"))
        self.assertTrue(title_matches("Marvels.Agents.of.S.H.I.E.L.D.S04", "Marvel's Agents of SHIELD"))
        self.assertTrue(title_matches("Law & Order S01", "Law and Order"))
        self.assertTrue(title_matches("Pokemon S01", "Pokémon"))

    def test_leading_group_tag_is_ignored(self):
        self.assertTrue(title_matches("[SubsPlease] One Piece - 1100 (1080p)", "One Piece 1100"))

    def test_year_and_filler_words_are_optional(self):
        self.assertTrue(title_matches("Rookie S04 1080p", "The Rookie 2018"))

    def test_other_show_is_rejected(self):
        self.assertFalse(title_matches("Rookie Blue S04E01", "The Rookie Feds"))
        self.assertFalse(title_matches("The Rookie Feds S01", "Rookie Blue"))
        self.assertFalse(title_matches("Rookie Blue S04E01", "The Rookie"))
        self.assertFalse(title_matches("Dune 2021 1080p", "Dune Part Two 2024"))


class QueryTest(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(query_variants("Show", 4, 5), ["Show S04E05"])
        self.assertEqual(
            query_variants("Show", 4, None), ["Show S04", "Show Season 4", "Show Complete"]
        )
        self.assertEqual(query_variants("Show", None, None), ["Show"])

    def test_split_query(self):
        self.assertEqual(split_query("The Rookie S04"), ("The Rookie", 4, None))
        self.assertEqual(split_query("the rookie s04e05"), ("the rookie", 4, 5))
        self.assertEqual(split_query("The Rookie Season 4 complete pack"), ("The Rookie", 4, None))
        self.assertEqual(split_query("Severance"), ("Severance", None, None))


class SplitMatchesTest(unittest.TestCase):
    ITEMS = [
        {"title": "The Rookie S04 COMPLETE 720p"},
        {"title": "The Rookie S04E05 1080p"},
        {"title": "The Rookie S03E05 1080p"},
        {"title": "The Rookie S01-S07 1080p"},
        {"title": "Rookie Blue S04 720p"},
    ]

    def titles(self, items):
        return [i["title"] for i in items]

    def test_season_search_prefers_packs(self):
        wanted, fallback = split_matches(self.ITEMS, "The Rookie", 4, None)
        self.assertEqual(
            self.titles(wanted), ["The Rookie S04 COMPLETE 720p", "The Rookie S01-S07 1080p"]
        )
        self.assertEqual(self.titles(fallback), ["The Rookie S04E05 1080p"])

    def test_episode_search_prefers_the_episode(self):
        wanted, fallback = split_matches(self.ITEMS, "The Rookie", 4, 5)
        self.assertEqual(self.titles(wanted), ["The Rookie S04E05 1080p"])
        self.assertEqual(
            self.titles(fallback), ["The Rookie S04 COMPLETE 720p", "The Rookie S01-S07 1080p"]
        )


class ShowPageSearchTest(unittest.TestCase):
    def pages(self):
        return {
            "/browse/": SEARCH_PAGE,
            f"{SHOW}torrents/": lambda p: COMPLETED_PAGE if p.get("completed") else EPISODE_PAGE,
        }

    def test_season_search_returns_the_show_pages_packs(self):
        scraper, calls = scraper_with_pages(self.pages())
        results = scraper.search("The Rookie", season=4, limit=100, match_title=True)
        self.assertEqual([r["torrent_id"] for r in results], [12, 10, 11])  # by seeders
        self.assertEqual(results[1]["title"], "The Rookie S04 COMPLETE 720p AMZN WEBRip x264-GalaxyTV")
        self.assertEqual(calls[-1], (f"{SHOW}torrents/", {"s": "4", "completed": "1"}))
        self.assertEqual(results[0]["categories"], [5000])

    def test_show_path_is_cached_for_the_episode_search(self):
        scraper, calls = scraper_with_pages(self.pages())
        scraper.search("The Rookie", season=4, match_title=True)
        calls.clear()
        results = scraper.search("The Rookie", season=4, episode=5, match_title=True)
        self.assertEqual(calls, [(f"{SHOW}torrents/", {"s": "4", "e": "5"})])
        self.assertEqual([r["torrent_id"] for r in results], [21, 20])

    def test_cached_only_season_search_never_fetches(self):
        scraper, calls = scraper_with_pages(self.pages())
        self.assertEqual(scraper.search("The Rookie", season=4, match_title=True, cached_only=True), [])
        self.assertEqual(calls, [])
        live = scraper.search("The Rookie", season=4, match_title=True)
        calls.clear()
        cached = scraper.search("The Rookie", season=4, match_title=True, cached_only=True)
        self.assertEqual(calls, [])
        self.assertEqual([r["torrent_id"] for r in cached], [r["torrent_id"] for r in live])

    def test_without_a_show_page_variants_are_filtered(self):
        search = table(
            row("The Rookie S04E22 1080p", 1, 50),
            row("The Rookie Season 4 Complete 720p", 2, 10),
            row("Rookie Blue Season 4 720p", 3, 99),
            row("The Rookie S03 1080p", 4, 70),
        )
        scraper, calls = scraper_with_pages({"/browse/": search})
        results = scraper.search("The Rookie", season=4, match_title=True)
        self.assertEqual([r["torrent_id"] for r in results], [2])
        self.assertEqual(
            sorted(c[1]["q"] for c in calls),
            ["The Rookie Complete", "The Rookie S04", "The Rookie Season 4"],
        )

    def test_episodes_are_returned_when_no_pack_exists(self):
        search = table(row("The Rookie S04E22 1080p", 1, 50), row("The Rookie S04E21 1080p", 2, 40))
        scraper, _calls = scraper_with_pages({"/browse/": search})
        results = scraper.search("The Rookie", season=4, match_title=True)
        self.assertEqual([r["torrent_id"] for r in results], [1, 2])

    def test_free_text_search_is_untouched(self):
        scraper, _calls = scraper_with_pages({"/browse/": SEARCH_PAGE})
        results = scraper.search("rookie")
        self.assertEqual(len(results), 3)


if __name__ == "__main__":
    unittest.main()
