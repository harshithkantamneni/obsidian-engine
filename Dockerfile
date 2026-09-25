# Base image is pinned by tag only. For reproducible/supply-chain-hardened
# builds, pin by digest (python:3.11-slim@sha256:<digest>) after verifying it.
FROM python:3.11-slim

# System dependencies for Remotion (Chromium), ffmpeg, ffprobe
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    # Chromium/headless Chrome dependencies required by Remotion
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libxshmfence1 \
    libx11-xcb1 \
    libxcb-dri3-0 \
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 for Remotion
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user. APP_UID should match the host user that owns the
# bind-mounted ./data and ./outputs directories (default 1000).
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app
RUN chown app:app /app

# Python dependencies (installed system-wide as root)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Everything under /app is owned by `app`: the pipeline writes outputs/,
# outputs/media/, remotion/public/, remotion/src/video-data.json, and Remotion
# downloads its headless browser into remotion/node_modules at render time.
USER app

# Node dependencies for Remotion
COPY --chown=app:app remotion/package*.json remotion/
RUN cd remotion && npm ci --omit=dev

# Copy application code
COPY --chown=app:app . .

# Create output/data directories and seed intelligence files
RUN mkdir -p data outputs/logs outputs/images outputs/media remotion/public/music \
    && echo '{}' > channel_insights.json \
    && echo '{}' > lessons_learned.json

# The container must listen on all interfaces (the app defaults to 127.0.0.1).
ENV HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1

# Expose dashboard port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Startup: runtime JSON state lives in /app/data (bind-mount ./data there).
# For each state file, /app/<file> becomes a symlink to /app/data/<file>;
# insights/lessons default to '{}', token/secrets are only linked. A regular
# file left at /app/<file> by an atomic write in a previous run is copied into
# data/ first. Nothing here can abort startup — python always starts.
CMD ["sh", "-c", "\
mkdir -p data 2>/dev/null; \
if [ -w data ]; then \
  for f in channel_insights.json lessons_learned.json youtube_token.json client_secrets.json; do \
    if [ -d \"$f\" ] && [ ! -L \"$f\" ]; then echo \"[start] WARNING: /app/$f is a directory (old single-file mount?) - skipping\"; continue; fi; \
    if [ -d \"data/$f\" ]; then echo \"[start] WARNING: /app/data/$f is a directory - skipping\"; continue; fi; \
    if [ -f \"$f\" ] && [ ! -L \"$f\" ] && [ -s \"$f\" ] && [ \"$(cat \"$f\")\" != '{}' ]; then \
      if [ ! -s \"data/$f\" ] || [ \"$f\" -nt \"data/$f\" ]; then cp -f \"$f\" \"data/$f\"; fi; \
    fi; \
    case \"$f\" in channel_insights.json|lessons_learned.json) [ -s \"data/$f\" ] || echo '{}' > \"data/$f\";; esac; \
    rm -f \"$f\"; ln -s \"data/$f\" \"$f\"; \
  done; \
else \
  echo \"[start] WARNING: /app/data is not writable by uid $(id -u) - runtime state will not persist. On the host run: mkdir -p data outputs && chown -R $(id -u) data outputs\"; \
fi; \
for d in outputs remotion/public/music; do [ -w \"$d\" ] || echo \"[start] WARNING: /app/$d is not writable by uid $(id -u)\"; done; \
exec python3 scheduler.py --daemon"]
