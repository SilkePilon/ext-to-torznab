"""
Release-title matching for structured (*arr) searches.

ext.to's search is a plain keyword search sorted by age, so a season search
for ``Show S04`` comes back as a page of the newest single episodes and the
season packs never make it onto the page.  This module

* expands a season search into several query variants that each surface a
  different pack naming style (``S04``, ``Season 4``, ``Complete``), and
* classifies every returned title so wrong shows, wrong seasons and wrong
  episodes can be dropped before the results reach Sonarr / Prowlarr.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

_SEP = r"[ ._\-]"

# S04E05, S04 E05, S04E05E06, S04E05-E08, S04E05-08
_RE_SXXEYY = re.compile(
    rf"\bS(\d{{1,3}}){_SEP}?E(\d{{1,4}})(?:{_SEP}?-?{_SEP}?E(\d{{1,4}})|-(\d{{1,3}})\b)?",
    re.IGNORECASE,
)
# 4x05
_RE_NXNN = re.compile(r"\b(\d{1,2})x(\d{2,3})\b", re.IGNORECASE)
# Season 4 Episode 5, Season 04 Ep 05
_RE_SEASON_EPISODE = re.compile(
    rf"\bSeason{_SEP}*(\d{{1,3}}){_SEP}*(?:Episode|Ep|E){_SEP}*(\d{{1,4}})\b",
    re.IGNORECASE,
)
# S01-S04, S01-04, S01 - S04, S01~S04
_RE_SEASON_RANGE = re.compile(
    rf"\bS(\d{{1,2}}){_SEP}*[-~]{_SEP}*S?(\d{{1,2}})\b(?!{_SEP}?E\d)",
    re.IGNORECASE,
)
# Seasons 1-4, Season 1 to 4, Season 1 - 4
_RE_SEASON_WORD_RANGE = re.compile(
    rf"\bSeasons?{_SEP}*(\d{{1,2}}){_SEP}*(?:-|~|to){_SEP}*(\d{{1,2}})\b",
    re.IGNORECASE,
)
# S04 (not followed by an episode marker)
_RE_SEASON = re.compile(rf"\bS(\d{{1,3}})\b(?!{_SEP}?E\d)", re.IGNORECASE)
# Season 4 (not followed by an episode marker)
_RE_SEASON_WORD = re.compile(
    rf"\bSeason{_SEP}*(\d{{1,3}})\b(?!{_SEP}*(?:Episode|Ep|E){_SEP}*\d)",
    re.IGNORECASE,
)
_RE_COMPLETE = re.compile(
    rf"\b(?:Complete|All{_SEP}Seasons|Full{_SEP}Series)\b",
    re.IGNORECASE,
)

_RE_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_RE_YEAR = re.compile(r"^(?:19|20)\d\d$")
# Words a release name often drops or reorders; not required to match.
_OPTIONAL_WORDS = frozenset({"the", "a", "an", "and"})

# Episode ranges wider than this are a parsing accident, not a release.
_MAX_EPISODE_SPAN = 200


@dataclass(frozen=True)
class ReleaseInfo:
    """What a release title says about its season / episode coverage."""

    kind: str  # "episode", "season", "complete" or "unknown"
    seasons: FrozenSet[int] = field(default_factory=frozenset)
    episodes: FrozenSet[int] = field(default_factory=frozenset)

    def has_season(self, season: int) -> bool:
        return season in self.seasons


def _span(start: int, end: int) -> FrozenSet[int]:
    if end < start or end - start > _MAX_EPISODE_SPAN:
        return frozenset({start})
    return frozenset(range(start, end + 1))


def classify(title: str) -> ReleaseInfo:
    """Work out whether *title* is an episode, a season pack or neither."""
    m = _RE_SXXEYY.search(title)
    if m:
        start = int(m.group(2))
        end = m.group(3) or m.group(4)
        episodes = _span(start, int(end)) if end else frozenset({start})
        return ReleaseInfo("episode", frozenset({int(m.group(1))}), episodes)

    for pattern in (_RE_SEASON_EPISODE, _RE_NXNN):
        m = pattern.search(title)
        if m:
            return ReleaseInfo(
                "episode", frozenset({int(m.group(1))}), frozenset({int(m.group(2))})
            )

    seasons: set = set()
    for pattern in (_RE_SEASON_RANGE, _RE_SEASON_WORD_RANGE):
        for m in pattern.finditer(title):
            seasons |= _span(int(m.group(1)), int(m.group(2)))
    if not seasons:
        for pattern in (_RE_SEASON, _RE_SEASON_WORD):
            seasons |= {int(m.group(1)) for m in pattern.finditer(title)}
    if seasons:
        return ReleaseInfo("season", frozenset(seasons))

    if _RE_COMPLETE.search(title):
        return ReleaseInfo("complete")
    return ReleaseInfo("unknown")


# ---------------------------------------------------------------------------
# Title vs. query
# ---------------------------------------------------------------------------

def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ").replace("'", "").replace("’", "")
    return _RE_NON_ALNUM.sub(" ", text).strip()


# Where the name part of a release title ends.
_RE_NAME_END = re.compile(
    rf"\bS\d{{1,3}}(?:{_SEP}?E\d{{1,4}})?\b|\b\d{{1,2}}x\d{{2,3}}\b"
    r"|\bSeasons?\b|\b(?:19|20)\d\d\b|\b(?:480|576|720|1080|2160)[pi]\b|\b4K\b"
    r"|\bComplete\b|[\[(]",
    re.IGNORECASE,
)


def _significant(words: List[str]) -> List[str]:
    return [w for w in words if w not in _OPTIONAL_WORDS and not _RE_YEAR.match(w)]


# Leading release-group / site tags: "[SubsPlease] Show - 05".
_RE_LEADING_TAGS = re.compile(r"^(?:\s*[\[(][^\])]*[\])])+")


def release_name(title: str) -> Optional[str]:
    """The show / movie name a release title starts with, or None if unclear."""
    title = _RE_LEADING_TAGS.sub("", title)
    for m in _RE_NAME_END.finditer(title):
        name = normalise(title[: m.start()])
        if name:
            return name
    return None


def title_matches(title: str, query: str) -> bool:
    """True when *title* names exactly what *query* asked for.

    The release name (everything before the first season / episode / year /
    resolution marker) must equal the query, so ``Rookie Blue S04`` does not
    pass for ``The Rookie``.  Years and filler words are ignored, and a second
    comparison with all spaces removed covers ``S.H.I.E.L.D.`` vs ``SHIELD``
    and ``Spider-Man`` vs ``Spiderman``.  Titles without any marker fall back
    to requiring every query word.
    """
    wanted = _significant(normalise(query).split())
    if not wanted:
        return True
    name = release_name(title)
    if name is None:
        words = set(normalise(title).split())
        return all(w in words for w in wanted)
    have = _significant(name.split())
    return have == wanted or "".join(have) == "".join(wanted)


# ---------------------------------------------------------------------------
# Search planning + filtering
# ---------------------------------------------------------------------------

def query_variants(query: str, season: Optional[int], episode: Optional[int]) -> List[str]:
    """Site queries to run for one Torznab request, most specific first."""
    query = query.strip()
    if not query or season is None:
        return [query]
    if episode is not None:
        return [f"{query} S{season:02d}E{episode:02d}"]
    return [
        f"{query} S{season:02d}",
        f"{query} Season {season}",
        f"{query} Complete",
    ]


def split_matches(
    results: List[Dict],
    query: str,
    season: Optional[int],
    episode: Optional[int],
) -> Tuple[List[Dict], List[Dict]]:
    """Split *results* into (wanted, fallback); everything else is dropped.

    * Season search: wanted = packs covering the season (incl. complete
      series), fallback = single episodes of that season.
    * Episode search: wanted = releases containing the episode, fallback =
      packs covering its season.
    * No season: wanted = every title that matches the query.
    """
    wanted: List[Dict] = []
    fallback: List[Dict] = []
    for item in results:
        title = item.get("title", "")
        if query and not title_matches(title, query):
            continue
        if season is None:
            wanted.append(item)
            continue

        info = classify(title)
        is_pack = info.kind == "complete" or (info.kind == "season" and info.has_season(season))
        is_episode = info.kind == "episode" and info.has_season(season)

        if episode is None:
            if is_pack:
                wanted.append(item)
            elif is_episode:
                fallback.append(item)
        elif is_episode and episode in info.episodes:
            wanted.append(item)
        elif is_pack:
            fallback.append(item)
    return wanted, fallback


_RE_QUERY_TAIL = re.compile(
    r"^(?P<show>.+?)\s+(?:"
    r"S(?P<s1>\d{1,3})(?:\s*E(?P<e1>\d{1,4}))?"
    r"|Season\s*(?P<s2>\d{1,3})(?:\s*(?:Episode|Ep)\s*(?P<e2>\d{1,4}))?"
    r")(?:\s+(?:complete|pack|full))*\s*$",
    re.IGNORECASE,
)


def split_query(query: str) -> Tuple[str, Optional[int], Optional[int]]:
    """Pull a trailing ``S04`` / ``S04E05`` / ``Season 4`` off a free-text query.

    Lets a manual search such as ``Show S04`` or ``Show Season 4 complete``
    get the same pack handling as a structured Sonarr season search.
    """
    m = _RE_QUERY_TAIL.match(query.strip())
    if not m:
        return query, None, None
    season = m.group("s1") or m.group("s2")
    episode = m.group("e1") or m.group("e2")
    return m.group("show").strip(), int(season), int(episode) if episode else None
