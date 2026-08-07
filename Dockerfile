# syntax=docker/dockerfile:1

# Serves the team website and the accessibility audit API
# (entry_points/api_server.py).  Built for a long-running container, which is
# what makes the NDJSON progress stream viable — unlike a serverless function,
# there is no execution-time cap to work around.

# ── Build stage: resolve and install into /app/.venv ──────────────────────────
FROM python:3.12-slim AS builder

# uv installs from uv.lock, so the image gets exactly the versions committed to
# the repo.  Pinned deliberately rather than tracking latest mid-semester.
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1

WORKDIR /app

# Dependencies first, without the project itself, so editing source does not
# invalidate this layer.  --frozen fails the build if uv.lock is out of date
# with pyproject.toml, which keeps the two from silently diverging.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Application code.  See .dockerignore — .env and venv/ are deliberately
# excluded so no API key is ever baked into an image layer.
COPY . .
RUN uv sync --frozen --no-dev

# ── Runtime stage: no uv, no build tooling ───────────────────────────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Single chowned COPY: the virtualenv lands in exactly one layer.  Copying it
# after creating it, rather than chown -R'ing it in place, keeps the image from
# carrying two copies.  The path must match the builder's so the venv's
# absolute shebangs stay valid.
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app /app

# Put the synced virtualenv first on PATH so `python` is the project's.
ENV PATH="/app/.venv/bin:$PATH"

# api_server.py reads both of these from the environment (see its main()).
# Coolify injects its own PORT; HOST must be 0.0.0.0 to accept traffic from
# outside the container.
ENV HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

# Run unprivileged.  /tmp must stay writable: every audit runs inside a
# tempfile.mkdtemp() directory that is removed when the request completes.
USER appuser

# GET / serves index.html; urlopen raises on any non-2xx, failing the check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8000') + '/', timeout=4)"]

CMD ["python", "entry_points/api_server.py"]
