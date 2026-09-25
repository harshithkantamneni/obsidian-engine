"""Phase Setup — initialization, agent loading, credit checks, crash handlers."""

from __future__ import annotations

import os
import re
import sys
import json
import time
import atexit
import signal
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from core.log import get_logger
from core.paths import OUTPUT_DIR
from core.shutdown import _shutdown_event
from pipeline.loader import load_agent
from pipeline.state import load_state, save_state
from pipeline.helpers import _sanitize_topic

logger = get_logger(__name__)

if TYPE_CHECKING:
    from pipeline.context import PipelineContext


def _load_parent_context(ctx, series_meta: dict) -> None:
    """Load Part 1's research, script, and angle from its state file for series continuations."""
    parent_state_path = series_meta.get("parent_state_path", "")
    parent_state = None

    # Try direct path first
    if parent_state_path:
        p = Path(parent_state_path)
        if p.exists():
            try:
                with open(p) as f:
                    parent_state = json.load(f)
                logger.info(f"[Series] Loaded parent state from: {p.name}")
            except Exception as e:
                logger.warning(f"[Series] Could not load parent state file: {e}")

    # Fallback: find by parent topic slug
    if not parent_state:
        parent_topic = series_meta.get("parent_topic", "")
        if parent_topic:
            import re as _re
            parent_slug = _re.sub(r'[^a-z0-9]+', '_', parent_topic.lower())[:40]
            existing = sorted(
                OUTPUT_DIR.glob(f"{parent_slug}_*_state.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if existing:
                try:
                    with open(existing[0]) as f:
                        parent_state = json.load(f)
                    logger.info(f"[Series] Loaded parent state via slug match: {existing[0].name}")
                except Exception as e:
                    logger.warning(f"[Series] Could not load parent state by slug: {e}")

    if parent_state:
        # Extract visual bible from parent's stage 7 (scene breakdown + visual continuity)
        parent_stage_7 = parent_state.get("stage_7", {})
        parent_visual_bible = None
        if isinstance(parent_stage_7, dict):
            parent_visual_bible = parent_stage_7.get("visual_bible")
        ctx.parent_context = {
            "research": parent_state.get("stage_1"),
            "angle": parent_state.get("stage_2"),
            "script": parent_state.get("stage_4"),
            "blueprint": parent_state.get("stage_3"),
            "series_plan": parent_state.get("series_plan"),
            "visual_bible": parent_visual_bible,
        }
        part_num = series_meta.get("series_part", 2)
        logger.info(f"[Series] Part {part_num} has parent context: "
                     f"research={'yes' if ctx.parent_context.get('research') else 'no'}, "
                     f"script={'yes' if ctx.parent_context.get('script') else 'no'}")
    else:
        logger.warning("[Series] Could not find parent state — Part will run without prior context")


def init_context(topic: str, resume: bool, from_stage: int, is_experiment: bool,
                  series_meta: dict | None = None):
    """Build a PipelineContext from CLI args. Replaces lines 76-153 of run_pipeline()."""
    from pipeline.context import PipelineContext

    topic = _sanitize_topic(topic)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r'[^a-z0-9]+', '_', topic.lower())[:40]

    ctx = PipelineContext(
        topic=topic,
        slug=slug,
        ts=ts,
        resume=resume,
        from_stage=from_stage,
        is_experiment=is_experiment,
    )
    ctx.state_path = OUTPUT_DIR / f"{slug}_{ctx.run_id}_state.json"

    # Series continuation: load Part 1 context if this is Part 2+
    ctx.series_meta = series_meta
    # Auto-load series metadata from Supabase if not passed via CLI
    if not ctx.series_meta:
        try:
            from clients.supabase_client import get_client
            _sc = get_client()
            _topic_row = _sc.table("topics").select("source, metadata")\
                .eq("topic", topic).limit(1).execute()
            if _topic_row.data and _topic_row.data[0].get("source") == "series_auto":
                _meta = _topic_row.data[0].get("metadata") or {}
                if _meta.get("series_part", 1) > 1:
                    ctx.series_meta = _meta
                    logger.info(f"[Pipeline] Auto-loaded series metadata from Supabase: Part {_meta.get('series_part')}")
        except Exception as _e:
            logger.debug(f"[Pipeline] Series metadata auto-load failed (non-fatal): {_e}")
    # Also detect from topic name as final fallback
    if not ctx.series_meta:
        import re as _re
        _part_match = _re.search(r'\(Part (\d+)', topic)
        if _part_match and int(_part_match.group(1)) > 1:
            ctx.series_meta = {"series_part": int(_part_match.group(1)), "parent_topic": topic.split("(Part")[0].strip()}
            logger.info(f"[Pipeline] Inferred series metadata from topic name: Part {ctx.series_meta['series_part']}")
    if ctx.series_meta and ctx.series_meta.get("series_part", 1) > 1:
        _load_parent_context(ctx, ctx.series_meta)

    # Resume: find most recent existing state file for this topic
    if resume:
        existing = sorted(
            OUTPUT_DIR.glob(f"{slug}_*_state.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if existing:
            ctx.state_path = existing[0]
            logger.info(f"[Pipeline] Resuming from: {ctx.state_path.name}")

    # Reset session costs
    try:
        from clients.claude_client import reset_session_costs
        reset_session_costs()
    except Exception:
        pass

    # Reset profile cache (load fresh for this run)
    try:
        from core.profile import reset_profile_cache, get_profile
        reset_profile_cache()
        profile = get_profile()
        logger.info(f"[Pipeline] Active profile: {profile.get('name', 'default')}")
    except Exception:
        pass

    # Load parameter overrides (frozen for entire run)
    try:
        from core.param_overrides import reset_pipeline_cache, load_overrides_for_pipeline
        reset_pipeline_cache()
        load_overrides_for_pipeline()
    except Exception as e:
        logger.warning(f"[Pipeline] parameter overrides unavailable, using defaults: {e}")

    # Check for optimizer exploration perturbation
    try:
        from core.param_history import load_optimizer_state, is_optimizer_enabled
        if is_optimizer_enabled():
            opt_state = load_optimizer_state()
            if opt_state and opt_state.get("pending_exploration"):
                exploration = opt_state["pending_exploration"]
                from core.param_overrides import save_override
                for param_key, value in exploration.items():
                    save_override(param_key, value, approved_by="optimizer_explore")
                logger.info(f"[Pipeline] Applied optimizer exploration: {list(exploration.keys())}")
                opt_state["pending_exploration"] = {}
                from core.param_history import save_optimizer_state
                save_optimizer_state(opt_state)
                from core.param_overrides import reset_pipeline_cache as _rpc, load_overrides_for_pipeline as _lop
                _rpc()
                _lop()
    except Exception as exp_err:
        logger.warning(f"[Pipeline] Optimizer exploration check (non-fatal): {exp_err}")

    # Load or init state
    if resume or from_stage > 1:
        ctx.state = load_state(ctx.state_path)
    else:
        ctx.state = {}
    ctx.state.setdefault("topic", topic)
    ctx.state.setdefault("completed_stages", [])
    ctx.state.setdefault("completed_short_stages", [])

    # Classify era
    try:
        from intel.era_classifier import classify_era
        ctx.state.setdefault("era", classify_era(topic))
    except Exception:
        ctx.state.setdefault("era", "other")
    save_state(ctx.state, ctx.state_path)

    # Cost budget
    try:
        from core.pipeline_config import COST_BUDGET_MAX_USD
        ctx.budget_cap = COST_BUDGET_MAX_USD
    except ImportError:
        ctx.budget_cap = 0

    # Pipeline timing — set before cost tracking to match original placement
    ctx.start_time = time.time()

    # Cost tracking
    ctx.cost_run_id = f"{ts}_{slug}"
    try:
        from core import cost_tracker
        cost_tracker.start_run(topic, ctx.cost_run_id)
        logger.info(f"[Cost] Tracking started: {ctx.cost_run_id}")
        ctx.cost_tracker = cost_tracker
    except Exception as _ce:
        logger.warning(f"[Cost] Tracking unavailable: {_ce}")
        ctx.cost_tracker = None

    return ctx


def register_crash_handlers(ctx: PipelineContext) -> None:
    """Register atexit + SIGTERM handlers. Replaces lines 134-146."""

    def _emergency_save():
        try:
            save_state(ctx.state, ctx.state_path)
        except Exception:
            pass

    atexit.register(_emergency_save)

    def _sigterm_handler(signum, frame):
        logger.warning("[Pipeline] SIGTERM received — initiating graceful shutdown...")
        _shutdown_event.set()
        _emergency_save()
        time.sleep(5)
        sys.exit(1)

    signal.signal(signal.SIGTERM, _sigterm_handler)


def load_agents(ctx: PipelineContext) -> None:
    """Load all agent modules into ctx.agents. Replaces lines 327-355."""
    agents = ctx.agents

    agents["a01"] = load_agent(Path("01_research_agent.py"))
    agents["a02"] = load_agent(Path("02_originality_agent.py"))
    agents["a03"] = load_agent(Path("03_narrative_architect.py"))
    agents["a04"] = load_agent(Path("04_script_writer.py"))
    agents["a05"] = load_agent(Path("05_fact_verification_agent.py"))
    agents["a06"] = load_agent(Path("06_seo_agent.py"))
    agents["a07"] = load_agent(Path("07_scene_breakdown_agent.py"))

    # Optional agents
    try:
        agents["a04b"] = load_agent(Path("04b_script_doctor.py"))
    except FileNotFoundError:
        agents["a04b"] = None
    try:
        agents["a07b"] = load_agent(Path("07b_visual_continuity.py"))
    except FileNotFoundError:
        agents["a07b"] = None

    agents["a09"] = load_agent(Path("09_footage_hunter.py"))
    agents["a11"] = load_agent(Path("11_youtube_uploader.py"))

    try:
        agents["a_short_script"] = load_agent(Path("short_script_agent.py"))
        agents["a_short_storyboard"] = load_agent(Path("short_storyboard_agent.py"))
    except FileNotFoundError as e:
        logger.warning(f"[Pipeline] \u26a0\ufe0f  Short agents not found — shorts disabled: {e}")
        agents["a_short_script"] = None
        agents["a_short_storyboard"] = None

    try:
        agents["a_tts_format"] = load_agent(Path("tts_format_agent.py"))
    except FileNotFoundError:
        agents["a_tts_format"] = None


def run_credit_checks(ctx: PipelineContext) -> None:
    """LLM (Anthropic only) + TTS provider credit gates."""
    from providers.registry import get_provider_name

    try:
        _llm_name = get_provider_name("llm")
    except Exception:
        _llm_name = "anthropic"
    if _llm_name == "anthropic":
        _check_anthropic_credits()
    else:
        logger.info(f"[LLM] Provider '{_llm_name}' — skipping Anthropic credit check")
    _check_tts_credits()


def _check_anthropic_credits() -> None:
    try:
        import anthropic as _anth
        _test_client = _anth.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        _test_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=5,
            messages=[{"role": "user", "content": "ping"}],
        )
        logger.info("[Anthropic] API credit check: OK")
    except _anth.BadRequestError as e:
        if "credit balance" in str(e).lower():
            raise Exception(
                "Anthropic API credits exhausted. "
                "Top up at console.anthropic.com → Plans & Billing."
            )
        raise
    except Exception as e:
        if "credit balance" in str(e).lower():
            raise Exception(
                "Anthropic API credits exhausted. "
                "Top up at console.anthropic.com → Plans & Billing."
            )
        logger.warning(f"[Anthropic] Credit check warning: {e}")


def _check_tts_credits() -> None:
    """Ask the configured TTS provider for its quota (generic check_credits()).

    Providers raise on an invalid key (e.g. ElevenLabs 401) — that aborts the
    run early. Anything else is a warning. limit <= 0 means "unknown / no
    quota endpoint" and is not logged.
    """
    label = "TTS"
    try:
        from providers.registry import get_provider
        tts = get_provider("tts")
        label = getattr(tts, "name", "TTS")
        credits = tts.check_credits() or {}
        limit = credits.get("limit", 0) or 0
        remaining = credits.get("remaining", 0) or 0
        if limit > 0:
            unit = credits.get("unit", "characters")
            unit = "chars" if unit == "characters" else unit
            used_pct = ((limit - remaining) / limit) * 100
            logger.info(
                f"[{label}] Credits: {remaining:,} {unit} remaining ({used_pct:.0f}% used)"
                + (" — overage active" if remaining < 0 else "")
            )
    except Exception as e:
        if "401" in str(e):
            raise
        logger.warning(f"[{label}] Credit check warning: {e}")


def run_topic_dedup(ctx: PipelineContext) -> None:
    """Topic deduplication check. Non-fatal. Replaces lines 317-324."""
    if ctx.series_meta and ctx.series_meta.get("series_part", 1) > 1:
        logger.info("[Pipeline] Skipping dedup check — series continuation")
        return
    if not ctx.resume:
        try:
            from server import topic_store
            is_dup, matched = topic_store.is_duplicate(ctx.topic)
            if is_dup:
                logger.warning(f"[Pipeline] \u26a0\ufe0f  Topic may be duplicate of: '{matched}' — continuing anyway")
        except Exception:
            pass


def notify_start(ctx: PipelineContext) -> None:
    """Send pipeline start notification. Replaces lines 261-265."""
    logger.info(f"\n{'='*60}")
    logger.info("  THE OBSIDIAN ARCHIVE — MASTER PIPELINE")
    logger.info(f"  Topic: {ctx.topic}")
    logger.info(f"{'='*60}\n")

    try:
        from server import notify
        notify.notify_pipeline_start(ctx.topic)
    except Exception:
        pass
