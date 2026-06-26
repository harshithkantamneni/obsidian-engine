# Technical Report
## The Obsidian Archive — System Analysis & Engineering Report

| Field | Value |
|---|---|
| **System** | The Obsidian Archive v1.0 |
| **Author** | Harshith Kantamneni |
| **Date** | March 2026 |
| **Classification** | Internal Technical Documentation |

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Quantitative Profile](#2-quantitative-profile)
3. [Architecture Deep Dive](#3-architecture-deep-dive)
4. [Agent System Design](#4-agent-system-design)
5. [Execution Engine](#5-execution-engine)
6. [Data Flow & State Machine](#6-data-flow--state-machine)
7. [Intelligence Layer](#7-intelligence-layer)
8. [Quality Engineering](#8-quality-engineering)
9. [Resilience & Fault Tolerance](#9-resilience--fault-tolerance)
10. [Cost Engineering](#10-cost-engineering)
11. [Performance Analysis](#11-performance-analysis)
12. [Security Posture](#12-security-posture)
13. [Technical Debt Assessment](#13-technical-debt-assessment)
14. [Comparative Analysis](#14-comparative-analysis)
15. [Future Architecture Considerations](#15-future-architecture-considerations)

---

## 1. System Overview

The Obsidian Archive is a production-grade autonomous video pipeline that transforms unstructured text topics into fully rendered, SEO-optimized, YouTube-published documentary videos. The system operates as a 13-stage pipeline coordinating 20 AI agents across 8 external services, with parallel execution, self-learning parameter optimization, and closed-loop analytics feedback.

**Operational since:** Q1 2026
**Deployment:** Railway (Docker container, daemon mode)
**Primary output:** 8-25 minute YouTube documentaries on historical topics
**Secondary output:** 45-60 second YouTube Shorts (parallel pipeline)

---

## 2. Quantitative Profile

### 2.1 Codebase Metrics

| Metric | Value |
|---|---|
| **Total Python modules** | ~150 significant files |
| **Python lines of code (non-test)** | ~38,000 lines |
| **Test files** | 52 |
| **Test functions** | 1,310 |
| **Test lines of code** | 15,818 |
| **Test coverage** | 57% (minimum threshold: 26%) |
| **TypeScript/JS files** | 75 |
| **TypeScript/JS lines** | 7,830 |
| **Git commits** | 172 |
| **CI jobs** | 5 (py-lint, ts-lint, py-test, ts-test, docker) |
| **External API integrations** | 8 services |
| **Python dependencies** | 23 direct packages |

### 2.2 Agent Inventory

| Agent | Lines | Model | Purpose |
|---|---|---|---|
| 00 Topic Discovery | 1,285 | Sonnet | Viral topic scoring by channel maturity |
| 01 Research | 89 | Sonnet | Deep web research synthesis |
| 02 Originality | 176 | Sonnet | Unique angle discovery |
| 03 Narrative Architect | 232 | Sonnet | Story blueprint (6 structures, 3 length tiers) |
| 04 Script Writer | 282 | **Opus** | Full narration (1,400-2,200 words) |
| 04b Script Doctor | 331 | Sonnet | 9-dimension quality scoring |
| 05 Fact Verification | 222 | Sonnet | Multi-source claim verification |
| 06 SEO | 136 | Sonnet | YouTube metadata optimization |
| 07 Scene Breakdown | 256 | Sonnet | Visual scene decomposition |
| 07b Visual Continuity | 184 | Sonnet | Art style + character consistency |
| 08 Audio Producer | 257 | N/A | ElevenLabs TTS orchestration |
| 09 Footage Hunter | 291 | N/A | Wikimedia + Pexels sourcing |
| 11 YouTube Uploader | 929 | N/A | OAuth2 upload + metadata |
| 12 Analytics | 2,348 | N/A | YouTube performance feedback loop |
| 13 Content Auditor | 2,097 | Sonnet | 12-pass post-production QA |
| Thumbnail | 486 | Haiku | AI render + vision scoring |
| Short Script | 133 | Sonnet | 45-60s Shorts script |
| Short Storyboard | 91 | Sonnet | Vertical scene layout |
| TTS Format | 218 | Haiku | Script prep for TTS delivery |
| **Total** | **10,043** | | **20 agents** |

### 2.3 Module Size Distribution

| Directory | Files | Lines | Responsibility |
|---|---|---|---|
| agents/ | 20 | 10,043 | AI agent logic |
| core/ | 22 | 7,816 | System intelligence, quality, config |
| intel/ | 13 | 6,639 | Channel analytics, trends, engagement |
| pipeline/ | 18 | 4,445 | Phase orchestration, media processing |
| server/ | 5 | 2,522 | Webhook, notifications, topic store |
| scripts/ | 9 | 3,951 | Utility scripts |
| clients/ | 3 | 660 | External API wrappers |
| scheduler.py | 1 | 1,801 | Daemon scheduler |
| run_pipeline.py | 1 | 141 | Thin orchestrator |

### 2.4 Test Distribution

| Test File | Tests | Coverage Target |
|---|---|---|
| test_phase_production.py | 41 | Phase prod helpers |
| test_pipeline_doctor.py | 39 | Error recovery engine |
| test_phase_setup.py | 37 | Pipeline initialization |
| test_phase_script.py | 34 | Script phase logic |
| test_quality_gates.py | 33 | Quality check functions |
| test_pipeline_runner.py | 29 | StageRunner + PipelineContext |
| test_compliance_checker.py | 27 | Content safety |
| test_audio_pipeline.py | 23 | Audio generation |
| test_images_pipeline.py | 20 | Image generation |
| test_convert_pipeline.py | 18 | Remotion conversion |
| test_notify_functions.py | 17 | Notification system |
| test_channel_insights.py | 16 | Channel intelligence |
| test_pipeline_integration.py | 11 | End-to-end pipeline |
| test_thread_safety.py | 7 | Concurrent stress tests |
| Other (34 files) | 958 | Various modules |
| **Total** | **1,310** | **57% overall** |

---

## 3. Architecture Deep Dive

### 3.1 Decomposition Strategy

The system follows a **phase-based decomposition** where the monolithic pipeline is split into four sequential phases, each owning a clear subset of stages:

```
run_pipeline.py (141 lines)
    |
    |-- phase_setup.py (271 lines)
    |   Init context, load agents, credit checks, crash handlers
    |
    |-- phase_script.py (287 lines)
    |   Stages 1-5: Research -> Verification (sequential)
    |
    |-- phase_prod.py (541 lines)
    |   Stages 6-12: SEO -> Render (3 parallel waves + QA)
    |
    |-- phase_post.py (393 lines)
        Stage 13+: Upload -> Analytics -> Cleanup
```

**Key design decisions:**

1. **PipelineContext dataclass** replaces 13 nested closures with explicit fields. All phase functions receive `ctx` as their first argument, making data flow visible and testable.

2. **StageRunner class** encapsulates the stage execution lifecycle (skip check, timing, marking, recovery), providing thread-safe state recording through `mark()` and `mark_metadata()`.

3. **Re-export compatibility layer** in `run_pipeline.py` preserves all existing test imports (`run_pipeline.load_state(...)`, `run_pipeline.check_research(...)`) via `# noqa: F401` re-exports.

### 3.2 Data Flow Architecture

```
                    Topic (string)
                         |
                    [Phase Setup]
                         |
                    PipelineContext
                    {state: {}, agents: {}, ...}
                         |
         ┌───────────────┼───────────────────┐
         |               |                   |
    [Research]     [Originality]        [Narrative]
    ctx.research   ctx.angle           ctx.blueprint
         |               |                   |
         └───────────────┼───────────────────┘
                         |
                    [Script Writer]
                    ctx.script
                         |
                  [Script Doctor] (0-2 rewrites)
                         |
                  [Fact Verification]
                  ctx.verification
                         |
         ┌───────────────┼───────────────────┐
         |               |                   |
      [SEO]         [Scenes]          [Compliance]
      ctx.seo      ctx.scenes_data    state["compliance"]
         |               |                   |
         └───────────────┼───────────────────┘
                         |
                  [Visual Continuity]
                  [TTS Formatting]
                         |
         ┌───────────────┼───────────────────┐
         |               |                   |
      [Audio]       [Footage]         [Thumbnail]
      ctx.audio    ctx.footage       state["thumbnail"]
         |               |                   |
         └───────────────┼───────────────────┘
                         |
                    [Image Gen]
                    ctx.manifest
                         |
                    [Convert -> Render]
                    state["stage_12"]
                         |
                    [QA Tiers 0-2]
                         |
                    [Upload -> Analytics]
                    state["stage_13"]
```

### 3.3 Concurrency Architecture

Three ThreadPoolExecutor instances coordinate parallel work:

**Wave 1 (max_workers=3):**
```python
with ThreadPoolExecutor(max_workers=3) as dag:
    seo_future = dag.submit(runner.run_stage, 6, "SEO", seo_fn)
    scene_future = dag.submit(runner.run_stage, 7, "Scenes", scene_fn)
    compliance_future = dag.submit(_run_compliance, ctx, runner)
    # All .result() collected before proceeding
```

**Wave 3 (max_workers=3):**
```python
with ThreadPoolExecutor(max_workers=3) as dag:
    audio_future = dag.submit(runner.run_stage, 8, "Audio", audio_fn)
    footage_future = dag.submit(runner.run_stage, 9, "Footage", footage_fn)
    thumb_future = dag.submit(_generate_thumbnail_task, ctx)
```

**Shorts Background (max_workers=1):**
```python
shorts_executor = ThreadPoolExecutor(max_workers=1)
shorts_future = shorts_executor.submit(_shorts_pipeline_impl, ctx, runner)
# Collected in phase_post via shorts_future.result(timeout=900)
```

**Thread safety invariant:** All state writes during parallel phases acquire `ctx.stage_lock`. This is enforced through `StageRunner.mark()` (for numbered stages), `StageRunner.mark_metadata()` (for arbitrary keys), and `StageRunner.run_short_stage()` (for shorts stages).

Stress-tested with 7 concurrent tests including 50-thread metadata writes and barrier-synchronized duplicate detection.

---

## 4. Agent System Design

### 4.1 Agent Execution Wrapper

All agents execute through `core/agent_wrapper.py`, which provides:

**4-Tier Model Routing:**
```
OPUS (claude-opus-4-6)       — Premium creative tasks (script writing)
SONNET (claude-sonnet-4-6)   — Full analysis (research, SEO, scenes)
HAIKU (claude-haiku-4-5)     — Light tasks (scoring, formatting)
NANO (cached)                — Repeated prompts with identical context
```

Effort offset system allows dynamic model selection: `-1` demotes to cheaper tier, `+1` promotes to higher quality.

**Structured Output Enforcement:**
- JSON Schema validation via `output_config={"format":{"type":"json_schema","schema":...}}`
- Automatic retry on malformed output (up to 3 attempts)
- Schema registry (`core/structured_schemas.py`) with per-agent schemas

**SLA Enforcement:**
- 45-120 second timeout per agent call (configurable)
- Diagnostic logging: prompt length, response time, token usage
- JSONL trace file with 5MB rotation

**Recovery Integration:**
- Pipeline Doctor intercepts failures in `StageRunner.run_stage()`
- Categorizes errors: rate_limit, timeout, hallucination, schema_error, unknown
- Applies strategy: retry, model_switch, prompt_modification, skip

### 4.2 Agent Communication Pattern

Agents do not communicate directly. All inter-agent data flows through `PipelineContext`:

```python
# Stage 1: Research agent writes to ctx
ctx.research = runner.run_stage(1, "Research", agent01.run, topic)

# Stage 4: Script agent reads from ctx
ctx.script = runner.run_stage(4, "Script", agent04.run,
    ctx.research, ctx.angle, ctx.blueprint, channel_dna)
```

This explicit data threading (vs. shared mutable state) makes dependencies visible and testable.

### 4.3 Structured Output Schemas

Each agent defines its output schema in `core/structured_schemas.py`:

| Schema | Key Fields | Validation |
|---|---|---|
| RESEARCH | core_facts[], key_figures[], timeline[] | Min 5 facts, 2 figures |
| ORIGINALITY | chosen_angle, uniqueness_score, twist_potential | chosen_angle required |
| NARRATIVE | structure_type, length_tier, hook{}, act1-3{}, ending{} | All acts required |
| SCRIPT | full_script, word_count, estimated_seconds | Word count within bounds |
| VERIFICATION | overall_verdict, claims[], corrections[] | Not REQUIRES_REWRITE |
| SEO | recommended_title, description, tags[], chapter_markers[] | Title present |
| SCENES | scenes[] with mood, visual_type, duration_seconds | Min 5 scenes |
| SCRIPT_DOCTOR | 9 score fields (1-10), specific_fixes[] | Average >= 7.0 |

Exception: Agent 13 (Content Auditor) uses per-call custom schemas, not the registry.

---

## 5. Execution Engine

### 5.1 StageRunner Implementation

```python
class StageRunner:
    def __init__(self, ctx: PipelineContext):
        self.ctx = ctx

    def done(self, stage) -> bool:
        """Resume skip gate: checks completed_stages + validate_stage_output."""

    def mark(self, stage, data, elapsed=None):
        """Record stage completion under ctx.stage_lock + save_state."""

    def mark_metadata(self, key, value):
        """Set arbitrary state key under lock (for parallel phases)."""

    def run_stage(self, num, name, fn, *args):
        """Execute stage with: skip check -> timing -> recovery -> mark."""

    def run_short_stage(self, key, name, fn, *args):
        """Short stages with string-key tracking under lock."""
```

**Execution flow for `run_stage()`:**
```
1. Skip check: num < from_stage OR done(num) -> return cached result
2. Print stage banner
3. Start timer
4. Try fn(*args)
5. On failure: Pipeline Doctor intervene() with 3-attempt guard
6. Record elapsed time
7. mark(num, result, elapsed) -> acquires lock, saves state
8. Budget check (optional, raises BudgetExceededError)
9. Return result
```

### 5.2 State Persistence

`pipeline/state.py` implements crash-safe persistence:

```python
def save_state(state: dict, state_path: Path) -> None:
    tmp_path = state_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())  # Force write to disk
    tmp_path.replace(state_path)  # Atomic rename
```

**Recovery chain:**
1. Normal operation: atomic write after every stage
2. Process crash: `atexit` handler saves current state
3. SIGTERM (Railway shutdown): handler saves state + 5s drain
4. Corrupted state file: JSON validation on load, skip invalid

### 5.3 Resume Mechanism

```
--resume flag:
1. Find most recent *_state.json for this topic slug
2. Load state dict with completed_stages list
3. StageRunner.done(N) checks: N in completed_stages AND validate_stage_output(N, data)
4. If both true, skip stage and return cached result
5. If output invalid, re-run stage despite being in completed list

--from-stage N:
1. Auto-enables resume
2. StageRunner.run_stage() skips all stages where num < N
3. State must contain prerequisite data (research, angle, etc.)
```

---

## 6. Data Flow & State Machine

### 6.1 Pipeline State Schema

```json
{
  "topic": "The Fall of Constantinople",
  "era": "medieval",
  "completed_stages": [1, 2, 3, 4, 5, 6, 7],
  "completed_short_stages": ["short_script", "short_storyboard"],

  "stage_1": { "core_facts": [...], "key_figures": [...] },
  "stage_2": { "chosen_angle": "...", "uniqueness_score": 8 },
  "stage_3": { "hook": {...}, "act1": {...}, "act2": {...}, "act3": {...} },
  "stage_4": { "full_script": "...", "word_count": 1650 },
  "stage_5": { "overall_verdict": "VERIFIED", "claims": [...] },
  "stage_6": { "recommended_title": "...", "tags": [...] },
  "stage_7": { "scenes": [...] },
  "stage_8": { "audio_path": "...", "total_duration_seconds": 480 },
  "stage_9": { "footage": [...] },
  "stage_12": "/path/to/rendered_video.mp4",
  "stage_13": { "video_id": "abc123", "url": "https://youtube.com/..." },

  "stage_timings": { "1": 12.3, "2": 8.7, ... },
  "compliance": { "risk_level": "green", "flag_count": 0 },
  "thumbnail": { "path": "...", "score": 8.5 },
  "predictive_score": { "score": 8, "max": 11, "reasons": [...] },
  "render_verification": { "overall_compliance": 0.95, "deviations": [] },
  "costs": { "usd_total": 3.72, "per_stage": {...}, "per_service": {...} },
  "pipeline_status": "COMPLETE",
  "elapsed_seconds": 1847.3
}
```

### 6.2 State Transitions

```
INIT -> RESEARCH_COMPLETE -> ANGLE_COMPLETE -> BLUEPRINT_COMPLETE
  -> SCRIPT_COMPLETE -> VERIFIED -> SEO_COMPLETE -> SCENES_COMPLETE
  -> AUDIO_COMPLETE -> FOOTAGE_COMPLETE -> IMAGES_COMPLETE
  -> RENDERED -> QA_PASSED -> UPLOADED -> COMPLETE
```

Each transition adds the stage number to `completed_stages` and writes stage data to `stage_N`. The transition is atomic (single `save_state` call under lock).

### 6.3 External Data Sources

| Source | Data | Freshness | Fallback |
|---|---|---|---|
| channel_insights.json | YouTube performance, audience sentiment | 72h max age | Bayesian priors |
| lessons_learned.json | Pipeline Doctor interventions, quality patterns | Per-run update | Empty dict |
| cost_log.json | Per-run cost history | Real-time append | In-memory only |
| optimizer_log.jsonl | Parameter observation history | Per-run append | No optimization |
| Supabase topics table | Topic queue with scores | Real-time | Local JSON fallback |
| Supabase videos table | Published video records | Real-time | Skip Supabase save |

---

## 7. Intelligence Layer

### 7.1 Confidence Blending

The system doesn't naively trust its own data. `intel/channel_insights.py` implements confidence blending:

```
confidence_weight = min(1.0, video_count / 30)

blended_metric = (confidence_weight * own_metric) +
                 ((1 - confidence_weight) * bayesian_prior)
```

At 0 videos: 100% prior knowledge (from `youtube_knowledge_base.py`)
At 15 videos: 50% own data, 50% priors
At 30+ videos: 100% own data

Priors are stratified by subscriber tier (0-1K, 1K-10K, 10K-100K, 100K+) and content category (history/documentary).

### 7.2 Parameter Optimizer Design

Neural-network-inspired optimization without actually running a neural network:

**Loss function:**
```
L = 0.35 * retention_loss +
    0.20 * views_velocity_loss +
    0.20 * engagement_loss +
    0.10 * sentiment_loss +
    0.15 * hook_retention_loss
```

**Gradient estimation:** Finite differences between paired observations (same era, different params):
```
grad_i = (L(params_B) - L(params_A)) / (params_B[i] - params_A[i])
```

**Momentum:** Exponential moving average of gradients (beta=0.9):
```
momentum[i] = beta * momentum[i] + (1 - beta) * grad[i]
```

**Update rule:**
```
params[i] -= learning_rate * momentum[i]
params[i] = clamp(params[i], PARAM_BOUNDS[i].min, PARAM_BOUNDS[i].max)
```

**Safety mechanisms:**
- All updates clamped to PARAM_BOUNDS
- Brand consistency: Shorts params stay within 0.15 of long-form defaults
- Kill switch in Supabase kv_store
- Cooldown: 10 videos between rollbacks
- Approval workflow for changes > 10% of current value

### 7.3 Correlation Engine

7-layer directed acyclic graph for statistical analysis:

```
Layer 1: Script params ──> Script quality metrics
Layer 2: Short params ──> Short performance metrics
Layer 3: Raw topic signals ──> Baseline health
Layer 4: Audience signals ──> Transformation rates
Layer 5: Short prod ──> Parent video lift
Layer 6: Era stratification ──> Per-era parameter effects
Layer 7: Cross-format ──> Shorts insights -> Long-form transfer
```

Uses Pearson correlation with p-values. Multiple comparison correction via Benjamini-Hochberg FDR.

### 7.4 Topic Discovery Scoring

Agent 00 scores topics across 14+ signals with maturity-adjusted weights:

| Signal | Weight (Early) | Weight (Mature) |
|---|---|---|
| Search demand | 1.5x | 1.0x |
| Subscriber conversion potential | 1.0x | 0.8x |
| Engagement prediction | 1.0x | 1.2x |
| Era performance (own data) | 0.5x | 1.5x |
| Audience requests | 1.0x | 1.3x |
| Competitive saturation | 1.0x | 1.0x |
| Trending velocity | 1.5x | 0.8x |
| Novelty (not yet covered) | 1.2x | 1.0x |

---

## 8. Quality Engineering

### 8.1 Quality Gate Architecture

Three tiers with different failure modes:

```
TIER 0 — GATE (abort pipeline)
├── gate_script_length(): 1,000-2,500 words (varies by tier)
├── gate_verification_passed(): Not REQUIRES_REWRITE
├── gate_audio_exists(): File > 500KB
├── gate_render_exists(): Video > 50MB
├── gate_wpm_range(): 130 WPM ± 15%
├── gate_script_breathability(): Max 3 consecutive dense sentences
└── gate_short_script_length(): 80-180 words

TIER 1 — WARNING (logged, non-blocking)
├── quality_research(): Source authority, specificity
├── quality_script(): Emotional arc, active voice, hook
├── quality_scenes(): Visual diversity, mood consistency
├── quality_audio(): Peak levels, compression
├── quality_images(): Resolution, aesthetic consistency
├── quality_seo(): Title length, tag coverage
├── quality_content_policy(): Copyright, brand safety
└── ... (18 functions total)

TIER 2 — METRIC (observational, feeds optimizer)
├── metrics_script(): word_count, readability_score
├── metrics_audio(): dynamic_range_db, noise_floor
├── metrics_images(): color_variance, avg_resolution
└── ... (4 functions total)
```

### 8.2 Script Quality Loop

```
Script Writer (Opus)
      |
      v
Script Doctor evaluation (9 dimensions)
      |
      ├── Score >= 7.0 ──> PASS
      |
      └── Score < 7.0 ──> Rewrite with specific feedback
                |
                v
          Script Writer (rewrite #1)
                |
                v
          Script Doctor re-evaluation
                |
                ├── Score >= 7.0 ──> PASS
                |
                └── Score < 7.0 ──> Rewrite #2 (final attempt)
                          |
                          v
                    Score >= 7.0 ──> PASS
                    Score < 7.0 ──> RuntimeError (pipeline aborts)
```

### 8.3 Render Verification

Post-render measurements using ffmpeg and PIL:

| Metric | Target | Tolerance | Method |
|---|---|---|---|
| Loudness | -14 LUFS | ± 2 LUFS | ffmpeg ebur128 filter |
| WPM | 130 | ± 15% | word_count / duration |
| Speech ducking | 0.13 primary | ± 0.03 | Segment loudness analysis |
| Silence ducking | 0.28 primary | ± 0.05 | Segment loudness analysis |
| Resolution | 1920x1080 | Exact | ffprobe |
| Black frames | 0 | < 3 frames | Pixel analysis |
| Scene sharpness | > 50 (Laplacian) | Per-scene | PIL Laplacian variance |

Overall compliance score: weighted average (0.0-1.0). Deviations listed for investigation.

---

## 9. Resilience & Fault Tolerance

### 9.1 Error Recovery (Pipeline Doctor)

`core/pipeline_doctor.py` provides multi-strategy automatic recovery:

**Error categorization:**
```python
def categorize(error):
    if "rate_limit" in str(error).lower() or "429" in str(error):
        return "rate_limit"
    if "timeout" in str(error).lower():
        return "timeout"
    if isinstance(error, json.JSONDecodeError):
        return "schema_error"
    # ... more categories
    return "unknown"
```

**Recovery strategies:**

| Category | Strategy | Details |
|---|---|---|
| rate_limit | Wait + retry | Exponential backoff (2s, 4s, 8s) |
| timeout | Retry with faster model | Demote Opus->Sonnet, Sonnet->Haiku |
| schema_error | Retry with explicit format | Add JSON formatting instructions |
| hallucination | Retry with grounding | Strengthen research context in prompt |
| unknown | Raise original error | No recovery attempted |

**Recursion guard:** Maximum 3 recovery attempts per stage. Guard tracked via `FALLBACK_SAFE_STAGES` set.

### 9.2 External Service Resilience

| Service | Retry | Backoff | Fallback |
|---|---|---|---|
| Anthropic Claude | 5 attempts | Exponential (2s base) | Model tier demotion |
| ElevenLabs | 5 attempts | Exponential (2s base) | N/A (critical) |
| fal.ai | 3 attempts | Exponential (1s base) | Recraft model, then Wikimedia |
| Supabase | 3 attempts | Exponential (1s base) | Local JSON fallback |
| YouTube API | 3 attempts | Exponential (2s base) | N/A (critical) |
| Wikimedia | 1 attempt | None | Pexels fallback |
| Pexels | 1 attempt | None | AI-generated image |

### 9.3 State Durability

```
Normal operation:
  save_state() -> temp file -> fsync -> atomic rename

Process crash:
  atexit handler -> save_state() (best effort, no lock)

SIGTERM (Railway):
  signal handler -> set shutdown_event -> save_state() -> 5s drain -> exit(1)

Corrupted state:
  load_state() -> JSON parse error -> return empty dict -> pipeline restarts

Disk full:
  save_state() -> write error -> state preserved in memory -> next save retries
```

### 9.4 Download Safety

All external file downloads use `pipeline.helpers.download_file()`:
```python
def download_file(url, dest, timeout=60):
    response = requests.get(url, timeout=timeout, stream=True)
    response.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
```

Replaces 5 prior `urllib.request.urlretrieve()` calls that had no timeout protection.

---

## 10. Cost Engineering

### 10.1 Service Pricing

| Service | Unit | Cost | Typical Usage/Video |
|---|---|---|---|
| Claude Opus | 1M input tokens | $15.00 | ~15K input, 3K output |
| Claude Opus | 1M output tokens | $75.00 | |
| Claude Sonnet | 1M input tokens | $3.00 | ~30K input, 10K output |
| Claude Sonnet | 1M output tokens | $15.00 | |
| Claude Haiku | 1M input tokens | $0.80 | ~5K input, 2K output |
| Claude Haiku | 1M output tokens | $4.00 | |
| ElevenLabs | 1K characters | $0.30 | ~3K characters |
| fal.ai | 1 image | $0.05 | ~24 images |
| YouTube API | Quota units | Free | Within daily quota |
| Supabase | Free tier | $0 | Within limits |

### 10.2 Typical Cost Breakdown

| Component | Cost | Percentage |
|---|---|---|
| Claude Opus (script writing) | $1.50 | 38% |
| fal.ai (image generation) | $1.20 | 31% |
| Claude Sonnet (6 analysis agents) | $0.40 | 10% |
| ElevenLabs (TTS) | $0.35 | 9% |
| Claude Haiku (scoring + formatting) | $0.05 | 1% |
| Infrastructure (Railway) | $0.40 | 10% |
| **Total** | **$3.90** | **100%** |

### 10.3 Cost Optimization Strategies

1. **Model tier routing:** Only Script Writer uses Opus. All analysis uses Sonnet. Scoring uses Haiku. Saves ~60% vs. all-Opus.
2. **Prompt caching:** Claude's prompt caching reduces repeat context costs by 90%.
3. **Conditional execution:** Shorts pipeline only if main succeeds. Thumbnail only if render succeeds.
4. **Post-upload cleanup:** Intermediate files deleted, saving ~2-5GB/video of storage.
5. **Budget enforcement:** `COST_BUDGET_MAX_USD` aborts pipeline if exceeded.

### 10.4 Cost Tracking Architecture

```
Agent call
    |
    v
claude_client.py: accumulate tokens (thread-safe)
    |
    v
cost_tracker.py: log_cost(run_id, stage, service, units)
    |
    v
cost_log.json (JSONL append)
    |
    v
Supabase videos table (usd_total)
    |
    v
Monthly ROI reports (cost-per-view, cost-per-subscriber)
```

---

## 11. Performance Analysis

### 11.1 Pipeline Timing Profile

Typical 10-minute documentary (1,650 words):

| Phase | Duration | Parallelism |
|---|---|---|
| Setup (init, credits, agents) | 5-10s | Sequential |
| Stage 1: Research | 15-25s | Sequential |
| Stage 2: Originality | 8-15s | Sequential |
| Stage 3: Narrative | 10-20s | Sequential |
| Stage 4: Script + Doctor | 30-60s | Sequential (1-3 iterations) |
| Stage 5: Verification | 15-25s | Sequential |
| Wave 1: SEO + Scenes + Compliance | 20-30s | 3 parallel |
| Wave 2: Visual Continuity + TTS Format | 10-20s | Sequential |
| Wave 3: Audio + Footage + Thumbnail | 60-120s | 3 parallel |
| Stage 10: Image Generation | 120-300s | 3-worker pool |
| Stage 11: Data Conversion | 5-10s | Sequential |
| Stage 12: Video Render | 120-300s | Remotion (CPU-bound) |
| QA Tiers | 10-30s | Sequential |
| Stage 13: Upload | 60-180s | Sequential (network-bound) |
| Post-upload operations | 10-30s | Sequential |
| **Total** | **25-40 minutes** | |

**Bottlenecks:** Image generation (fal.ai latency) and video rendering (CPU-intensive Remotion) dominate. Parallel execution of Waves 1 and 3 saves approximately 5-8 minutes per run.

### 11.2 Resource Usage

| Resource | Typical | Peak |
|---|---|---|
| Memory | 200-400MB | 800MB (during render) |
| CPU | 1-2 cores | 4 cores (Remotion render) |
| Disk (working) | 2-5GB | 8GB (pre-cleanup) |
| Disk (after cleanup) | 50-200MB | 500MB (state + logs) |
| Network | Moderate | Burst during upload |

---

## 12. Security Posture

### 12.1 Authentication & Authorization

| Component | Method | Notes |
|---|---|---|
| API keys | Environment variables | Never in code or logs |
| YouTube OAuth2 | Token file (base64 in env on Railway) | Auto-refresh on expiry |
| Dashboard | Optional password | `DASHBOARD_PASSWORD` env var |
| Webhook triggers | Rate limiting | 10/hour per IP |
| Supabase | Row-level security | Service key (server-side only) |

### 12.2 Audit Trail

All API interactions logged in `webhook_server.py`:
```python
_audit_logger.info(json.dumps({
    "action": action,
    "ip": request.remote_addr,
    "timestamp": datetime.utcnow().isoformat(),
    "details": details,
}))
```

Stored in `outputs/audit/audit.log` (rotation-managed).

### 12.3 Content Safety

- Compliance checker scans scripts for sensitive claims, misinformation, hate speech
- Risk levels: green (proceed), yellow (flag for review), red (abort or require safe_script)
- Fact verification agent cross-references claims against 2+ sources
- 5 verdict levels: VERIFIED_SCHOLARLY, VERIFIED_POPULAR, UNVERIFIED, DISPUTED, HALLUCINATION

---

## 13. Technical Debt Assessment

### 13.1 Known Issues (Low Priority)

| Issue | Impact | Effort | Notes |
|---|---|---|---|
| `server/notifier.py` still exists | Dead code | 5 min | Legacy Discord-only, replaced by notify.py |
| `core/pipeline_optimizer.py` exists | Dead code | 5 min | Replaced by param_optimizer.py |
| Some agents have inline schemas | Inconsistency | 1 hour | Agent 13 uses per-call schemas (intentional) |
| `scheduler.py` is 1,801 lines | Readability | 4 hours | Candidate for phase decomposition |
| No `.env.example` file | Onboarding friction | 10 min | Document required vars |

### 13.2 Architecture Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Single-server deployment | Medium | Stateless design enables multi-instance |
| JSON file state (not DB) | Low | Atomic writes + resume sufficient for single server |
| Thread safety relies on single lock | Low | Stress-tested; lock granularity sufficient for 3 workers |
| No circuit breaker for external services | Medium | Pipeline Doctor provides per-call recovery |

### 13.3 Codebase Health Metrics

| Metric | Value | Assessment |
|---|---|---|
| Cyclomatic complexity (avg) | Low-Medium | Most functions < 10 paths |
| Module coupling | Low | Phase decomposition enforces boundaries |
| Test coverage | 57% | Good for pipeline code; scripts/ uncovered |
| Lint violations | 0 | ruff E,F,W clean |
| Type annotations | Partial | TYPE_CHECKING blocks + dataclass fields |

---

## 14. Comparative Analysis

### 14.1 vs. Manual Production

| Dimension | Manual | Obsidian Archive |
|---|---|---|
| Research time | 8-15 hours | 15-25 seconds |
| Script writing | 4-8 hours | 30-60 seconds |
| Visual production | 6-12 hours | 5-10 minutes |
| Audio production | 2-4 hours | 1-2 minutes |
| SEO optimization | 1-2 hours | 10-15 seconds |
| Post-production | 4-8 hours | 5-10 minutes (automated render) |
| **Total** | **25-50 hours** | **25-40 minutes** |
| **Cost** | **$200-500** (freelancers) | **$3.50-$4.50** |

### 14.2 vs. Existing Tools

| Feature | Obsidian Archive | Typical AI Video Tools |
|---|---|---|
| End-to-end automation | Full pipeline (topic to YouTube) | Single-step (script OR images OR TTS) |
| Quality gates | 3-tier (32 checks) | None or basic |
| Self-learning | Parameter optimizer + analytics feedback | None |
| Fact verification | 5-verdict system with sources | None |
| Narrative structure | 6 structures, 3 length tiers | Template-based |
| Thread-safe parallel execution | 3 ThreadPoolExecutors | Sequential |
| Resume/recovery | Atomic state + Pipeline Doctor | Re-run from scratch |
| Cost tracking | Per-run, per-service, ROI analysis | None |
| Channel intelligence | Confidence-blended with Bayesian priors | None |

---

## 15. Future Architecture Considerations

### 15.1 Horizontal Scaling

Current single-server design can scale to multi-instance by:
1. Moving state from JSON files to Supabase (state table with row locking)
2. Using Redis or Supabase for stage_lock (distributed lock)
3. Running multiple pipeline workers pulling from topic queue
4. Shared media storage (S3/GCS instead of local disk)

### 15.2 Multi-Channel Support

Path to supporting multiple YouTube channels:
1. Channel DNA per-channel (already structured for this)
2. Channel-specific parameter overrides (add channel_id to tuning_overrides)
3. Multi-tenant Supabase schema (add channel_id FK)
4. Per-channel scheduling in scheduler.py

### 15.3 Content Format Expansion

The agent architecture supports new content formats by:
1. Adding new agents (e.g., podcast_script_agent, blog_writer_agent)
2. Adding new pipeline phases (same StageRunner infrastructure)
3. New render targets (beyond Remotion — e.g., podcast RSS, blog CMS)

### 15.4 Monitoring Evolution

From print-based logging to full observability:
1. Structured JSON logging (completed in v1.0)
2. Log aggregation (ELK/Loki) for multi-instance
3. Metrics (Prometheus) for SLA dashboards
4. Distributed tracing (OpenTelemetry) for agent call chains

---

*Report compiled March 2026. Source: github.com/harshithkantamneni/obsidian-archive (172 commits, 1,310 tests, 57% coverage).*
