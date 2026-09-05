# ── Base image ──────────────────────────────────────────────────────────────
FROM python:3.12-slim

# ── Working directory ────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies (layer-cached) ──────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ─────────────────────────────────────────────────────────
COPY app/ ./app/

# ── Runtime defaults (all overridable via env / docker-compose) ──────────────
# EXT_TO_URL is deliberately *not* set here: a value baked into the image is
# treated as an explicit override and would push ext.to (Cloudflare-challenged,
# needs FlareSolverr) ahead of the challenge-free mirrors in EXT_TO_URLS.
ENV FLARESOLVERR_URL=http://flaresolverr:8191 \
    PORT=5000 \
    HOST=0.0.0.0 \
    FLARESOLVERR_TIMEOUT=60000 \
    INCLUDE_ADULT=true \
    LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2

EXPOSE 5000

# MALLOC_ARENA_MAX: every short-lived magnet worker thread otherwise gets its
# own glibc arena, and the fragmented free space in those arenas is what made
# resident memory creep from 45 MiB to a 256 MiB OOM-kill over a day.  Two
# arenas measured ~120 MiB steady under load versus ~170 MiB unbounded.

# Health check via the stdlib so the image needs no curl (and no apt layer).
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",\"5000\")}/healthz', timeout=5)" || exit 1

CMD ["sh", "-c", \
    "python -m uvicorn app.main:app --host ${HOST} --port ${PORT} --workers 1 --no-server-header"]
