"""
Torznab XML response builder.

Produces standards-compliant Torznab RSS 2.0 XML as expected by
Sonarr, Lidarr, Radarr, and other *arr applications.

Spec reference: https://torznab.github.io/spec-1.3-draft/
"""

from datetime import datetime, timezone
from typing import Dict, List
from xml.etree import ElementTree as ET

from .categories import TORZNAB_CATEGORIES

_TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
_NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"

# Register namespaces so ElementTree renders clean prefixes
ET.register_namespace("torznab", _TORZNAB_NS)
ET.register_namespace("newznab", _NEWZNAB_NS)

_XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n'


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

def build_caps_response(base_url: str) -> str:
    """Return a Torznab caps XML document as a string."""
    root = ET.Element("caps")

    server = ET.SubElement(root, "server")
    server.set("version", "1.1")
    server.set("title", "EXT Torrents")
    server.set("strapline", "EXT Torrents Torznab Proxy")
    server.set("url", base_url)

    limits = ET.SubElement(root, "limits")
    limits.set("max", "100")
    limits.set("default", "25")

    searching = ET.SubElement(root, "searching")

    def _add_mode(tag: str, available: bool, params: str) -> None:
        el = ET.SubElement(searching, tag)
        el.set("available", "yes" if available else "no")
        el.set("supportedParams", params)

    _add_mode("search",       True, "q")
    _add_mode("tv-search",    True, "q,season,ep,imdbid")
    _add_mode("movie-search", True, "q,imdbid")
    _add_mode("audio-search", True, "q")
    _add_mode("book-search",  True, "q")

    cats_el = ET.SubElement(root, "categories")
    for cat_id, cat_name, subcats in TORZNAB_CATEGORIES:
        cat_el = ET.SubElement(cats_el, "category")
        cat_el.set("id", str(cat_id))
        cat_el.set("name", cat_name)
        for sub_id, sub_name in subcats:
            sub_el = ET.SubElement(cat_el, "subcat")
            sub_el.set("id", str(sub_id))
            sub_el.set("name", sub_name)

    return _XML_HEADER + ET.tostring(root, encoding="unicode")


# ---------------------------------------------------------------------------
# Search results
# ---------------------------------------------------------------------------

def build_search_response(
    results: List[Dict],
    offset: int = 0,
    total: int = 0,
    base_url: str = "",
    apikey: str = "",
) -> str:
    """Return a Torznab search RSS 2.0 document as a string.

    *base_url* and *apikey* are used to construct self-referencing
    ``t=download`` URLs for results that don't yet have a magnet link.
    This lets *arr apps call back to us so we can resolve the magnet
    on-demand through FlareSolverr instead of handing them a raw
    Cloudflare-protected page URL.
    """
    rss = ET.Element("rss")
    rss.set("version", "2.0")

    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "EXT Torrents"
    ET.SubElement(channel, "link").text = "https://ext.to"
    ET.SubElement(channel, "description").text = "EXT Torrents Torznab Proxy"
    ET.SubElement(channel, "language").text = "en-US"
    ET.SubElement(channel, "category").text = "search"

    # Newznab response element (used by *arr for pagination)
    resp_el = ET.SubElement(channel, f"{{{_NEWZNAB_NS}}}response")
    resp_el.set("offset", str(offset))
    resp_el.set("total", str(total or len(results)))

    now_rfc = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    for torrent in results:
        item = ET.SubElement(channel, "item")

        title = torrent.get("title", "Unknown")
        ET.SubElement(item, "title").text = title

        guid_val = torrent.get("guid", torrent.get("details_url", ""))
        guid_el = ET.SubElement(item, "guid")
        guid_el.text = guid_val
        guid_el.set("isPermaLink", "true")

        magnet = torrent.get("magnet_url", "")
        raw_download_url = torrent.get("download_url", "")

        # If we don't have a magnet yet, point <link> and <enclosure> back at
        # our own t=download endpoint.  The *arr app will call that URL, we
        # resolve the magnet on-demand via FlareSolverr, and redirect.
        if magnet:
            download_url = raw_download_url  # already a magnet:// URL
        elif base_url and raw_download_url.startswith("http"):
            qs = f"t=download&guid={raw_download_url}"
            if apikey:
                qs += f"&apikey={apikey}"
            download_url = f"{base_url}/api?{qs}"
        else:
            download_url = raw_download_url

        ET.SubElement(item, "link").text = download_url

        pub_date = torrent.get("pub_date") or now_rfc
        ET.SubElement(item, "pubDate").text = pub_date

        size = torrent.get("size", 0)
        ET.SubElement(item, "size").text = str(size)
        ET.SubElement(item, "description").text = title

        # Enclosure – the element Sonarr/Lidarr use to grab the download link
        encl = ET.SubElement(item, "enclosure")
        encl.set("url", download_url)
        encl.set("length", str(size))
        if magnet:
            encl.set("type", "application/x-bittorrent;x-scheme-handler/magnet")
        else:
            encl.set("type", "application/x-bittorrent")

        # ---------- Torznab extended attributes ----------
        def _attr(name: str, value) -> None:
            if value is None or str(value) == "":
                return
            el = ET.SubElement(item, f"{{{_TORZNAB_NS}}}attr")
            el.set("name", name)
            el.set("value", str(value))

        for cat_id in torrent.get("categories", [8000]):
            _attr("category", cat_id)

        _attr("size", size)
        _attr("seeders", torrent.get("seeders", 0))
        _attr("leechers", torrent.get("leechers", 0))
        _attr("peers", torrent.get("peers", 0))

        infohash = torrent.get("infohash", "")
        if infohash:
            _attr("infohash", infohash)

        if magnet:
            _attr("magneturl", magnet)

        files = torrent.get("files", 1)
        if files:
            _attr("files", files)

        _attr("downloadvolumefactor", 0)
        _attr("uploadvolumefactor", 1)

    return _XML_HEADER + ET.tostring(rss, encoding="unicode")


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

def build_error_response(code: int, description: str) -> str:
    """Return a Torznab error XML document as a string."""
    error = ET.Element("error")
    error.set("code", str(code))
    error.set("description", description)
    return _XML_HEADER + ET.tostring(error, encoding="unicode")
