# The Obsidian Archive — Railway Deployment Checklist

## Overview
The container runs `scheduler.py --daemon` continuously, producing videos on
schedule and uploading them to YouTube. The scheduler also starts the Flask
dashboard/control API (`server/webhook_server.py`) on port **8080** (`PORT`),
which serves the dashboard at `/`, the health check at `/health`, and the
key-protected control endpoints (`/trigger`, `/kill`, `/api/setup/*`, ...).

---

## Security (read before exposing the port)

- **Set `TRIGGER_KEY`** — every control endpoint returns `503` until it is set.
  Generate one with `python -c "import secrets;print(secrets.token_urlsafe(32))"`.
  Callers send it in the `X-Trigger-Key` header (`?key=` is accepted only on
  the `/stream` SSE endpoint).
- **Set `DASHBOARD_PASSWORD`** — without it the dashboard only works from
  `localhost`; remote browsers never receive the trigger key.
- **Set `FLASK_SECRET_KEY`** so login sessions survive restarts, and
  `COOKIE_SECURE=true` when served over HTTPS.
- The server binds to `127.0.0.1` unless `HOST` is set. The Docker image sets
  `HOST=0.0.0.0` because a container must listen on all interfaces — so set
  the keys above before publishing port 8080.
- If you put a reverse proxy **on the same host** in front of the server,
  requests appear to come from `127.0.0.1`; always set `DASHBOARD_PASSWORD`
  in that setup.
- On a fresh local (non-Docker) install with no `TRIGGER_KEY`, the setup
  wizard is reachable from `127.0.0.1` only; saving it generates a
  `TRIGGER_KEY` and writes it to `.env`.

---

## Step 1 — One-time local setup (before deploying)

### 1a. Generate YouTube OAuth token locally
The YouTube upload API requires an interactive OAuth flow that **cannot run inside Railway**.
You must generate `youtube_token.json` on your local machine first:

```bash
cd obsidian-engine
python3 agents/11_youtube_uploader.py
# Browser will open — log in with your YouTube channel account
# Token is saved to youtube_token.json
```

The token now includes the `yt-analytics.readonly` scope (required by Agent 12).
**Keep this file safe — it grants upload access to your channel.**

### 1b. Upload the token to Railway as a secret file
In Railway dashboard → your service → **Variables** → add:
```
YOUTUBE_TOKEN_JSON = <paste the full contents of youtube_token.json here>
```
`scheduler.py` writes this back to `youtube_token.json` at startup
(and `agents/11_youtube_uploader.py` also restores it from the env var).

---

## Step 2 — Set all environment variables in Railway dashboard

Go to: Railway dashboard → your project → your service → **Variables**

### Required — pipeline will fail without these

| Variable             | Description                                              |
|----------------------|----------------------------------------------------------|
| `ANTHROPIC_API_KEY`  | Claude API key — used by every agent                     |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS API key — Stage 8 audio production        |
| `FAL_KEY`            | fal.ai key — Stage 10 AI image generation                |
| `FAL_API_KEY`        | **Must match `FAL_KEY`** — `run_pipeline.py` reads this name specifically (`os.getenv("FAL_API_KEY")`) |
| `PEXELS_API_KEY`     | Pexels video search — Stage 9 footage hunting            |
| `SUPABASE_URL`       | Your Supabase project URL (`https://xxx.supabase.co`)    |
| `SUPABASE_KEY`       | Supabase `service_role` or `anon` key                    |

> ⚠️ **Note on `FAL_KEY` vs `FAL_API_KEY`:** Set **both** to the same value.
> The fal-client SDK reads `FAL_KEY`; `run_pipeline.py` reads `FAL_API_KEY`.

### Required — for YouTube upload & analytics

| Variable                  | Description                                       |
|---------------------------|---------------------------------------------------|
| `YOUTUBE_TOKEN_JSON`      | Full JSON contents of `youtube_token.json`         |

### Required — dashboard / API security

