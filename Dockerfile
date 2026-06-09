# ── Base image ──────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── OS-level dependencies ────────────────────────────────────────────────────
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies (layer-cached) ──────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ─────────────────────────────────────────────────────────
COPY app/ ./app/

# ── Runtime defaults (all overridable via env / docker-compose) ──────────────
ENV FLARESOLVERR_URL=http://flaresolverr:8191 \
    EXT_TO_URL=https://ext.to \
    PORT=5000 \
    HOST=0.0.0.0 \
    FLARESOLVERR_TIMEOUT=60000 \
    INCLUDE_ADULT=true \
    LOG_LEVEL=INFO

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -sf http://localhost:${PORT}/healthz || exit 1

CMD ["sh", "-c", \
    "python -m uvicorn app.main:app --host ${HOST} --port ${PORT} --workers 1"]
