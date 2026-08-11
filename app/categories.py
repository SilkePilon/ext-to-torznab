"""
Category mappings between ext.to URL path fragments and Torznab category IDs.
Based on the Jackett exttorrents.yml definition (PR #16411).
"""

from typing import Dict, List, Optional, Tuple

# -------------------------------------------------------------------------
# ext.to URL path → Torznab category ID
# The key is formed by concatenating the 2nd and (optional) 3rd <a> hrefs
# found inside the first <td> of a result row.
# -------------------------------------------------------------------------
EXT_TO_CAT_MAP: Dict[str, int] = {
    # Anime
    "/anime/": 5070,
    "/anime//anime/audio-lossless/": 3040,
    "/anime//anime/english-translated/": 5070,
    "/anime//anime/live-action-english/": 5070,
    "/anime//anime/manga-english/": 7030,
    "/anime//anime/raw": 5070,
    "/anime//anime/raw/": 5070,
    "/anime//anime/subs/": 5070,
    "/anime/raw": 5070,
    "/anime/raw/": 5070,
    # Applications
    "/applications/": 4000,
    "/applications//applications/android/": 4070,
    "/applications//applications/ios/": 4060,
    "/applications//applications/linux/": 4000,
    "/applications//applications/mac/": 4030,
    "/applications//applications/other-applications/": 4040,
    "/applications//applications/windows/": 4010,
    # Books
    "/books/": 7000,
    "/books//books/audio-books/": 3030,
    "/books//books/comics/": 7030,
    "/books//books/ebooks/": 7020,
    # Games
    "/games/": 4050,
    "/games//games/mac/": 4030,
    "/games//games/nds/": 1000,
    "/games//games/other-games/": 1000,
    "/games//games/pc-games/": 4050,
    "/games//games/ps3/": 1000,
    "/games//games/ps4/": 1000,
    "/games//games/psp/": 1000,
    "/games//games/switch/": 1000,
    "/games//games/wii/": 1000,
    "/games//games/xbox360/": 1000,
    # Movies
    "/movies/": 2000,
    "/movies//movies/3d-movies/": 2060,
    "/movies//movies/bollywood/": 2000,
    "/movies//movies/documentary/": 2000,
    "/movies//movies/dubbed-movies/": 2000,
    "/movies//movies/dvd/": 2030,
    "/movies//movies/highres-movies/": 2040,
    "/movies//movies/movie-clips/": 2020,
    "/movies//movies/mp4/": 2000,
    "/movies//movies/music-videos/": 3020,
    "/movies//movies/other-movies/": 2020,
    "/movies//movies/ultrahd/": 2045,
    # Music
    "/music/": 3000,
    "/music//music/aac/": 3000,
    "/music//music/lossless/": 3040,
    "/music//music/mp3/": 3010,
    "/music//music/other-music/": 3050,
    "/music//music/radio-shows/": 3000,
    # TV
    "/tv/": 5000,
    "/tv//tv/big-season-packs/": 5000,
    "/tv//tv/episodes-4k-uhd/": 5045,
    "/tv//tv/episodes-hd/": 5040,
    "/tv//tv/episodes-sd/": 5030,
    "/tv//tv/season-packs/": 5000,
    "/tv//tv/sport/": 5060,
    "/tv//tv/other-tv/": 5050,
    # Other / Adult
    "/other/": 8000,
    "/video/": 6000,
    "/xxx/": 6000,
    "/xxx//xxx/games/": 6050,
    "/xxx//xxx/hentai/": 6050,
    "/xxx//xxx/magazines/": 6050,
    "/xxx//xxx/pictures/": 6060,
    "/xxx//xxx/video/": 6000,
}

