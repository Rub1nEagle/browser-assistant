# Playwright's official Python image ships Chromium + all system deps for it.
# Pin the same minor as the Python `playwright` package in pyproject.toml so
# the bundled browser version matches what the SDK expects.
FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    BROWSER_PROFILE_DIR=/data/profile \
    PYTHONUNBUFFERED=1

# Xvfb provides the virtual display, x11vnc exposes it as VNC, websockify+noVNC
# wraps it as HTTP so the user can open the browser in any tab. supervisord
# keeps all three running and restarts crashes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        novnc \
        websockify \
        supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements.txt pins the direct deps so the image is reproducible.
# Install pinned deps in a separate layer (cached unless the file changes),
# then install the package itself with --no-deps so pip doesn't try to
# pull in fresh transitives.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY src/ ./src/
COPY prompts/ ./prompts/
RUN pip install --no-cache-dir --no-deps -e .

# Browsers ship pre-installed under /ms-playwright in this base image —
# do NOT run `playwright install` here, it would re-download them.

COPY docker/ /opt/docker/
RUN chmod +x /opt/docker/entrypoint.sh

RUN mkdir -p /data/profile

EXPOSE 6080

ENTRYPOINT ["/opt/docker/entrypoint.sh"]
