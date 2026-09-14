FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/scout/.venv/bin:$PATH

WORKDIR /opt/scout

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY docs/nationwide-LICENSE.txt docs/nationwide-LICENSE.txt
COPY src ./src

# Keep the remote login viewer in the image. It runs only on the temporary
# loopback port in the Gluetun namespace and never needs a host Chrome install.
RUN apt-get update \
    && apt-get install --no-install-recommends -y xvfb xauth x11vnc novnc \
    && rm -rf /var/lib/apt/lists/* \
    && uv sync --frozen --no-dev \
    && DEBIAN_FRONTEND=noninteractive uv run --no-sync playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

RUN groupadd --gid 1000 scout \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/scout --shell /usr/sbin/nologin scout \
    && mkdir -p /data \
    && chown 1000:1000 /data

USER 1000:1000
WORKDIR /opt/scout

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8765/', timeout=5)"]

CMD ["scout", "serve", "--no-open"]
