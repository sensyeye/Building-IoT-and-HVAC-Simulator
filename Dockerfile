# syntax=docker/dockerfile:1.6

# ---------- Builder ----------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install build deps only if needed (paho-mqtt is pure python, but keep gcc available
# in case future deps need it). Comment out to slim further.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt


# ---------- Runtime ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000 \
    SIMULATOR_CONFIG="" \
    SIMULATOR_DATA_DIR="/app/data"

# Non-root user with a predictable UID/GID so host-side chown is easy:
#   sudo chown -R 1000:1000 data outputs
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid ${APP_GID} app \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --home /app --shell /usr/sbin/nologin app

# Tini (signal handling) + gosu (drop privileges in entrypoint)
RUN apt-get update && apt-get install -y --no-install-recommends tini gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Copy application code (respects .dockerignore)
COPY . .

# Entrypoint fixes ownership of bind-mounted volumes (data/, outputs/),
# then drops privileges to `app` and execs the command.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Pre-create writable runtime directories
RUN mkdir -p /app/data /app/outputs && chown -R app:app /app

# NOTE: must start as root so the entrypoint can chown bind mounts.
# It will `exec gosu app` (well, `su-exec`-style) before running uvicorn.
EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]

# Healthcheck hits the FastAPI liveness probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)" || exit 1

# Default: run the FastAPI web UI + JSON API.
# Override CMD to run CLI subcommands, e.g.:
#   docker run --rm -v $PWD/configs:/app/configs -v $PWD/outputs:/app/outputs \
#     building-iot-simulator \
#     python -c "from simulator.main import main; main(['dry-run-config','--config','configs/realistic_mixed_use.yaml'])"
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
