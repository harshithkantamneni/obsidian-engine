<div align="center">

# Obsidian Engine

### One topic in. Finished YouTube video out.

**The open-source AI pipeline that researches, writes, narrates, illustrates, and uploads complete YouTube videos — autonomously.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-1%2C400%2B_passing-brightgreen.svg)](#running-tests)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

<!-- TODO: Replace with actual demo GIF — record: run_pipeline.py "topic" → rendered video → YouTube upload -->
<!-- ![Demo](docs/demo.gif) -->

```bash
python run_pipeline.py "The Secret History of the Internet"
# ☕ Come back in 15 minutes. Your video is on YouTube.
```

**See the output live:** every video on [@ObsidianArchiveUnearthed](https://www.youtube.com/@ObsidianArchiveUnearthed) was produced end-to-end by this pipeline.

[Quick Start](#quick-start) · [How It Works](#how-it-works) · [Content Profiles](#content-profiles) · [Configuration](#configuration) · [Contributing](CONTRIBUTING.md)

</div>

---

## Why This Exists

Creating a single YouTube video takes **20-40 hours** of research, writing, recording, editing, and optimization. Obsidian Engine does all of it in **~15 minutes for ~$2**.

It's not a template filler. Each video gets unique research, a narrative arc, fact-checked scripts, AI narration with pacing control, generated + stock visuals, background music, and SEO-optimized metadata. The pipeline has quality gates at every stage — if something isn't good enough, it rewrites it automatically.

## How It Works

```
"The Fall of the Roman Empire"
         │
         ▼
┌─ RESEARCH ──────────────────────────────────────────┐
│  1. Deep Research      — AI investigates the topic   │
│  2. Originality Check  — finds an uncovered angle    │
│  3. Narrative Design   — architects story structure   │
│  4. Script Writing     — broadcast-quality narration  │
│  4b. Script Doctor     — scores & rewrites until ✓   │
│  5. Fact Verification  — checks every claim          │
└──────────────────────────────────────────────────────┘
         │
         ▼
┌─ PRODUCTION (parallel) ─────────────────────────────┐
│  6. SEO Optimization   — titles, tags, descriptions  │
│  7. Scene Breakdown    — visual storyboard           │
│  7b. Visual Continuity — consistent look across all  │
│  8. AI Narration       — text-to-speech with pacing  │
│  9. Stock Footage      — relevant B-roll             │
│  10. AI Images         — generated scene visuals     │
│  11. Video Render      — composites everything       │
│  12. Quality Assurance — automated review            │
└──────────────────────────────────────────────────────┘
         │
         ▼
┌─ PUBLISH ───────────────────────────────────────────┐
│  13. Upload            — to YouTube (or save local)  │
│      + Shorts          — vertical clips auto-cut     │
│      + Thumbnail       — generated cover image       │
└──────────────────────────────────────────────────────┘
         │
         ▼
    🎬 Done. Video is live.
```

Every stage has **quality gates**, **automatic retries**, and **self-healing recovery**. If Stage 4 produces a weak script, the Script Doctor rewrites it. If an image comes back blurry, it regenerates. If the pipeline crashes mid-run, `--resume` picks up exactly where it left off.

## Quick Start

### Option 1: Docker (recommended)

```bash
git clone https://github.com/harshithkantamneni/obsidian-engine.git
cd obsidian-engine
cp .env.example .env     # Add your API keys
docker compose up --build
```

Open `http://localhost:8080` → use the **Setup Wizard** to configure everything from your browser.

### Option 2: Local

```bash
git clone https://github.com/harshithkantamneni/obsidian-engine.git
cd obsidian-engine

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd remotion && npm install && cd ..

cp .env.example .env     # Add your API keys
python run_pipeline.py "The History of the Internet"
```

### Option 3: Resume / restart from any stage

```bash
python run_pipeline.py "The Roman Empire" --resume        # Pick up where you left off
python run_pipeline.py "The Roman Empire" --from-stage 8  # Re-run from narration
```

## API Keys

You need keys only for the providers you configure in `obsidian.yaml`. The built-in defaults use these four:

| Service | Provider (type) | Env var | What For | Free Tier | Link |
|---------|-----------------|---------|----------|-----------|------|
| **Anthropic** | `anthropic` (`llm`) | `ANTHROPIC_API_KEY` | Script writing, research, all AI reasoning | $5 credit | [console.anthropic.com](https://console.anthropic.com) |
| **ElevenLabs** | `elevenlabs` (`tts`) | `ELEVENLABS_API_KEY` | Voice narration | 10K chars/mo | [elevenlabs.io](https://elevenlabs.io) |
| **fal.ai** | `fal` (`images`) | `FAL_KEY` | AI image generation | $10 credit | [fal.ai](https://fal.ai) |
| **Pexels** | `pexels` (`footage`) | `PEXELS_API_KEY` | Stock footage | Free (200 req/hr) | [pexels.com/api](https://www.pexels.com/api/new/) |

The other defaults need no key: `upload: local` saves to `outputs/final/`, and `music: auto` / `sfx: auto` in the shipped `obsidian.yaml` use Epidemic Sound only when `EPIDEMIC_SOUND_API_KEY` is set. If you switch a provider, set its key instead: `openai` (LLM or TTS) reads `OPENAI_API_KEY`, and Ollama through the `openai` provider's `base_url` needs none. See [docs/PROVIDERS.md](docs/PROVIDERS.md). Two features call Claude directly whatever LLM you use, and report an error without `ANTHROPIC_API_KEY`: the scheduler's weekly competitor thumbnail analysis and the vision passes of the manual content auditor (`agents/13_content_auditor.py`).

<details>
<summary><b>Optional services</b> (analytics, auto-upload, notifications)</summary>

| Service | What For | Link |
|---------|----------|------|
| YouTube API | Auto-upload to YouTube (set `providers.upload.name: youtube`) | [Google Cloud Console](https://console.cloud.google.com) |
| Supabase | Analytics database | [supabase.com](https://supabase.com) |
| Epidemic Sound | Premium music + SFX | [epidemicsound.com](https://www.epidemicsound.com) |
| Telegram | Pipeline notifications | [@BotFather](https://t.me/BotFather) |

</details>

## Cost Per Video

| Component | Cost |
|-----------|------|
| AI (Claude — scripts, research, scenes) | $0.50 – $1.50 |
| Voice (ElevenLabs TTS) | $0.30 – $0.80 |
| Images (fal.ai generation) | $0.20 – $0.50 |
| Footage (Pexels) | Free |
| **Total per video** | **~$1 – $3** |

A 15-minute documentary for the price of a coffee.

## Content Profiles

Change one line to completely transform the output style:

```yaml
# obsidian.yaml
profile: documentary     # Netflix/HBO style
```

| Profile | Style | Think... |
|---------|-------|----------|
| `documentary` | Cinematic, authoritative, dark | Lemmino, Netflix docs |
| `explainer` | Clear, curious, visual | Kurzgesagt, Wendover |
| `true_crime` | Investigative, suspenseful | JCS, That Chapter |
| `video_essay` | Analytical, personal | Nerdwriter, Philosophy Tube |

**Make your own:** copy `profiles/_template.yaml` → customize tone, pacing, visual style → set `profile: your_name`.

## Pluggable Providers

Every external service (LLM, voice, images, footage, upload, music, SFX) goes through a provider, so you can swap any of them without touching code:

```yaml
# obsidian.yaml
providers:
  llm:     { name: openai }        # GPT instead of Claude
  tts:     { name: elevenlabs }    # or: openai, epidemic_sound
  images:  { name: fal }           # AI image generation
  footage: { name: pexels }        # Stock B-roll
  upload:  { name: local }         # Save to outputs/final/ (or: youtube to publish)
  music:   { name: auto }          # Epidemic Sound → local fallback
  sfx:     { name: auto }
```

Built-in: `anthropic`, `openai`, `elevenlabs`, `epidemic_sound`, `fal`, `pexels`, `local`, `youtube`

**Bring your own:** set `name` to the dotted path of your own class (e.g. `my_providers.tts.MyTTS`). Anything under `options:` is passed to its constructor. Run `python -m providers` to check that everything loads. [docs/PROVIDERS.md](docs/PROVIDERS.md) documents the interface for each type, and [examples/providers/](examples/providers/) has working templates.

## Dashboard

Real-time monitoring at `http://localhost:8080`:

| Tab | What It Shows |
|-----|--------------|
| **Home** | Live pipeline status, logs, run history |
| **Queue** | Topic queue with scheduling |
| **Intel** | Channel analytics + performance insights |
| **Health** | Error tracking, agent stats, traces |
| **Tuning** | Parameter optimization recommendations |
| **Setup** | Guided wizard for first-time configuration |

Keyboard shortcuts: `1`–`6` switch tabs, `T` triggers a run, `L` toggles logs.

## Configuration

Everything lives in one file — `obsidian.yaml`:

```yaml
profile: documentary

voice:
  narrator_id: "JBFqnCBsd6RMkjVDRZzb"  # ElevenLabs voice
  speed_body: 0.76                       # Narration pace

models:
  premium: "claude-opus-4-6"    # Creative tasks (script, narrative)
  full: "claude-sonnet-4-6"     # Analysis (research, SEO)
  light: "claude-haiku-4-5-20251001"  # Fast tasks (compliance, scoring)

cost:
  budget_max_usd: 5.00          # Hard cap per video (0 = unlimited)

video:
  fps: 30
  long_width: 1920
  long_height: 1080
```

See [`obsidian.yaml`](obsidian.yaml) for all options with inline documentation.

## Architecture

```
obsidian-engine/
├── run_pipeline.py        # Entry point — one command runs everything
├── obsidian.yaml          # Single config file
├── profiles/              # Content style definitions
├── providers/             # Pluggable backends (LLM, TTS, images, etc.)
├── agents/                # 15 specialized AI agents across the 13 stages (incl. 04b Script Doctor, 07b Visual Continuity)
├── core/                  # Schemas, logging, cost tracking, config
├── pipeline/              # Media processing (audio, images, video)
├── clients/               # API clients
├── server/                # Webhook server + notifications
├── dashboard/             # Monitoring UI (Preact + Tailwind)
├── remotion/              # Video renderer (React + Remotion)
├── intel/                 # Channel analytics + competitive intelligence
└── tests/                 # 1,400+ tests
```

## Features

- [x] **13-stage autonomous pipeline** — topic to YouTube in one command
- [x] **4 content profiles** — documentary, explainer, true crime, video essay (+ custom)
- [x] **Pluggable providers** — swap LLM, TTS, images, footage, upload in config
- [x] **Quality gates at every stage** — auto-rewrite until standards are met
- [x] **Script Doctor** — scores scripts on 8 dimensions, rewrites weak areas
- [x] **Fact verification** — checks claims before publishing
- [x] **Crash-safe resume** — `--resume` picks up from last checkpoint
- [x] **YouTube Shorts** — auto-generates vertical clips alongside long-form
- [x] **Series detection** — identifies multi-part topics automatically
- [x] **Real-time dashboard** — monitor pipeline via browser (SSE)
- [x] **Cost tracking** — per-video budget caps with real-time token counting
- [x] **Notifications** — Telegram + Discord alerts on completion/failure
- [x] **Docker + Docker Compose** — one-command deployment
- [x] **Setup Wizard** — browser-based configuration for non-technical users
- [x] **1,400+ tests** — 26%+ coverage, CI on every push
- [x] **Local LLMs via Ollama**: the built-in `openai` LLM provider with `base_url` and `default_model` ([setup](docs/PROVIDERS.md#llm-llmprovider)); a native provider is tracked in [#6](https://github.com/harshithkantamneni/obsidian-engine/issues/6)
- [ ] Knowledge graph for cross-video intelligence ([#20](https://github.com/harshithkantamneni/obsidian-engine/issues/20))
- [ ] A/B testing for titles & thumbnails ([#25](https://github.com/harshithkantamneni/obsidian-engine/issues/25))
- [ ] TikTok & Instagram Reels export ([#26](https://github.com/harshithkantamneni/obsidian-engine/issues/26))
- [ ] More LLM providers: Gemini ([#21](https://github.com/harshithkantamneni/obsidian-engine/issues/21))
- [ ] More image providers: DALL-E, Stability AI ([#11](https://github.com/harshithkantamneni/obsidian-engine/issues/11), [#14](https://github.com/harshithkantamneni/obsidian-engine/issues/14))

## Running Tests

```bash
# Full test suite
python -m pytest tests/ -v --tb=short --cov=. --cov-fail-under=26

# Lint
ruff check --select E,F,W --ignore E501,E402 --exclude remotion .

# Frontend
cd remotion && npm test && npm run lint
```

## Contributing

Contributions welcome — especially new providers and content profiles. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and guidelines.

Looking for a place to start? Check issues labeled [`good first issue`](https://github.com/harshithkantamneni/obsidian-engine/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).

## License

[MIT](LICENSE) — use it however you want.

---

<div align="center">

**Built by [Harshith Kantamneni](https://github.com/harshithkantamneni)**

If this saves you time, consider giving it a ⭐

</div>