| Variable             | Description                                              |
|----------------------|----------------------------------------------------------|
| `TRIGGER_KEY`        | Shared secret for the control API (see **Security**)     |
| `DASHBOARD_PASSWORD` | Dashboard login password                                 |
| `FLASK_SECRET_KEY`   | Session signing key (keeps logins valid across restarts) |

### Optional

| Variable               | Description                                          |
|------------------------|------------------------------------------------------|
| `COOKIE_SECURE`        | `true` when the dashboard is served over HTTPS (Railway domains are) |
| `HOST`                 | Bind address — the Dockerfile already sets `0.0.0.0` |
| `PYTHONUNBUFFERED`     | Set to `1` (already in Dockerfile, but safe to repeat) |

---

## Step 3 — YouTube token restore (already built in)

No code change is needed: `scheduler.py` already restores `youtube_token.json`
from `YOUTUBE_TOKEN_JSON` at startup.

---

## Step 4 — Mount a persistent volume for outputs

Railway containers are **ephemeral** — the filesystem is wiped on each deploy.
All rendered videos, audio files, and state JSON live in `outputs/`.

In Railway dashboard → your service → **Volumes**:
- Mount path: `/app/outputs`
- Size: start with **20 GB** (each video ~500MB–2GB rendered)

> Without this volume, every restart loses all rendered content.

> The image runs as the unprivileged `app` user (uid 1000). Railway mounts
> volumes as root, so if logs show `Permission denied: '/app/outputs/...'`,
> add the variable `RAILWAY_RUN_UID=0` (Railway's documented fix for
> non-root images with volumes).

---

## Step 5 — Deploy

```bash
# Push to the Git repo connected to Railway
git add .
git commit -m "Add Railway deployment files"
git push origin main
```

Railway detects the `Dockerfile` via `railway.toml` and builds automatically.
Build time: **~8–12 minutes** (Node + Python deps + Remotion Chromium download).

---

## Step 6 — Verify deployment

In Railway dashboard → **Logs**, you should see:
```
============================================================
  DAEMON MODE — 2x/week
============================================================
  Scheduled: Daily at 06:00 (analytics)
  Scheduled: Tuesday at 09:00
  Scheduled: Friday at 09:00

[Scheduler] Running... (Ctrl+C to stop)
```

If you see import errors, check that all env vars in Step 2 are set.

---

## Schedule summary

| Time (UTC)     | Job                                        |
|----------------|--------------------------------------------|
| Every Monday 08:00  | Topic discovery (Agent 00)            |
| Every Tuesday 09:00 | Full video pipeline                   |
| Every Friday 09:00  | Full video pipeline                   |
| Every day 06:00     | Analytics feedback loop (Agent 12)    |
| Every 5th video     | Experiment video (20% DNA budget)     |

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `SUPABASE_URL and SUPABASE_KEY must be set` | Missing env vars in Railway |
| `No rendered video found` | Volume not mounted, outputs/ is empty |
| `YouTube token deleted — re-authentication required` | `youtube_token.json` missing — restore from `YOUTUBE_TOKEN_JSON` env var |
| `FATAL: Fact verification requires full rewrite` | Twist reveal unverifiable — topic needs to be re-queued with a different angle |
| `Pipeline halted: script too short` | Claude returned < 1000 words — retry the topic |
| Remotion render crash | Chrome deps missing — check Dockerfile build logs |

---

## Docker Compose (self-hosted)

```bash
cp .env.example .env            # fill in API keys, TRIGGER_KEY, DASHBOARD_PASSWORD
mkdir -p data outputs remotion/public/music
docker compose up -d --build
```

- Runtime state (`channel_insights.json`, `lessons_learned.json`) is stored in
  `./data/`. Put `client_secrets.json` and `youtube_token.json` in `./data/`
  too (or set `YOUTUBE_TOKEN_JSON` in `.env`).
- The container runs as an unprivileged user (uid 1000). On Linux, if your
  uid differs, build with `APP_UID=$(id -u) APP_GID=$(id -g) docker compose up -d --build`,
  or `chown` the `data/` and `outputs/` directories to uid 1000.
- Browse to `http://<host>:8080` and log in with `DASHBOARD_PASSWORD`.
