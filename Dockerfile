# Wingo prediction analyzer - container image for Amazon Lightsail
# (Lightsail "Create container service" or any VM with Docker).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so source edits do not invalidate the layer
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Chromium for the four browser heads. Installed project-locally
# (/app/.playwright-browsers - where wingo_analyzer.heads looks) so it works
# for any UID; --only-shell matches the headless-only configuration.
RUN PLAYWRIGHT_BROWSERS_PATH=/app/.playwright-browsers \
    playwright install --with-deps chromium --only-shell

COPY wingo_analyzer ./wingo_analyzer
COPY run.py ./

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data /app/logs /app/snapshots \
    && chown -R app:app /app
USER app

# Restart policy ("unless-stopped" / "always") keeps it running without interruptions
CMD ["python", "-m", "wingo_analyzer", "run"]
