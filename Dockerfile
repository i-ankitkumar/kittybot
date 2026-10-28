FROM python:3.12-slim

# Playwright is needed for the Blinkit and BigBasket session imports.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    KITTYBOT_BROWSER_HOME=/home/kittybot/.cache/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY kittybot ./kittybot

RUN pip install --no-cache-dir ".[dev]" playwright \
    && python -m playwright install --with-deps chromium

# The bot and the dashboard both run in this container.
RUN useradd --create-home --uid 1000 kittybot \
    && mkdir -p /home/kittybot/.config/kittybot \
    && chown -R kittybot:kittybot /home/kittybot /app
USER kittybot

VOLUME ["/home/kittybot/.config/kittybot"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8080/ || exit 1

CMD ["kittybot", "run"]
