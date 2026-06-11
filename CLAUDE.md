# Obsidian Engine — Project Intelligence

## What This Is

Open-source AI video pipeline. Takes a topic, outputs a complete YouTube video. Pluggable providers, swappable content styles, browser-based setup wizard. MIT licensed.

**Repo:** https://github.com/harshithkantamneni/obsidian-engine

## Architecture

13-stage automated pipeline → rendered video → upload.

```
Topic → Research(1) → Originality(2) → Narrative(3) → Script(4) → ScriptDoctor(4b)
     → Verification(5) → SEO(6) → Scenes(7) → VisualContinuity(7b) → TTS(8)
     → Footage(9) → Images(10) → Video(11) → QA(12) → Upload(13)
```

**Core call chain:** `agents/*.py` → `core/agent_wrapper.py:call_agent()` → `clients/claude_client.py:call_claude()` → Anthropic API

### Key Files

| Area | Files |
|------|-------|
| Pipeline orchestrator | `run_pipeline.py` (decomposed into `pipeline/` modules) |
| Agent wrapper (SLA/recovery) | `core/agent_wrapper.py` |
| Claude API client | `clients/claude_client.py` |
| **Epidemic Sound client** | `clients/epidemic_client.py` (MCP protocol, music/SFX/stems/voiceover) |
| **Master config** | `obsidian.yaml` → `core/config.py` (dot-access `cfg` singleton) |
| **Config constants** | `core/pipeline_config.py` (reads from `cfg`, backward-compatible) |
| **Content profiles** | `profiles/*.yaml` → `core/profile.py` (loader + cache) |
| **Profile injection** | `intel/dna_loader.py:get_dna()` prepends style directive |
| **Provider abstraction** | `providers/base.py` (7 ABCs) → `providers/registry.py` (factory) |
| **Provider implementations** | `providers/llm/`, `providers/tts/`, `providers/images/`, `providers/footage/`, `providers/upload/`, `providers/music/`, `providers/sfx/` |
| Music manager (API) | `media/epidemic_music_manager.py` (mood→search, download+cache) |
| Music manager (local) | `media/music_manager.py` (library scan, energy matching, rotation) |
| Track adapter | `media/track_adapter.py` (adapt to exact duration, download stems) |
| SFX manager | `media/epidemic_sfx_manager.py` (per-scene SFX from API) |
| Forced alignment | `media/forced_alignment.py` (Whisper word timestamps for TTS) |
| Structured output schemas | `core/structured_schemas.py` |
| Observability | `core/observability.py` |
| Dashboard | `dashboard/src/` (Preact + Signals + Tailwind, 6 tabs + Intel music tab) |
| Webhook server | `server/webhook_server.py` (Flask + SSE) |
| Setup wizard API | `server/webhook_server.py` (`/api/setup/status`, `/api/setup/validate`, `/api/setup/save`) |
| Notifications | `server/notify.py` (Telegram `_tg()` + Discord `_send()`) |
| Video renderer | `remotion/src/` |
| Scheduler | `scheduler.py` |

### Configuration System

All config flows through `obsidian.yaml` → `core/config.py`:

```python
from core.config import cfg
cfg.voice.narrator_id       # Dot-access to any YAML key
cfg.models.premium           # "claude-opus-4-6"
cfg.get("profile", "documentary")  # Safe access with default
cfg.voice.body.to_dict()    # Convert section to plain dict
```

Override priority: defaults → `obsidian.yaml` → env vars (`OBSIDIAN_` prefix)

### Profile System

Profiles control tone, structure, and visuals. Set in `obsidian.yaml`:
```yaml
profile: documentary  # or explainer, true_crime, video_essay, custom
```

Profile's `style_directive` is injected into every agent via `intel/dna_loader.py:get_dna()`.

Key functions: `core/profile.py` → `get_profile()`, `get_style_directive()`, `get_mood_palette()`, `get_hook_registers()`, `reset_profile_cache()`

### Provider System

Swap any external service in `obsidian.yaml`:
```yaml
providers:
  llm:
    name: anthropic    # or openai, or custom.module.ClassName
  tts:
    name: elevenlabs   # or epidemic_sound, openai (PR pending)
  images:
    name: fal
  footage:
    name: pexels
  upload:
    name: local
  music:
    name: auto         # epidemic_sound, local, or auto
  sfx:
    name: auto
```