# -------------------------------------------------------------------------
# Full Torznab category tree for the /api?t=caps response
# Structure: (id, name, [(subcat_id, subcat_name), ...])
# -------------------------------------------------------------------------
TORZNAB_CATEGORIES: List[Tuple[int, str, List[Tuple[int, str]]]] = [
    (1000, "Console", [
        (1010, "NDS"),
        (1020, "PSP"),
        (1030, "Wii"),
        (1040, "XBox"),
        (1050, "XBox 360"),
        (1060, "PS3"),
        (1070, "Other"),
        (1080, "PS4"),
        (1090, "Switch"),
    ]),
    (2000, "Movies", [
        (2010, "Foreign"),
        (2020, "Other"),
        (2030, "SD"),
        (2040, "HD"),
        (2045, "UHD"),
        (2050, "BluRay"),
        (2060, "3D"),
    ]),
    (3000, "Audio", [
        (3010, "MP3"),
        (3020, "Video"),
        (3030, "Audiobook"),
        (3040, "Lossless"),
        (3050, "Other"),
    ]),
    (4000, "PC", [
        (4010, "0day"),
        (4020, "ISO"),
        (4030, "Mac"),
        (4040, "Mobile-Other"),
        (4050, "Games"),
        (4060, "Mobile-iOS"),
        (4070, "Mobile-Android"),
    ]),
    (5000, "TV", [
        (5020, "Foreign"),
        (5030, "SD"),
        (5040, "HD"),
        (5045, "UHD"),
        (5050, "Other"),
        (5060, "Sport"),
        (5070, "Anime"),
        (5080, "Documentary"),
    ]),
    (6000, "XXX", [
        (6010, "DVD"),
        (6020, "WMV"),
        (6030, "XviD"),
        (6040, "x264"),
        (6050, "Other"),
        (6060, "Imageset"),
        (6070, "Pack"),
        (6080, "BluRay"),
    ]),
    (7000, "Books", [
        (7010, "Mags"),
        (7020, "EBook"),
        (7030, "Comics"),
        (7040, "Technical"),
        (7050, "Other"),
    ]),
    (8000, "Other", [
        (8010, "Misc"),
        (8020, "Hashed"),
    ]),
]


def path_to_cat_id(path: str) -> int:
    """Convert an ext.to category path fragment to a Torznab category ID."""
    return EXT_TO_CAT_MAP.get(path, 8000)


# Numeric category IDs accepted by /browse/?cat=N.  ext.to redirects its pretty
# category paths (/tv/ → /browse/?cat=2, …) to these, so using them directly
# saves a redirect and lets the site filter server-side:
#   1 Movies · 2 TV · 3 Music · 4 Games · 5 Applications · 6 Books · 7 Anime
#   8 Other

# Torznab category ID → ext.to numeric browse category
CAT_ID_TO_SITE_CAT: Dict[int, int] = {
    1000: 4,   # Console  → Games
    2000: 1,   # Movies
    3000: 3,   # Audio    → Music
    4000: 5,   # PC       → Applications
    4050: 4,   # PC/Games → Games
    5000: 2,   # TV
    5070: 7,   # TV/Anime → Anime
    7000: 6,   # Books
    8000: 8,   # Other
}


def torznab_cat_to_site_cat(cat_id: int) -> Optional[int]:
    """Map a Torznab category ID to ext.to's numeric ``cat`` parameter."""
    if cat_id in CAT_ID_TO_SITE_CAT:
        return CAT_ID_TO_SITE_CAT[cat_id]
    return CAT_ID_TO_SITE_CAT.get((cat_id // 1000) * 1000)


def cat_id_matches(result_cat: int, filter_cat: int) -> bool:
    """
    Return True if *result_cat* should be included when the client filters
    by *filter_cat*.

    Rules:
    - Exact match always succeeds.
    - If *filter_cat* is a top-level ID (multiple of 1000), any sub-category
      belonging to that parent also matches (e.g. filter 2000 matches 2040).
    """
    if result_cat == filter_cat:
        return True
    if filter_cat % 1000 == 0:
        # Top-level category: match all sub-categories in the same block
        return (result_cat // 1000) * 1000 == filter_cat
    return False
