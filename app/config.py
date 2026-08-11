import os
from typing import List

# Mirrors tried when EXT_TO_URLS is not set, in preference order.
# extto.com usually serves without a Cloudflare challenge, which lets the
# proxy use plain HTTP (~10× faster than a FlareSolverr round-trip).
_DEFAULT_MIRRORS = [
    "https://extto.com",
    "https://ext.to",
    "https://ext2.to",
]


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


class Config:
    # FlareSolverr external instance URL (leave empty to disable entirely)
    FLARESOLVERR_URL: str = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191")

    # Primary base URL for ext.to (kept for backwards compatibility)
    EXT_TO_URL: str = os.environ.get("EXT_TO_URL", "https://ext.to")

    # Comma-separated mirror list, tried in order with automatic failover.
    # EXT_TO_URL is always tried first when it is set to a non-default value.
    EXT_TO_URLS: List[str] = []

    # Optional API key to protect this proxy
    API_KEY: str = os.environ.get("API_KEY", "")

    # Server bind settings
    PORT: int = _env_int("PORT", 5000)
    HOST: str = os.environ.get("HOST", "0.0.0.0")

    # Timeout for FlareSolverr requests in milliseconds
    FLARESOLVERR_TIMEOUT: int = _env_int("FLARESOLVERR_TIMEOUT", 60000)

    # Tabs-till-verify hint for FlareSolverr Turnstile bypass (0 = disabled)
    FLARESOLVERR_TABS_TILL_VERIFY: int = _env_int("FLARESOLVERR_TABS_TILL_VERIFY", 0)

    # Timeout in seconds for plain HTTP (non-FlareSolverr) requests
    HTTP_TIMEOUT: int = _env_int("HTTP_TIMEOUT", 20)

    # Try plain HTTP before falling back to FlareSolverr
    PREFER_DIRECT: bool = _env_bool("PREFER_DIRECT", True)

    # Seconds a mirror stays in cooldown after a failure
    MIRROR_COOLDOWN: int = _env_int("MIRROR_COOLDOWN", 300)

    # Resolve magnet links during search: "auto" (only on the fast direct
    # transport), "always", or "never" (resolve lazily via t=download).
    RESOLVE_MAGNETS: str = os.environ.get("RESOLVE_MAGNETS", "auto").strip().lower()

    # Concurrent magnet API requests on the direct transport.  ext.to throttles
    # bursts, so this stays modest.
    MAGNET_WORKERS: int = _env_int("MAGNET_WORKERS", 8)

    # Concurrent magnet API requests when going through FlareSolverr, which
    # serialises everything through one browser
    MAGNET_WORKERS_FLARESOLVERR: int = _env_int("MAGNET_WORKERS_FLARESOLVERR", 3)

    # Most results per search that get their magnet resolved up front; the
    # rest resolve on demand when the *arr app grabs them
    MAGNET_MAX_RESOLVE: int = _env_int("MAGNET_MAX_RESOLVE", 30)

    # Seconds a resolved magnet link stays cached
    MAGNET_CACHE_TTL: int = _env_int("MAGNET_CACHE_TTL", 3600)

    # Seconds a fetched search page stays cached (0 disables page caching)
    PAGE_CACHE_TTL: int = _env_int("PAGE_CACHE_TTL", 120)

    # Whether to include adult (XXX) content in results
    INCLUDE_ADULT: bool = _env_bool("INCLUDE_ADULT", True)

    # Log level
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

    def __init__(self) -> None:
        raw = os.environ.get("EXT_TO_URLS", "")
        urls = [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
        if not urls:
            urls = list(_DEFAULT_MIRRORS)

        # Only an explicitly configured EXT_TO_URL takes priority; the default
        # value must not push the (usually challenge-free) mirror down the list.
        primary = os.environ.get("EXT_TO_URL", "").strip().rstrip("/")
        if primary:
            if primary in urls:
                urls.remove(primary)
            urls.insert(0, primary)

        self.EXT_TO_URLS = urls


config = Config()