Key functions: `providers/registry.py` → `get_provider("llm")`, `list_providers()`, `clear_cache()`

Built-in providers: `anthropic`, `openai`, `elevenlabs`, `epidemic_sound`, `fal`, `pexels`, `local`

Provider types (7): `llm`, `tts`, `images`, `footage`, `upload`, `music`, `sfx`

### Epidemic Sound Integration

**API**: MCP server at `https://www.epidemicsound.com/a/mcp-service/mcp` — JSON-RPC protocol, NOT REST.
**Auth**: Bearer token, expires every 30 days. Key in `.env` only — NEVER commit to repo.
**Client**: `clients/epidemic_client.py` uses `_call_tool("tool_name", {args})` for all MCP tool calls.

**Capabilities (5 phases implemented):**
1. Music search/download with mood→search mapping for all 8 moods
2. Track adaptation to exact video duration + stem separation (bass/drums/instruments)
3. Dynamic per-scene SFX and ambient from API with session caching
4. Voiceover as TTS alternative (`providers/tts/epidemic.py`) with Whisper forced alignment
5. Analytics feedback loop: mood/BPM/source/adapted/stems → retention correlation → self-tuning

**Fallback chain**: Epidemic API → cached epidemic_* → local premium → Kevin MacLeod → hardcoded MOOD_MUSIC

**Stem ducking self-tunes via parameter optimizer** — values in `PARAM_BOUNDS` (param_overrides.py), read via `get_override()`, compared against YouTube retention after 48h.

### Series Continuity

- Detection: `pipeline/series.py` → `detect_series_potential()`
- Part 2+ context: metadata in Supabase topics table (`metadata JSONB`), loaded via `pipeline/phase_setup.py:_load_parent_context()`
- Agents receive parent research/script/angle for continuations
- **Visual bible persists across series**: Part 2+ inherits Part 1's art_style, color_palette, character_descriptions via `parent_visual_bible` in `07b_visual_continuity.py`
- **Cliffhanger protection**: Script Doctor (04b) locks the final 30 words when `part_1_cliffhanger` is set in blueprint — prevents smoothing Part 1 ending into resolved state
- Dedup skipped for series parts, series detection skipped for continuations

### Scene Intent System

Unified intent resolution: `media/scene_intent.py` → `resolve_all_scenes()`

**Input fields** (from scene breakdown + agents): `mood`, `narrative_function` (20 values), `narrative_position`, `is_reveal_moment`, `is_breathing_room`

**Resolved output fields** (computed per scene):
- `intent_transition_type`: normal | act | reveal | silence
- `intent_motion_seed`: 0-15 (KenBurns pattern)
- `intent_music_volume_base`: 0.0-1.0
- `intent_pace_modifier`: 0.8-1.2
- `intent_caption_style`: standard | emphasis | whisper
- `intent_scene_energy`: 0.0-1.0
- `intent_speech_intensity`: 0.0-1.0 (drives TTS delivery dynamics)
- `intent_silence_beat`: bool (coordinated audio+visual silence)

**20 narrative functions**: cold_open, hook, setup, exposition, rising_action, complication, question, answer, escalation, climax, twist, reveal, falling_action, breathing_room, reflection, resolution, conclusion, coda, callback, silence

### Visual Bible System

Agent 07b produces a visual bible that now reaches image generation:
- `art_style` → replaces hardcoded STYLE_FLUX in image prompts
- `character_descriptions` → injected into per-scene prompts when characters appear
- `color_palette` → hex hints in prompts + post-gen color harmonization (PIL blend)
- `recurring_motifs` → injected for tense/dark/dramatic/cold scenes

**Era constraints**: `ERA_CONSTRAINTS` in `pipeline/images.py` maps 7 eras (Mauryan, Roman, Medieval, Egyptian, Mughal, Greek, Colonial) to positive anchors + negative prompts. Auto-detected from scene keywords/years.

### Epistemic Labeling

- `claim_confidence` field per scene: `established | contested | speculative | null`
- Verification agent (stage 5) data passed to scene breakdown agent (stage 7)
- Disputed/low-confidence claims tagged in scene breakdown prompt
- Flows through to Remotion TypeScript interface

### Audio Systems

