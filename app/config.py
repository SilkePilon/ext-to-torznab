import os


class Config:
    # FlareSolverr external instance URL
    FLARESOLVERR_URL: str = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191")

    # Base URL for ext.to (can be overridden with a mirror)
    EXT_TO_URL: str = os.environ.get("EXT_TO_URL", "https://ext.to")

    # Optional API key to protect this proxy
    API_KEY: str = os.environ.get("API_KEY", "")

    # Server bind settings
    PORT: int = int(os.environ.get("PORT", "5000"))
    HOST: str = os.environ.get("HOST", "0.0.0.0")

    # Timeout for FlareSolverr requests in milliseconds
    FLARESOLVERR_TIMEOUT: int = int(os.environ.get("FLARESOLVERR_TIMEOUT", "60000"))

    # Tabs-till-verify hint for FlareSolverr Turnstile bypass (0 = disabled)
    FLARESOLVERR_TABS_TILL_VERIFY: int = int(os.environ.get("FLARESOLVERR_TABS_TILL_VERIFY", "0"))

    # Whether to include adult (XXX) content in results
    INCLUDE_ADULT: bool = os.environ.get("INCLUDE_ADULT", "true").lower() == "true"

    # Log level
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")


config = Config()
