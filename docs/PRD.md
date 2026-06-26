# Product Requirements Document
## The Obsidian Archive — Autonomous Documentary Video Pipeline

| Field | Value |
|---|---|
| **Product** | The Obsidian Archive |
| **Version** | 1.0 |
| **Author** | Harshith Kantamneni |
| **Date** | March 2026 |
| **Status** | Production |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Product Vision](#3-product-vision)
4. [Target Users](#4-target-users)
5. [System Architecture](#5-system-architecture)
6. [Pipeline Stages](#6-pipeline-stages)
7. [Intelligence Systems](#7-intelligence-systems)
8. [Infrastructure & Services](#8-infrastructure--services)
9. [Quality Assurance](#9-quality-assurance)
10. [Monitoring & Observability](#10-monitoring--observability)
11. [Cost Model](#11-cost-model)
12. [Configuration & Tuning](#12-configuration--tuning)
13. [Deployment](#13-deployment)
14. [Non-Functional Requirements](#14-non-functional-requirements)
15. [Risks & Mitigations](#15-risks--mitigations)
16. [Appendices](#16-appendices)

---

## 1. Executive Summary

The Obsidian Archive is a fully autonomous video production pipeline that transforms a single text topic into a publish-ready YouTube documentary — complete with researched script, AI-generated visuals, professional narration, background music, captions, SEO metadata, and YouTube upload — without any human intervention.

The system orchestrates 15 AI agents across 13 pipeline stages, coordinating 8 external services (Anthropic Claude, ElevenLabs, fal.ai, YouTube, Supabase, Telegram, Discord, Google Trends) through a thread-safe parallel execution engine. It includes a self-learning parameter optimizer, real-time monitoring dashboard, automated scheduling, and a closed-loop analytics feedback system that improves output quality with every published video.

**Key metrics:**
- **Time per video:** 25-40 minutes (fully automated)
- **Cost per video:** $3.50-$4.50 USD
- **Pipeline stages:** 13 main + parallel shorts sub-pipeline
- **Test coverage:** 57% (1,310 tests)
- **Uptime target:** 99.5% (Railway deployment)

---

## 2. Problem Statement

### 2.1 Market Context

History and documentary content consistently ranks among the highest-retention categories on YouTube, with average watch times 2-3x platform median. However, producing documentary-quality content requires:

- **Research depth:** 8-15 hours per topic for primary source verification
- **Scriptwriting craft:** Narrative structure, emotional pacing, hook design
- **Visual production:** Historical imagery, atmospheric footage, AI-generated scenes
- **Audio production:** Professional narration, music selection, audio mastering
- **SEO optimization:** Title testing, description writing, tag research, thumbnail design
- **Post-production:** Video editing, caption sync, quality assurance
- **Analytics feedback:** Performance tracking, audience sentiment, content iteration

A single 10-minute documentary video requires 40-80 hours of skilled labor across 5+ disciplines.

### 2.2 Core Problem

No existing tool automates the end-to-end documentary production workflow. Current solutions address individual steps (AI scriptwriting, TTS, image generation) but leave creators responsible for orchestrating, quality-gating, and iterating across the entire pipeline.

### 2.3 Solution

The Obsidian Archive eliminates the human bottleneck by chaining specialized AI agents into a deterministic, quality-gated pipeline. Each agent is an expert in one domain (research, storytelling, audio, visual, SEO). The pipeline enforces quality at every stage through structured output validation, multi-tier QA checks, and automated error recovery — producing content that meets a measurable quality bar before publishing.

---

## 3. Product Vision

### 3.1 Mission

Automate documentary video production to the point where a single creator can operate a professional YouTube channel producing 1-2 videos per week, with AI handling 100% of production and the creator focused on strategy, community, and creative direction.

### 3.2 Design Principles

| Principle | Implementation |
|---|---|
| **Quality over speed** | 3-tier quality gates abort or warn before publishing substandard content |
| **Closed-loop learning** | Every published video feeds performance data back into future productions |
| **Graceful degradation** | Every external service call has fallback, retry, and recovery logic |
| **Observable by default** | Every pipeline run produces structured logs, cost reports, and quality metrics |
| **Human-in-the-loop optional** | System runs fully autonomously but surfaces decisions via Telegram for override |

### 3.3 Success Metrics

| Metric | Target | Measurement |
|---|---|---|
| Average viewer retention | > 40% at 50% mark | YouTube Analytics API |
| CTR (click-through rate) | > 5% | YouTube Analytics API |
| Cost per video | < $5.00 USD | Internal cost tracker |
| Pipeline success rate | > 95% | State file analysis |
| Time to publish | < 45 minutes | Pipeline elapsed_seconds |
| Script quality score | > 7.0/10 | Script Doctor 9-dimension eval |

---

## 4. Target Users

### 4.1 Primary: Solo Creator-Operators

Individuals running YouTube channels who want to scale content production beyond what manual effort allows. Technical enough to deploy on Railway and configure API keys, but not necessarily developers.

### 4.2 Secondary: Content Agencies

Small teams managing multiple YouTube channels across history, true crime, science, or documentary niches. Benefit from the scheduling, analytics, and multi-topic queue management.

### 4.3 User Journey

```
Creator configures API keys and channel DNA
        |
        v
Scheduler discovers high-potential topics (or creator submits manually)
        |
        v
Pipeline produces video autonomously (25-40 min)
        |
        v
Creator receives Telegram notification with YouTube link
        |
        v
Analytics agent monitors performance for 48h
        |
        v
Insights feed into next video's production parameters
```

---

## 5. System Architecture

### 5.1 High-Level Architecture

```
                    ┌─────────────────────────────────┐
                    |        SCHEDULER (daemon)        |
                    |  Topic Discovery | Publish Timer |
                    └──────────┬──────────────────────┘
                               |
                    ┌──────────v──────────────────────┐
                    |       PIPELINE ORCHESTRATOR      |
                    |  run_pipeline.py (thin wrapper)  |
                    |  phase_setup -> phase_script ->  |
                    |  phase_prod -> phase_post        |
                    └──────────┬──────────────────────┘
                               |
          ┌────────────────────┼────────────────────┐
          |                    |                    |
   ┌──────v──────┐   ┌────────v────────┐   ┌──────v──────┐
   | SCRIPT PHASE|   | PRODUCTION PHASE|   |  POST PHASE |
   | Stages 1-5  |   | Stages 6-12    |   | Stage 13+   |
   | (sequential)|   | (parallel DAGs)|   | (sequential)|
   └──────┬──────┘   └────────┬────────┘   └──────┬──────┘
          |                    |                    |
   ┌──────v──────────────────v────────────────────v──────┐
   |              AGENT EXECUTION LAYER                   |
   |  core/agent_wrapper.py -> clients/claude_client.py   |
   |  Structured output | SLA enforcement | Recovery      |
   └──────────────────────┬──────────────────────────────┘
                          |
          ┌───────────────┼───────────────┐
          |               |               |
   ┌──────v──────┐ ┌─────v─────┐ ┌───────v───────┐
   |  Anthropic  | | ElevenLabs| |    fal.ai     |
   |  Claude API | | TTS API   | | Image Gen API |
   └─────────────┘ └───────────┘ └───────────────┘
```

### 5.2 Module Structure

```
obsidian-archive/
|-- run_pipeline.py          # Entry point (141 lines, thin orchestrator)
|-- scheduler.py             # Daemon scheduler (topic discovery + publish timing)
|
|-- agents/                  # 20 AI agent modules (10,043 lines)
|   |-- 00_topic_discovery   # Viral topic scoring by channel maturity
|   |-- 01_research          # Deep research synthesis
|   |-- 02_originality       # Unique angle discovery
|   |-- 03_narrative         # Story blueprint design (6 structures)
|   |-- 04_script_writer     # Full narration script (Opus-level)
|   |-- 04b_script_doctor    # 9-dimension quality scoring
|   |-- 05_fact_verification # Multi-source claim verification
|   |-- 06_seo              # YouTube metadata optimization
|   |-- 07_scene_breakdown  # Visual scene decomposition
|   |-- 07b_visual_continuity # Art style + character consistency
|   |-- 08_audio_producer   # ElevenLabs TTS with word timestamps
|   |-- 09_footage_hunter   # Wikimedia + Pexels sourcing
|   |-- 11_youtube_uploader # OAuth2 upload with full metadata
|   |-- 12_analytics        # YouTube performance feedback loop
|   |-- 13_content_auditor  # 12-pass post-production QA
|   |-- thumbnail_agent     # AI thumbnail gen + vision scoring
|   |-- short_script_agent  # 45-60s YouTube Shorts script
|   |-- short_storyboard    # Vertical storyboard for Shorts
|   |-- tts_format_agent    # Script prep for natural TTS
|
|-- pipeline/                # Phase orchestration (4,445 lines)
|   |-- context.py          # PipelineContext dataclass
|   |-- runner.py           # StageRunner (thread-safe execution)
|   |-- phase_setup.py      # Init, agents, credit checks
|   |-- phase_script.py     # Stages 1-5 (sequential)
|   |-- phase_prod.py       # Stages 6-12 (parallel DAGs)
|   |-- phase_post.py       # Stage 13+ (upload, analytics)
|   |-- audio.py            # Chunked TTS + mood-based voice
|   |-- images.py           # Multi-threaded fal.ai generation
|   |-- convert.py          # Remotion data conversion
|   |-- render.py           # ffprobe validation
|   |-- shorts.py           # Parallel shorts pipeline
|   |-- voice.py            # Voice presets + modulation
|   |-- series.py           # Multi-part series detection
|   |-- validators.py       # Quality gate functions
|   |-- state.py            # Atomic JSON state persistence
|   |-- helpers.py          # Shared utilities
|   |-- loader.py           # Dynamic agent loading
|
|-- core/                    # System intelligence (7,816 lines)
|   |-- agent_wrapper.py    # 4-tier model routing + SLA + recovery
|   |-- quality_gates.py    # 32 quality check functions (3 tiers)
|   |-- cost_tracker.py     # Per-run, per-service cost tracking
|   |-- param_optimizer.py  # Neural-network-inspired self-learning
|   |-- param_history.py    # Observation persistence + optimizer state
|   |-- param_overrides.py  # Runtime parameter patching
|   |-- param_registry.py   # 85+ tunable production parameters
|   |-- pipeline_doctor.py  # Automatic error recovery engine
|   |-- observability.py    # Error tracking + agent tracing (JSONL)
|   |-- render_verification.py # Post-render compliance checking
|   |-- compliance_checker.py  # Content safety validation
|   |-- structured_schemas.py  # JSON Schema registry for agents
|   |-- pipeline_config.py  # Global configuration constants
|   |-- log.py              # Structured logging (JSON + console)
|   |-- feedback_loops.py   # Performance insight injection
|   |-- json_compat.py      # Lenient JSON parsing
|   |-- shutdown.py         # Graceful SIGTERM handling
|   |-- paths.py            # Centralized path constants
|
|-- intel/                   # Channel analytics & guidance (6,639 lines)
|   |-- channel_insights.py # Confidence-blended intelligence
|   |-- correlation_engine.py # 7-layer statistical DAG
|   |-- trend_detector.py   # Google Trends + Reddit monitoring
|   |-- comment_analyzer.py # Audience sentiment extraction
|   |-- community_engagement.py # Post-upload engagement
|   |-- era_classifier.py   # Topic -> era mapping
|   |-- dna_loader.py       # Channel identity sections
|   |-- youtube_knowledge_base.py # Bayesian priors by channel size
|
|-- clients/                 # External service wrappers (660 lines)
|   |-- claude_client.py    # 3-model Claude API (Opus/Sonnet/Haiku)
|   |-- supabase_client.py  # Database with retry + backoff
|
|-- server/                  # Monitoring & control (2,522 lines)
|   |-- webhook_server.py   # Flask REST API + SSE + dashboard
|   |-- notify.py           # Dual Telegram + Discord notifications
|   |-- topic_store.py      # Topic queue persistence
|
|-- dashboard/               # Real-time monitoring UI
|   |-- src/                # Preact + Signals + Tailwind
|   |   |-- views/          # Summary, Performance, Content, Audience, Config
|   |   |-- components/     # MetricCard, Chart, ProgressBar, Tuning panels
|   |   |-- state/          # Signals store + API client + SSE
|   |   |-- types.ts        # Centralized type definitions
|
|-- remotion/                # Video rendering engine
|   |-- src/
|   |   |-- ObsidianVideo.tsx  # Main composition (music ducking, captions)
|   |   |-- ShortVideo.tsx     # 9:16 Shorts composition
|   |   |-- Captions.tsx       # Word-level caption timing
|   |   |-- audio-utils.ts     # Ducking + volume envelope logic
|
|-- tests/                   # 1,310 tests across 52 files
|-- scripts/                 # Utility scripts (music setup, diagnostics)
```

### 5.3 Threading Model

The pipeline uses 3 `ThreadPoolExecutor` instances for parallel execution:

| Executor | Workers | Scope | Thread Safety |
|---|---|---|---|
| **Wave 1 DAG** | 3 | SEO + Scenes + Compliance (parallel) | `stage_lock` via `StageRunner.mark()` |
| **Wave 3 DAG** | 3 | Audio + Footage + Thumbnail (parallel) | `stage_lock` via `StageRunner.mark_metadata()` |
| **Shorts Background** | 1 | Entire shorts pipeline (parallel to main) | `stage_lock` via `StageRunner.run_short_stage()` |

All state writes during parallel phases acquire `PipelineContext.stage_lock` before writing. Sequential phases use unlocked saves (no contention possible).

### 5.4 State Management

Pipeline state is persisted as JSON after every stage completion:
- **Atomic writes:** temp file -> `fsync` -> `rename` (prevents corruption on crash)
- **Resume support:** `--resume` flag loads most recent state file and skips completed stages
- **Stage jumping:** `--from-stage N` enables starting from any point in the pipeline
- **Emergency save:** `atexit` handler saves state on crash; `SIGTERM` handler enables graceful shutdown

---

## 6. Pipeline Stages

### 6.1 Script Phase (Sequential, Stages 1-5)

#### Stage 1: Research
- **Agent:** 01_research_agent.py (Claude Sonnet)
- **Input:** Topic string
- **Output:** Comprehensive fact sheet — core facts, key figures, timeline, suppressed details, contradictions, primary sources, archival gems
- **Quality gate:** Minimum 5 core facts, 2 key figures

#### Stage 2: Originality
- **Agent:** 02_originality_agent.py (Claude Sonnet)
- **Input:** Research output + covered angles archive (Supabase)
- **Output:** Dominant YouTube angles, content gaps, chosen unique angle, twist potential, uniqueness score (1-10)
- **Quality gate:** `chosen_angle` must be present

#### Stage 3: Narrative Architecture
- **Agent:** 03_narrative_architect.py (Claude Sonnet)
- **Input:** Research + angle
- **Output:** Complete story blueprint — structure type (6 options), length tier, cold open, hook, 3-act breakdown, POV shifts, reflection beat, emotional arc
- **Structures available:** CLASSIC, MYSTERY, DUAL_TIMELINE, COUNTDOWN, TRIAL, REFRAME
- **Length tiers:** STANDARD (8-10min), DEEP_DIVE (15-18min), EPIC (22-25min)
- **Quality gate:** `hook`, `act1`, `act2`, `act3` must be present

#### Stage 4: Script Writing
- **Agent:** 04_script_writer.py (Claude Opus)
- **Input:** Research + angle + blueprint + channel DNA
- **Output:** Full narration script (1,400-2,200 words) with act breakdown
- **Post-processing:**
  - Script Doctor evaluation (9 dimensions, threshold 7.0/10)
  - Up to 2 automated rewrites if below threshold
  - Series detection for multi-part topics
  - Hook consistency check (opening 50 words vs. blueprint hook)

#### Stage 5: Fact Verification
- **Agent:** 05_fact_verification_agent.py (Claude Sonnet)
- **Input:** Script + research
- **Output:** Per-claim verdicts (VERIFIED_SCHOLARLY, VERIFIED_POPULAR, UNVERIFIED, DISPUTED, HALLUCINATION), correction suggestions, source list
- **Quality gate:** Overall verdict must not be REQUIRES_REWRITE (up to 2 retry cycles)

### 6.2 Production Phase (Parallel DAGs, Stages 6-12)

#### Wave 1 (3 parallel workers)

**Stage 6: SEO Optimization**
- **Agent:** 06_seo_agent.py (Claude Sonnet)
- **Input:** Script + research + channel insights
- **Output:** Title variants, recommended title, description (hook lines + hashtags), tags, chapter markers, thumbnail concepts, end-screen CTA

**Stage 7: Scene Breakdown**
- **Agent:** 07_scene_breakdown_agent.py (Claude Sonnet)
- **Input:** Script + blueprint
- **Output:** 10-20 scenes with narration text, duration, visual type (historical_art, broll_atmospheric, map, text_overlay), mood, characters, retention hooks

**Compliance Check** (parallel to Stages 6-7)
- **Module:** core/compliance_checker.py
- **Output:** Risk level (green/yellow/red), flagged claims, safe_script boolean

#### Wave 2 (Sequential)

**Stage 7b: Visual Continuity**
- **Agent:** 07b_visual_continuity.py (Claude Sonnet)
- **Output:** Visual bible (art style, color palette, character descriptions, recurring motifs), enhanced image prompts per scene

**TTS Formatting**
- **Agent:** tts_format_agent.py (Claude Haiku)
- **Output:** Script reformatted for natural ElevenLabs delivery (em-dashes -> pauses, abbreviations expanded, numbers to words)

#### Wave 3 (3 parallel workers)

**Stage 8: Audio Generation**
- **Module:** pipeline/audio.py + 08_audio_producer.py
- **Service:** ElevenLabs (George voice)
- **Features:**
  - Chunked TTS (max 500 chars for prosody quality)
  - Mood-based voice settings per scene (stability, speed, style)
  - Quoted speech detection -> secondary voice (Adam)
  - Word-level timestamps for caption sync
  - Mutagen for precise MP3 offset measurement

**Stage 9: Footage Sourcing**
- **Agent:** 09_footage_hunter.py
- **Sources:** Wikimedia Commons (primary, archival), Pexels (fallback, atmospheric)
- **Features:** 7-day TTL cache, topic-aware fallback queries, era-specific sources

**Thumbnail Generation** (parallel to Stages 8-9)
- **Agent:** thumbnail_agent.py
- **Pipeline:** Claude concept -> fal.ai render -> Pillow text overlay -> Haiku vision scoring
- **Output:** 3 concepts, best-scored selected

#### Sequential Production

**Stage 10: Image Generation**
- **Module:** pipeline/images.py
- **Service:** fal.ai (Flux Pro + Recraft fallback)
- **Features:**
  - 3-worker ThreadPoolExecutor for parallel scene rendering
  - Adaptive quality thresholds by scene importance (hook scenes get higher bar)
  - Claude Haiku vision scoring (1-10) with retry on low scores
  - Wikimedia fallback on fal.ai failure
  - Character portrait generation for visual consistency

**Stage 11: Remotion Data Conversion**
- **Module:** pipeline/convert.py
- **Process:** Aligns scenes to word timestamps, maps music sections, exports JSON for Remotion

**Stage 12: Video Rendering**
- **Engine:** Remotion (React-based frame-by-frame)
- **Features:**
  - Multi-layer composition: captions + narration + music + SFX + images + transitions
  - Act envelope music volume control
  - Speech/silence ducking (configurable levels)
  - Word-level caption timing with opacity fade
  - Act-boundary-aware transitions (hard cuts for act breaks)
  - 1920x1080 @ 30fps (long-form), 1080x1920 (Shorts)

**QA Tiers** (post-render)
- **Tier 0:** Gate checks (abort on fail) — file exists, size > 50MB, audio stream present
- **Tier 1:** Technical checks (warn) — loudness, resolution, codec, WPM
- **Tier 2:** Content checks (observational) — narrative quality, visual diversity

### 6.3 Post Phase (Sequential, Stage 13+)

**Stage 13: YouTube Upload**
- **Agent:** 11_youtube_uploader.py
- **Features:** OAuth2 with token refresh, full SEO metadata, chapters, thumbnail, playlist assignment, privacy setting

**Post-Upload Operations:**
1. Predictive performance scoring (0-11 points)
2. Render verification (loudness, WPM, visual quality)
3. Community teaser notification (Telegram + Discord)
4. Topic recorded as covered (Supabase)
5. Parameter observation stored for optimizer
6. Analytics agent triggered
7. Comment analysis (sentiment, topic requests)
8. Community engagement recommendations
9. Quality report generation
10. Cost tracking finalization
11. Post-upload file cleanup (intermediate assets)

### 6.4 Shorts Sub-Pipeline (Parallel to Main)

Runs in a background thread alongside the main pipeline:

1. **Short Script:** 45-60 second script (hook -> claim -> tension -> payoff -> CTA)
2. **Short Storyboard:** 3-4 scenes in 9:16 portrait format
3. **Short Audio:** ElevenLabs TTS with Shorts-optimized voice settings
4. **Short Images:** fal.ai generation at 1080x1920
5. **Short Conversion:** Remotion data with vertical layout
6. **Short Render:** 9:16 video with centered captions
7. **Short Upload:** YouTube Shorts with cross-promotion to main video

---

## 7. Intelligence Systems

### 7.1 Channel Intelligence (Closed-Loop)

```
Published Video
      |
      v
YouTube Analytics API (48h delay)
      |
      v
Analytics Agent (12_analytics_agent.py)
      |
      v
channel_insights.json
      |
      v
Confidence Blending (intel/channel_insights.py)
      |
      +--> Injected into agent prompts (research, script, SEO)
      +--> Parameter optimizer training data
      +--> Topic discovery scoring weights
```

**Confidence blending:** With < 15 videos, the system blends its own performance data with Bayesian priors from `youtube_knowledge_base.py`. As video count increases, own data weight increases linearly until full confidence at 30+ videos.

### 7.2 Self-Learning Parameter Optimizer

**Architecture:** Neural-network-inspired gradient estimation with momentum.

**Tunable parameters:** 85+ production parameters across:
- Voice settings (8 moods x 4 params = 32)
- Narrative arc deltas (5 positions x 3 deltas = 15)
- Music ducking levels (6 params)
- Pause durations (4 params)
- Act boundary positions (3 params)
- Short-form specific settings (13 params)

**Optimization loop:**
1. Store parameter snapshot + render verification after upload
2. Attach YouTube metrics 48h post-publish
3. Compute loss: retention (0.35) + views velocity (0.20) + engagement (0.20) + sentiment (0.10) + hook retention (0.15)
4. Estimate gradients from paired observations
5. Apply momentum-weighted updates
6. Exploration queue for untested parameter ranges
7. Cooldown on rollback (10 videos)

**Safety:**
- All changes clamped to PARAM_BOUNDS
- Brand consistency constraints (Shorts params within 0.15 of long-form defaults)
- Kill switch via Supabase kv_store
- Approval workflow for large changes (surfaced via Telegram)

### 7.3 Correlation Engine

7-layer statistical DAG (`intel/correlation_engine.py`):

| Layer | Input | Output |
|---|---|---|
| 1 | Script params -> Script metrics | Which voice/pacing settings produce better scripts |
| 2 | Short params -> Short metrics | Shorts-specific parameter effects |
| 3 | Raw topic metrics | Baseline topic health signals |
| 4 | Audience transformation | How audience metrics evolve over time |
| 5 | Short production -> Parent lift | Does Shorts performance boost main video |
| 6 | Era stratification | Parameter effects split by historical era |
| 7 | Cross-format signal transfer | Which Shorts insights transfer to long-form |

Uses Pearson correlation with Benjamini-Hochberg FDR correction for multiple comparisons.

### 7.4 Trend Detection

Real-time monitoring of 3 sources:
- **Google Trends:** Rising search interest in historical topics
- **Reddit:** Viral history posts (r/history, r/todayilearned, etc.)
- **YouTube:** Search volume trends for documentary keywords

Cross-source scoring triggers emergency pipeline runs for high-scoring trends (> 80/100). Notifications sent via Telegram.

### 7.5 Topic Discovery

**Agent 00** scores potential topics across 14+ signals:
- Subscriber conversion potential
- Engagement rate prediction
- Search demand volume
- Traffic source mix
- Content pattern fit
- Era performance history
- Audience request frequency
- Competitive saturation
- Trending velocity
- Novelty (not covered by channel)

Scoring weights adjust by channel maturity stage (EARLY < 5K subs, GROWING < 50K, MATURE > 50K).

---

## 8. Infrastructure & Services

### 8.1 External Service Dependencies

| Service | Purpose | Cost Model | Fallback |
|---|---|---|---|
| **Anthropic Claude** | All AI reasoning (20 agents) | Per-token ($15/M input Opus, $3/M Sonnet, $0.80/M Haiku) | Model tier demotion |
| **ElevenLabs** | Text-to-speech narration | Per-character ($0.30/1K chars) | N/A (critical path) |
| **fal.ai** | AI image generation | Per-image ($0.05/image) | Recraft model fallback |
| **YouTube Data API v3** | Upload, analytics, comments | Free (quota-based) | N/A (critical path) |
| **YouTube Analytics v2** | Performance metrics | Free (quota-based) | Graceful skip |
| **Supabase** | Database (topics, videos, params) | Free tier sufficient | Local JSON fallback |
| **Telegram Bot API** | Real-time notifications | Free | Discord fallback |
| **Discord Webhook** | Notification redundancy | Free | Silent skip |
| **Google Trends** | Topic trend scoring | Free (rate-limited) | Graceful skip |
| **Wikimedia Commons** | Historical source imagery | Free | Pexels fallback |
| **Pexels** | Atmospheric B-roll | Free (API key) | Generated image fallback |

### 8.2 Technology Stack

**Backend:**
- Python 3.11+ (pipeline, agents, server)
- Flask (webhook server + REST API)
- Threading (ThreadPoolExecutor for parallel stages)

**Frontend:**
- Preact + Signals (dashboard UI)
- Tailwind CSS (styling)
- Vite (build tooling)

**Video Rendering:**
- Remotion 4.0 (React-based frame rendering)
- TypeScript + React 19

**Database:**
- Supabase (PostgreSQL) — topics, videos, analytics, parameters
- JSON files — state, insights, cost logs, observations

**Deployment:**
- Railway (Docker container, auto-deploy from main)
- GitHub Actions CI (lint + test + Docker build)

### 8.3 Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude API access |
| `ELEVENLABS_API_KEY` | Yes | TTS API access |
| `FAL_API_KEY` | Yes | Image generation API |
| `PEXELS_API_KEY` | Yes | Stock footage API |
| `SUPABASE_URL` | Yes | Database URL |
| `SUPABASE_KEY` | Yes | Database auth key |
| `YOUTUBE_TOKEN_JSON` | Yes | OAuth2 token (base64 on Railway) |
| `TELEGRAM_BOT_TOKEN` | No | Notification bot |
| `TELEGRAM_CHAT_ID` | No | Notification target |
| `DISCORD_WEBHOOK_URL` | No | Notification webhook |
| `DASHBOARD_PASSWORD` | No | Dashboard auth |
| `OBSIDIAN_LOG_LEVEL` | No | Logging verbosity (default: INFO) |
| `COST_BUDGET_MAX_USD` | No | Pipeline cost cap |

---

## 9. Quality Assurance

### 9.1 Three-Tier Quality Gate System

```
TIER 0: GATE CHECKS (abort pipeline on failure)
|-- Script length within bounds (STANDARD/DEEP_DIVE/EPIC)
|-- Verification verdict not REQUIRES_REWRITE
|-- Audio file exists and > 500KB
|-- Video file exists and > 50MB
|-- WPM within 15% of 130 target
|-- Script breathability (max 3 consecutive sentences without pause)

TIER 1: QUALITY WARNINGS (logged, non-blocking)
|-- Hook strength evaluation
|-- Emotional pacing analysis
|-- Scene visual diversity
|-- Audio peak levels and compression
|-- SEO title length optimization (7-11 words)
|-- Content policy compliance

TIER 2: SOFT METRICS (observational, feeds optimizer)
|-- Word count, sentence count, readability
|-- Audio dynamic range, noise floor
|-- Image aspect ratios, color variance
|-- Duration variance across scenes
```

### 9.2 Script Quality Enforcement

The Script Doctor (Agent 04b) evaluates scripts across 9 dimensions:

1. **Hook strength** (1-10) — First 30 seconds retention potential
2. **Emotional pacing** (1-10) — Variety and flow of emotional beats
3. **Personality** (1-10) — Distinctive voice and perspective
4. **POV shifts** (1-10) — Perspective changes for engagement
5. **Voice consistency** (1-10) — Tonal coherence throughout
6. **Factual grounding** (1-10) — Claims supported by research
7. **Emotional arc** (1-10) — Build-release pattern
8. **Breathability** (1-10) — Pacing and pause placement
9. **Revelation craft** (1-10) — Information reveal timing

**Threshold:** Average score must exceed 7.0/10. Up to 2 automated rewrites with specific feedback.

### 9.3 Render Verification

Post-render automated checks (`core/render_verification.py`):
- **Loudness:** LUFS measurement via ffmpeg ebur128 (target: -14 LUFS)
- **WPM:** Actual words-per-minute from video duration (target: 130 WPM +/- 15%)
- **Volume profile:** Speech vs. silence segment loudness, ducking detection
- **Visual quality:** Per-scene sharpness (Laplacian variance), black frame detection, Ken Burns motion detection
- **Overall compliance score:** 0.0-1.0 with deviation list

### 9.4 Error Recovery

The Pipeline Doctor (`core/pipeline_doctor.py`) provides automatic recovery:
- **Rate limit detection:** Wait and retry with backoff
- **Timeout recovery:** Retry with shorter prompt or faster model
- **Hallucination detection:** Re-run with stronger grounding instructions
- **Recursion guard:** Maximum 3 recovery attempts per stage
- **Fallback stages:** FALLBACK_SAFE_STAGES define which stages can be skipped on repeated failure

### 9.5 Test Coverage

| Category | Tests | Coverage |
|---|---|---|
| Pipeline runner + context | 29 | 96-100% |
| Phase setup | 37 | 82-99% |
| Phase script | 34 | 95% |
| Phase production | 41 | 100% |
| Pipeline integration | 11 | 98% |
| Thread safety (stress) | 7 | 98% |
| Quality gates | 33 | 100% |
| Compliance checker | 27 | 100% |
| Pipeline doctor | 39 | 100% |
| Notifications | 17 | 100% |
| Audio/Images/Convert | 138 | 99% |
| Other (schemas, config, etc.) | 897 | Varies |
| **Total** | **1,310** | **57%** |

---

## 10. Monitoring & Observability

### 10.1 Real-Time Dashboard

Web-based monitoring UI served from webhook_server.py:

**Views:**
- **Summary:** Current pipeline status, active stage, elapsed time, cost
- **Performance:** Retention curves, CTR trends, engagement metrics
- **Content:** Script quality scores, hook evaluation, SEO metadata
- **Audience:** Subscriber growth, demographics, sentiment trends
- **Config:** Tunable parameters with live adjustment
- **Queue:** Topic queue with scores, scheduling status

**Data flow:** SSE (Server-Sent Events) for real-time updates, REST API for dashboard data.

### 10.2 Structured Logging

All pipeline output goes through `core/log.py`:
- **Console:** Human-readable format (same as original print output)
- **File:** JSON lines at `outputs/pipeline.log` (10MB rotation, 2 backups)
- **Fields:** timestamp, level, logger name, message, optional structured data (stage, elapsed, component)

### 10.3 Error Tracking

Native observability (`core/observability.py`) replaces Sentry:
- JSONL error log with deduplication (fingerprint-based)
- Severity levels: warning, error, critical
- Telegram alerts on error spikes (5+ occurrences of same error)
- Structured context: stage, agent, error type, stack trace

### 10.4 Agent Tracing

Native tracing replaces Langfuse:
- JSONL trace log per agent execution
- Fields: agent name, model, input/output tokens, elapsed time, success/failure
- 5MB rotation with 1 backup

### 10.5 Notifications

Dual-channel notification system (`server/notify.py`):

| Event | Telegram | Discord |
|---|---|---|
| Pipeline start | Topic name | Topic name |
| Pipeline complete | Title + YouTube URL + elapsed time + Shorts URL | Same |
| Error spike | Error count + type + stage | Same |
| Community teaser | Teaser text + title | Same |
| Trend alert | Topic + score + sources | Same |
| Pipeline failed | Topic + stage + error | Same |

---

## 11. Cost Model

### 11.1 Per-Video Cost Breakdown

| Service | Typical Cost | Notes |
|---|---|---|
| Claude Opus (script) | $1.50 | ~15K input + 3K output tokens |
| Claude Sonnet (6 agents) | $0.40 | ~30K input + 10K output total |
| Claude Haiku (scoring) | $0.05 | Image scoring + formatting |
| ElevenLabs | $0.35 | ~3,000 characters |
| fal.ai | $1.20 | ~24 images (10 scenes x 2 attempts + portraits + thumbnail) |
| YouTube API | Free | Quota-based |
| Supabase | Free | Within free tier |
| **Total** | **$3.50-$4.50** | |

### 11.2 Cost Tracking

`core/cost_tracker.py` provides:
- Per-run cost accumulation (thread-safe)
- Per-service breakdown (Claude models, ElevenLabs, fal.ai)
- Per-stage attribution
- Budget enforcement (abort if COST_BUDGET_MAX_USD exceeded)
- Monthly summary reports
- ROI analysis (cost-per-view, cost-per-subscriber)

### 11.3 Cost Optimization

- **Model tier routing:** Haiku for scoring/formatting, Sonnet for analysis, Opus only for creative writing
- **Cache-aware pricing:** Claude prompt caching reduces repeat costs by 90%
- **Conditional execution:** Shorts pipeline only runs if main video succeeds
- **Cleanup after upload:** Intermediate files deleted to save storage

---

## 12. Configuration & Tuning

### 12.1 Pipeline Configuration

`core/pipeline_config.py` exposes 85+ tunable parameters:

**Voice Settings:**
- Narrator voice ID, stability, similarity_boost, style, speed
- Per-mood presets (dark, tense, dramatic, cold, reverent, wonder, warmth, absurdity)
- Narrative arc deltas (hook speed boost, climax stability, resolution calm)

**Video Rendering:**
- FPS, CRF (quality), resolution
- Music ducking levels (speech_volume, silence_volume, ramp_seconds)
- Act envelope volume multipliers
- Scene max duration, transition types

**Quality Thresholds:**
- MIN_RESEARCH_FACTS, MIN_TAGS, MIN_SCENES
- MIN_AUDIO_DURATION, MAX_AUDIO_DURATION
- MIN_VIDEO_SIZE_MB
- Script word count bounds by length tier

**Scheduling:**
- VIDEOS_PER_WEEK, PUBLISH_DAYS, DISCOVER_TIME
- Topic scoring weights by channel maturity

### 12.2 Runtime Overrides

Parameters can be adjusted at runtime via:
1. **Supabase `tuning_overrides` table** — Dashboard tuning panel writes here
2. **Parameter optimizer** — Automated adjustments based on performance data
3. **Dashboard API** — Direct parameter modification via REST

All overrides are clamped to PARAM_BOUNDS. Pipeline freezes parameter values at start (immutable during run).

### 12.3 Channel DNA

`intel/dna_loader.py` provides channel-specific context:
- **Identity:** Channel name, niche, tone, audience
- **Voice:** Narration style, vocabulary, pacing preferences
- **Content strategy:** Topic selection criteria, length targets, frequency
- **Story structure:** Preferred narrative patterns, hook styles
- **Channel intelligence:** Performance benchmarks, audience insights

Agents request only the sections they need (caching efficiency).

---

## 13. Deployment

### 13.1 Railway Deployment

```yaml
# railway.toml (implied)
Build: Docker
Start: python scheduler.py --daemon
Health Check: GET /api/pulse
```

**Auto-deploy:** Push to `main` triggers CI -> Docker build -> Railway deploy.

**Environment:** All secrets stored as Railway environment variables.

### 13.2 CI/CD Pipeline

```
GitHub Actions (.github/workflows/ci.yml)
|
|-- py-lint: ruff check (E,F,W rules)
|-- ts-lint: eslint + tsc (remotion/)
|-- py-test: pytest with 26% coverage minimum
|-- ts-test: vitest (remotion/)
|-- docker: Build image (main branch only)
```

### 13.3 Local Development

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Configure API keys

# Run single video
python run_pipeline.py "The Fall of Constantinople"

# Run with resume
python run_pipeline.py "topic" --resume

# Start from specific stage
python run_pipeline.py "topic" --from-stage 7

# Run dashboard
cd dashboard && npm run dev

# Run tests
python -m pytest tests/ -v --tb=short --cov=.
```

---

## 14. Non-Functional Requirements

### 14.1 Performance

| Requirement | Target | Notes |
|---|---|---|
| End-to-end pipeline time | < 45 minutes | Parallel DAGs reduce total time |
| Agent response time | < 120 seconds per call | SLA enforcement in agent_wrapper |
| Dashboard load time | < 2 seconds | SSE for real-time updates |
| State save latency | < 100ms | Atomic write with fsync |

### 14.2 Reliability

| Requirement | Implementation |
|---|---|
| Crash recovery | Atomic state persistence + atexit handler |
| Graceful shutdown | SIGTERM handler with 5s drain |
| API failure recovery | Pipeline Doctor with 3 retry tiers |
| Rate limit handling | Exponential backoff (2s base, 5 attempts) |
| State corruption prevention | JSON validation on load, temp-file atomic writes |

### 14.3 Security

| Requirement | Implementation |
|---|---|
| API key protection | Environment variables, never in code |
| Dashboard auth | Optional password gate |
| Webhook rate limiting | 10 triggers/hour per IP |
| Audit logging | All API actions logged with IP + timestamp |
| Content safety | Compliance checker flags sensitive content |

### 14.4 Scalability

| Dimension | Current | Path to Scale |
|---|---|---|
| Videos per day | 1-2 | Parallel pipeline instances |
| Channels | 1 | Channel DNA per-channel, multi-tenant Supabase |
| Storage | 100GB Railway | S3/GCS for media, cleanup after upload |
| API costs | $3.50-$4.50/video | Model tier optimization, prompt caching |

---

## 15. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **Anthropic API outage** | Low | Critical | Pipeline Doctor retry + model fallback |
| **ElevenLabs quota exceeded** | Medium | Critical | Character count monitoring + alerts |
| **fal.ai generation failure** | Medium | Medium | Recraft fallback + Wikimedia images |
| **YouTube API quota** | Low | Critical | Daily quota monitoring in scheduler |
| **Cost overrun** | Low | Medium | Budget enforcement + per-run tracking |
| **Content policy violation** | Low | High | Compliance checker + fact verification |
| **Parameter optimizer divergence** | Low | Medium | PARAM_BOUNDS clamping + kill switch + cooldown |
| **State corruption on crash** | Low | Medium | Atomic writes + fsync + resume support |
| **Hallucinated facts** | Medium | High | 5-verdict verification system + source attribution |

---

## 16. Appendices

### Appendix A: Agent Model Routing

| Agent | Default Model | Rationale |
|---|---|---|
| 01 Research | Sonnet | Balanced reasoning + cost |
| 02 Originality | Sonnet | Creative analysis |
| 03 Narrative | Sonnet | Structured design |
| 04 Script Writer | **Opus** | Maximum creative quality |
| 04b Script Doctor | Sonnet | Evaluative judgment |
| 05 Verification | Sonnet | Fact-checking accuracy |
| 06 SEO | Sonnet | Marketing optimization |
| 07 Scene Breakdown | Sonnet | Visual decomposition |
| 07b Visual Continuity | Sonnet | Style consistency |
| 08 Audio Producer | N/A | ElevenLabs TTS |
| 09 Footage Hunter | N/A | API search |
| Thumbnail | Haiku + fal.ai | Vision scoring |
| TTS Format | Haiku | Simple transformation |
| Short Script | Sonnet | Concise creative writing |

### Appendix B: Database Schema (Supabase)

| Table | Key Columns | Purpose |
|---|---|---|
| `topics` | id, topic, status, score, queued_at | Topic queue management |
| `videos` | id, topic, title, youtube_url, youtube_id, duration, word_count, pipeline_state | Video record |
| `tuning_overrides` | param_key, value, approved_by, updated_at | Runtime parameter overrides |
| `param_observations` | video_id, params, render_verification, youtube_metrics | Optimizer training data |
| `optimizer_cycles` | epoch, proposals, auto_applied, rollback | Optimization history |
| `kv_store` | key, value | Feature flags, optimizer state |

### Appendix C: Glossary

| Term | Definition |
|---|---|
| **Channel DNA** | Channel-specific context (identity, voice, strategy) injected into agent prompts |
| **Era** | Historical period classification (Ancient, Medieval, Colonial, Modern, etc.) |
| **Hook** | Opening 15-30 seconds of video designed to maximize viewer retention |
| **Pipeline Doctor** | Automatic error recovery engine that detects and fixes recoverable failures |
| **Render Verification** | Post-render automated measurement of actual vs. intended production parameters |
| **Script Doctor** | 9-dimension script quality evaluation agent |
| **Stage Lock** | Threading lock protecting shared pipeline state during parallel execution |
| **Visual Bible** | Art style guide ensuring visual consistency across all scenes |

---

*Document generated March 2026. For the latest version, see the repository at github.com/harshithkantamneni/obsidian-archive.*