- **Asymmetric ducking**: 0.1s attack (fast duck down) / 0.4s release (gentle return)
- **Silence beats**: `intent_silence_beat` suppresses ambient, SFX, captions, and all graphics overlays
- **Speech intensity**: `intent_speech_intensity` modulates ElevenLabs stability/style/speed per scene
- **Architectural ambient**: location-aware selection (stone_interior, marketplace, nature_wind, military_camp) overrides mood-based
- **Pronunciation guide**: TTS format agent respells 20+ historical terms (Sanskrit, Greek, Egyptian)

### Scene Manifest & Analytics

- `pipeline/convert.py` builds per-scene metadata manifest (mood, function, intent fields, claim_confidence, etc.)
- Stored in Supabase `videos.scene_manifest` JSONB column
- Analytics agent overlays retention curves against scene positions
- `_compute_scene_retention_correlation()` groups drops by mood, function, treatment, silence_beat
- `get_scene_retention_intelligence()` in `channel_insights.py` returns data-backed guidance

### Self-Tuning Parameters

37 params in `PARAM_BOUNDS` (core/param_overrides.py). Pipeline reads via `get_override(key, default)`. Optimizer self-tunes from YouTube retention data after 48h.

**Categories**: voice speed/stability, pause durations, ducking (speech/silence/attack/release), act volume multipliers, stem ducking (bass/drums/instruments), shorts params, music selection (energy weight, usage penalty, crossfade start), color harmonization (blend factor, contrast boost)

**Services:** Claude (Anthropic), ElevenLabs (TTS), Epidemic Sound (music/SFX/stems — MCP), fal.ai (images), Pexels (footage), Supabase (DB), YouTube API

## CI Pipeline (must pass before push)

```bash
ruff check --select E,F,W --ignore E501,E402 --exclude remotion .
cd remotion && npm run lint                    # eslint + tsc
python -m pytest tests/ -v --tb=short --cov=. --cov-report=term-missing --cov-fail-under=26
cd remotion && npm test                        # vitest
cd dashboard && npm test                       # vitest (local only)
```

Note: CI uses bare `python` (GitHub Actions), locally use `.venv/bin/python`. ~1,511 tests.

## Project Invariants (violating these = bugs)

1. **Anthropic SDK**: `output_config={"format":{"type":"json_schema","schema":...}}` — NOT `output_format`, NOT `response_format`
2. **Notifications**: All new notification code uses `server/notify.py` (`_tg()` + `_send()` dual-send). NEVER extend `server/notifier.py` (legacy, Discord-only).
3. **Dashboard types**: ALL type interfaces live in `dashboard/src/types.ts`. Never define types in component files or store.ts.
4. **Dashboard signals**: New signals must NOT be referenced in `systemState` computed (store.ts) unless they affect pipeline state.
5. **File I/O in server code**: Always `with open() as f:` context managers. Never bare `open()` with deque/readlines.
6. **Schema registry**: Agent 13 (Content Auditor) uses explicit per-call schemas, NOT the auto-lookup registry.
7. **Recovery calls**: `_call_raw()` args tuple must include ALL params that `_call_raw` accepts — silent schema loss otherwise.
8. **Thread safety**: `run_pipeline.py` image gen uses 3 ThreadPoolExecutor workers. Shared state must be read-only or locked.
9. **Import style**: One import per line for Python (ruff E401). Lazy imports in try/except for optional modules.
10. **Test coverage**: Minimum 26%. New signals/state must be covered in `resetAllSignals()` test.
11. **Config source of truth**: All tunable values come from `obsidian.yaml` via `core/config.py`. Never hardcode values that exist in config.
12. **Profile injection**: Style directives are injected via `intel/dna_loader.py:get_dna()`. Never modify agent files directly for style changes.
13. **Provider pattern**: New providers extend ABCs in `providers/base.py` and register in `providers/registry.py`. Never hardcode service calls.
14. **Epidemic Sound API key**: Expires every 30 days. `clients/epidemic_client.py` raises `KeyExpiredError` on 401/403. Pipeline falls back to local library. Key in `.env` only — NEVER commit to public repo.
15. **Music path format**: All paths in video-data.json relative to `remotion/public/` (e.g., `"music/filename.mp3"`, `"music/stems/bass.mp3"`).
16. **MCP protocol**: Epidemic Sound API uses JSON-RPC via single POST endpoint, NOT REST paths. Client uses `_call_tool("tool_name", {args})`.
17. **Series metadata**: Pass metadata dict with parent context via `add_topic(topic, source="series_auto", metadata={...})`. Topics table has `metadata JSONB`.
18. **Visual bible authority**: Image prompts use `art_style` from visual bible, NOT hardcoded `STYLE_FLUX`. `STYLE_FLUX` is fallback only when no visual bible exists.
19. **Intent system**: All scene-level rendering decisions flow through `media/scene_intent.py:resolve_all_scenes()`. Never compute transitions/energy/pacing outside the intent system.
20. **Silence beats**: When `intent_silence_beat=True`, ambient, SFX, captions, and ALL graphics overlays are suppressed. This is a hard constraint, not advisory.
21. **Atomic file writes**: `lessons_learned.json`, `run_history.json` use write-to-tmp + `os.replace()`. Never use bare `write_text()` for pipeline state files.
22. **Hook/cold open separation**: Cold open (3% of words) = pure sensation, no context. Hook (4%) = stakes escalation. They MUST serve different functions.
23. **Scene manifest**: `pipeline/convert.py` builds per-scene metadata manifest stored in Supabase `videos.scene_manifest` JSONB. Analytics agent uses it for retention correlation.
24. **Param count**: `PARAM_BOUNDS`, `PARAM_DEFAULTS`, and `PARAM_MIN_STEP` must all have the same number of entries (currently 37). Test enforces this.

