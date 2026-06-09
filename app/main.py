"""
FastAPI application – EXT Torrents Torznab Proxy.

Exposes a single /api endpoint that implements the Torznab specification
so that *arr apps (Sonarr, Lidarr, Radarr, …) can use ext.to as a source.

Supported functions (t= parameter):
  caps        – Capabilities document
  search      – Free-text search
  tvsearch    – TV search (q, season, ep, imdbid)
  movie       – Movie search (q, imdbid)
  music       – Music search (q)
  book        – Book search (q)
"""

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import RedirectResponse, Response

from .config import config
from .flaresolverr_client import FlareSolverrClient, FlareSolverrError
from .scraper import ExtToScraper
from .torznab import (
    build_caps_response,
    build_error_response,
    build_search_response,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global singletons (created in lifespan)
# ---------------------------------------------------------------------------
_fs_client: Optional[FlareSolverrClient] = None
_scraper: Optional[ExtToScraper] = None

_XML_CT = "application/rss+xml; charset=UTF-8"
_SEARCH_FUNCTIONS = {"search", "tvsearch", "movie", "music", "book"}


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    global _fs_client, _scraper

    logger.info("═══════════════════════════════════════")
    logger.info("  EXT Torrents Torznab Proxy starting  ")
    logger.info("  FlareSolverr : %s", config.FLARESOLVERR_URL)
    logger.info("  ext.to URL   : %s", config.EXT_TO_URL)
    logger.info("  Include adult: %s", config.INCLUDE_ADULT)
    logger.info("  API key set  : %s", bool(config.API_KEY))
    logger.info("═══════════════════════════════════════")

    _fs_client = FlareSolverrClient(
        config.FLARESOLVERR_URL, config.FLARESOLVERR_TIMEOUT
    )
    _scraper = ExtToScraper(config.EXT_TO_URL, _fs_client, config.INCLUDE_ADULT)

    yield

    if _fs_client:
        _fs_client.destroy_session()
    logger.info("EXT Torrents Torznab Proxy stopped")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="EXT Torrents Torznab Proxy",
    description="Torznab API proxy that scrapes ext.to via FlareSolverr",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _check_key(apikey: Optional[str]) -> bool:
    """Return True if the API key check passes (or no key is configured)."""
    if not config.API_KEY:
        return True
    return apikey == config.API_KEY


def _parse_cats(cat: Optional[str]) -> list[int]:
    if not cat:
        return []
    ids = []
    for part in cat.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


def _parse_optional_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/api?t=caps")


@app.get("/healthz", include_in_schema=False)
async def health():
    return {"status": "ok", "service": "exttorss"}


@app.get("/api")
async def torznab_api(
    request: Request,
    # ---- mandatory ----
    t: str = Query(..., description="Torznab function type"),
    # ---- common optional ----
    q: Optional[str] = Query(None, description="Search query"),
    cat: Optional[str] = Query(None, description="Comma-separated Torznab category IDs"),
    extended: Optional[str] = Query(None),
    attrs: Optional[str] = Query(None),
    apikey: Optional[str] = Query(None, description="API key (if configured)"),
    offset: int = Query(0, ge=0, description="Result offset for pagination"),
    limit: int = Query(25, ge=1, le=100, description="Max results to return"),
    # ---- TV search ----
    season: Optional[str] = Query(None, description="Season number"),
    ep: Optional[str] = Query(None, description="Episode number"),
    # ---- movie / TV ----
    imdbid: Optional[str] = Query(None, description="IMDB ID (e.g. tt1234567)"),
    tvdbid: Optional[str] = Query(None, description="TheTVDB show ID"),
    tvmazeid: Optional[str] = Query(None, description="TVMaze show ID"),
    # ---- music ----
    artist: Optional[str] = Query(None),
    album: Optional[str] = Query(None),
    label: Optional[str] = Query(None),
    year: Optional[str] = Query(None),
    genre: Optional[str] = Query(None),
):
    """
    Main Torznab API endpoint.

    Implements: caps, search, tvsearch, movie, music, book
    """
    # ---- authentication ----
    if not _check_key(apikey):
        xml = build_error_response(100, "Incorrect user credentials")
        return Response(content=xml, media_type=_XML_CT, status_code=401)

    base_url = str(request.base_url).rstrip("/")

    # ---- caps ----
    if t == "caps":
        xml = build_caps_response(base_url)
        return Response(content=xml, media_type=_XML_CT)

    # ---- search functions ----
    if t in _SEARCH_FUNCTIONS:
        # Build effective query
        search_query = (q or "").strip()

        # For music searches: combine artist + album if q is not supplied
        if t == "music" and not search_query:
            parts = [p for p in [artist, album] if p]
            search_query = " ".join(parts)

        categories = _parse_cats(cat)
        season_num = _parse_optional_int(season)
        episode_num = _parse_optional_int(ep)

        # Strip the leading "tt" from imdbid if present for normalisation;
        # the scraper will add it back
        clean_imdbid: Optional[str] = None
        if imdbid:
            clean_imdbid = imdbid if imdbid.startswith("tt") else f"tt{imdbid}"

        try:
            results = _scraper.search(
                query=search_query,
                categories=categories or None,
                imdbid=clean_imdbid,
                season=season_num,
                episode=episode_num,
                offset=offset,
                limit=limit,
            )
        except FlareSolverrError as exc:
            logger.error("FlareSolverr error during search: %s", exc)
            xml = build_error_response(900, f"FlareSolverr error: {exc}")
            return Response(content=xml, media_type=_XML_CT, status_code=500)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error during search: %s", exc)
            xml = build_error_response(900, f"Internal error: {exc}")
            return Response(content=xml, media_type=_XML_CT, status_code=500)

        xml = build_search_response(results, offset=offset, base_url=base_url, apikey=apikey or "")
        return Response(content=xml, media_type=_XML_CT)

    # ---- download (grab) ----
    if t == "download":
        guid = request.query_params.get("guid", "")
        if not guid:
            xml = build_error_response(300, "Missing guid parameter")
            return Response(content=xml, media_type=_XML_CT, status_code=400)

        # If the guid is already a magnet, redirect immediately
        if guid.startswith("magnet:"):
            return RedirectResponse(url=guid, status_code=302)

        # Try to resolve a magnet link from the guid (detail page or infohash URL)
        magnet = _scraper.fetch_magnet_for_guid(guid)
        if magnet:
            return RedirectResponse(url=magnet, status_code=302)

        # Last resort: redirect to the guid itself so the client can try
        if guid.startswith("http"):
            return RedirectResponse(url=guid, status_code=302)

        xml = build_error_response(300, "Could not resolve download URL")
        return Response(content=xml, media_type=_XML_CT, status_code=404)

    # ---- unknown function ----
    xml = build_error_response(202, f"No such function: {t}")
    return Response(content=xml, media_type=_XML_CT, status_code=400)
