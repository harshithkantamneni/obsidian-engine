#!/usr/bin/env python3
"""
The Obsidian Archive — Master Pipeline
Usage: python3 run_pipeline.py "Your topic here"
       python3 run_pipeline.py "Your topic here" --resume   (skip completed stages)
       python3 run_pipeline.py "Your topic here" --from-stage 7  (start from specific stage)

Stages:
  1  Research
  2  Originality
  3  Narrative
  4  Script
  5  Verification
  6  SEO
  7  Scene Breakdown
  8  Audio (chunked, mutagen offsets)
  9  Footage
  10 Image Generation (fal.ai)
  11 Remotion Data Conversion
  12 Video Render
"""

from __future__ import annotations
import sys
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", interpolate=False)

# Allow OAuth over HTTP on Railway (internal proxy handles HTTPS externally)
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# ── Lenient JSON parsing ──────────────────────────────────────────────────────
from core.json_compat import apply_lenient_json
apply_lenient_json()

# ── Shared paths and directories ──────────────────────────────────────────────
from core.paths import ensure_dirs
ensure_dirs()

# ── Re-exports for test compatibility ─────────────────────────────────────────
# Tests do `run_pipeline.load_state(...)`, `run_pipeline.check_research(...)`, etc.
# These must remain importable from this module.
from pipeline.loader import load_agent  # noqa: F401
from pipeline.state import load_state, save_state  # noqa: F401
from pipeline.validators import (  # noqa: F401
    validate_stage_output, check_research, check_angle, check_blueprint,
    check_script, check_pacing, check_verification, check_seo, check_scenes,
    check_audio,
)
from pipeline.helpers import score_hook, clean_script, _sanitize_topic, _cleanup_after_upload  # noqa: F401
from pipeline.series import (  # noqa: F401
    detect_series_potential, queue_series_part2,
    get_retention_optimal_length,
)
from pipeline.audio import run_audio  # noqa: F401
from pipeline.images import run_images  # noqa: F401
from pipeline.convert import run_convert  # noqa: F401
from pipeline.shorts import run_short_audio, run_short_images, run_short_convert, run_short_render  # noqa: F401
from pipeline.render import run_render  # noqa: F401

# ── Phase imports ─────────────────────────────────────────────────────────────
from pipeline.phase_setup import (
    init_context, load_agents, register_crash_handlers,
    run_credit_checks, run_topic_dedup, notify_start,
)
from pipeline.phase_script import run_script_phase
from pipeline.phase_prod import run_production_phase
from pipeline.phase_post import run_post_phase
from pipeline.runner import StageRunner


def _validate_topic(topic: str) -> None:
    """Reject obviously invalid topics before pipeline runs.

    Validates at pipeline entry -- not at scheduler queue time -- to catch
    both scheduled and manual invocations.
    """
    if not topic or not topic.strip():
        raise ValueError("[Pipeline] Topic is empty or whitespace-only. Aborting.")
    stripped = topic.strip()
    if stripped.upper().startswith("ERROR"):
        raise ValueError(f"[Pipeline] Topic looks like an error message: '{stripped}'. Aborting.")
    if stripped.startswith("-") or stripped.startswith("--"):
        raise ValueError(f"[Pipeline] Topic looks like a CLI flag: '{stripped}'. Aborting.")
    if len(stripped) < 5:
        raise ValueError(f"[Pipeline] Topic too short ({len(stripped)} chars): '{stripped}'. Aborting.")
    if "no topic submitted" in stripped.lower():
        raise ValueError(f"[Pipeline] Topic contains 'no topic submitted': '{stripped}'. Aborting.")


def run_pipeline(topic, resume=False, from_stage=1, is_experiment=False, series_meta=None):
    _validate_topic(topic)
    ctx = init_context(topic, resume, from_stage, is_experiment, series_meta=series_meta)
    register_crash_handlers(ctx)
    notify_start(ctx)

    run_credit_checks(ctx)
    run_topic_dedup(ctx)
    load_agents(ctx)

    runner = StageRunner(ctx)

    run_script_phase(ctx, runner)
    shorts_future, shorts_executor = run_production_phase(ctx, runner)
    run_post_phase(ctx, runner, shorts_future, shorts_executor)

    return ctx.state


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print("""
The Obsidian Archive — Master Pipeline

Usage:
  python3 run_pipeline.py "The Poisoning of Emperor Claudius"
  python3 run_pipeline.py "topic" --resume          # resume from last checkpoint
  python3 run_pipeline.py "topic" --from-stage 7    # start from specific stage

Stages:
  1  Research          6  SEO
  2  Originality       7  Scene Breakdown
  3  Narrative         8  Audio (ElevenLabs)
  4  Script            9  Footage Hunting
  5  Verification     10  Image Generation (fal.ai)
                      11  Remotion Conversion
                      12  Video Render

Cost per video: ~$0.50-1.00
Time per video: ~25-40 minutes
""")
        sys.exit(0)

    topic         = sys.argv[1]
    resume        = "--resume" in sys.argv
    is_experiment = "--experiment" in sys.argv
    from_stage    = 1
    series_meta   = None
    if "--from-stage" in sys.argv:
        resume = True  # --from-stage requires prior state, auto-enable resume
        idx = sys.argv.index("--from-stage")
        try:
            from_stage = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("Error: --from-stage requires a numeric value (e.g. --from-stage 5)")
            sys.exit(1)
    if "--series-meta" in sys.argv:
        idx = sys.argv.index("--series-meta")
        try:
            import json as _json
            series_meta = _json.loads(Path(sys.argv[idx + 1]).read_text())
            print(f"[Pipeline] Series metadata loaded: Part {series_meta.get('series_part')}")
        except Exception as _e:
            print(f"[Pipeline] Warning: could not load series metadata: {_e}")

    try:
        result = run_pipeline(topic, resume=resume, from_stage=from_stage,
                              is_experiment=is_experiment, series_meta=series_meta)
        sys.exit(0 if result.get("pipeline_status") == "COMPLETE" else 1)
    except Exception as _e:
        _msg = str(_e).lower()
        if "credits too low" in _msg or "401" in str(_e) or "credit balance" in _msg or "credits exhausted" in _msg:
            try:
                from server.notify import _tg
                _tg(f"\u26d4 *Pipeline Blocked \u2014 API Credits Exhausted*\n`{_e}`")
            except Exception:
                pass
            sys.exit(2)  # infrastructure failure — topic should be re-queued
        # War Room v5: notify on ALL pipeline failures, not just credits
        try:
            from server.notify import notify_pipeline_failed
            notify_pipeline_failed(topic, stage="unknown", error=str(_e)[:500])
        except Exception:
            pass
        raise