## Patterns to Follow

- **Adding a notification**: Add to `server/notify.py`, call both `_tg()` and `_send()`, follow pattern of `notify_pipeline_complete()`
- **Adding a dashboard panel**: Type in `types.ts`, signal in `store.ts` (isolated), fetch in `api.ts`, component in view file
- **Adding an API endpoint**: In `webhook_server.py`, use `@require_key` decorator, return `jsonify()`
- **Adding structured output to an agent**: Add schema to `core/structured_schemas.py`, add to `SCHEMA_REGISTRY` (unless agent has multiple call patterns)
- **Adding a content profile**: Copy `profiles/_template.yaml`, fill all sections, set `profile:` in `obsidian.yaml`. Tests auto-validate.
- **Adding a provider**: Extend ABC from `providers/base.py`, register in `providers/registry.py` `_BUILTIN_PROVIDERS`, add tests in `tests/test_providers.py`
- **Adding a music/SFX feature**: Use `clients/epidemic_client.py` (`_call_tool`) for API. Music: `music_manager.py` → `epidemic_music_manager.py`. SFX: `epidemic_sfx_manager.py`. Fallback chain: API → cached → local.
- **Adding a music provider**: Extend `MusicProvider` from `providers/base.py`, register in `providers/registry.py`. Implement `search()`, `download()`, `select_for_video()`.
- **Changing config defaults**: Edit `obsidian.yaml`, constants in `core/pipeline_config.py` will pick it up automatically
- **Music analytics**: Compute in analytics agent → write to `channel_insights.json["music_performance"]` → read via `get_music_intelligence()` → inject into agents via dna_loader.
- **Tunable parameters**: Register in `PARAM_BOUNDS` + `PARAM_DEFAULTS` + `PARAM_MIN_STEP` in `core/param_overrides.py`. Pipeline reads via `get_override(key, default)`. Optimizer self-tunes from YouTube data. Update test count in `tests/test_param_overrides.py`.
- **Adding a scene-level feature**: Add field to scene schema in agent 07's prompt. If it drives rendering, add to `SceneIntent` resolution in `media/scene_intent.py`. Wire through `convert.py` to `video-data.json`. Add to TypeScript interface in `ObsidianVideo.tsx`. Add to scene manifest in `convert.py`. Add to `_compute_scene_retention_correlation()` in analytics agent.
- **Adding an era constraint**: Add entry to `ERA_CONSTRAINTS` in `pipeline/images.py` with keywords, year range, positive anchors, and negative prompts.
- **Scene retention analytics**: Correlation data written to `channel_insights.json["scene_retention_correlation"]`. Read via `get_scene_retention_intelligence()` in `intel/channel_insights.py`.

## Open Issues (19)

Contributors welcome! See https://github.com/harshithkantamneni/obsidian-engine/issues

**Good first issues**: Content profiles (gaming, cooking, science, podcast, news), image providers (DALL-E, Stability AI), TTS providers (Google Cloud), footage providers (Pixabay), upload providers (S3/R2), LLM providers (Ollama)

**Help wanted**: Multi-language support, Docker Compose, queue UI management, Whisper word timestamps for OpenAI TTS

**Open PR**: #7 — OpenAI TTS provider (needs rebase onto latest main)
