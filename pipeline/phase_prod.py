"""Phase Production — Waves 1-3 (parallel DAGs), QA tiers, rendering."""

from __future__ import annotations

import json
import glob
import atexit
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING

from core.paths import OUTPUT_DIR, MEDIA_DIR, REMOTION_PUBLIC
from pipeline.validators import check_seo, check_scenes, check_audio
from pipeline.helpers import clean_script
from pipeline.audio import run_audio
from pipeline.images import run_images
from pipeline.convert import run_convert
from pipeline.shorts import run_short_audio, run_short_images, run_short_convert, run_short_render
from pipeline.render import run_render
from core.log import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from pipeline.context import PipelineContext
    from pipeline.runner import StageRunner


def _check_and_warn(issues, stage_name):
    if issues:
        logger.warning(f"\n\u26a0\ufe0f  {stage_name} quality issues:")
        for issue in issues:
            logger.warning(f"   - {issue}")


def run_production_phase(ctx: PipelineContext, runner: StageRunner) -> tuple:
    """Execute Waves 1-3, QA tiers, rendering. Returns (shorts_future, shorts_executor)."""

    # ── Launch Shorts in background ──────────────────────────────────────────
    shorts_future, shorts_executor = _launch_shorts(ctx, runner)

    # ══════════════════════════════════════════════════════════════════════════
    # Wave 1: Compliance, then SEO(6) + Scenes(7) in parallel
    # ══════════════════════════════════════════════════════════════════════════
    _run_wave1(ctx, runner)

    # ── Wave 2: Visual Continuity + TTS Formatting ───────────────────────────
    _run_wave2(ctx, runner)

    # ══════════════════════════════════════════════════════════════════════════
    # Wave 3: Audio(8) + Footage(9) + Thumbnail in parallel
    # ══════════════════════════════════════════════════════════════════════════
    _run_wave3(ctx, runner)

    # ── Build media manifest ─────────────────────────────────────────────────
    _build_manifest(ctx, runner)

    # ── Stage 10: Image Generation ───────────────────────────────────────────
    ctx.manifest = runner.run_stage(10, "Image Generation", run_images, ctx.manifest) or ctx.manifest
    with open(MEDIA_DIR / "media_manifest.json", "w") as f:
        json.dump(ctx.manifest, f, indent=2)

    # ── Phase 3: Video rendering ─────────────────────────────────────────────
    _run_render_phase(ctx, runner)

    # ── QA tiers ─────────────────────────────────────────────────────────────
    _run_qa_tiers(ctx, runner)

    return shorts_future, shorts_executor


# ══════════════════════════════════════════════════════════════════════════════
# Shorts Pipeline
# ══════════════════════════════════════════════════════════════════════════════

def _launch_shorts(ctx: PipelineContext, runner: StageRunner) -> tuple:
    """Launch shorts pipeline in background thread."""

    def _run_shorts_pipeline():
        return _shorts_pipeline_impl(ctx, runner)

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shorts_bg")
    atexit.register(lambda: executor.shutdown(wait=False, cancel_futures=True))
    future = executor.submit(_run_shorts_pipeline)
    return future, executor


def _shorts_pipeline_impl(ctx: PipelineContext, runner: StageRunner):
    """Shorts pipeline — fully self-contained, non-fatal."""
    a_short_script = ctx.agents.get("a_short_script")
    a_short_storyboard = ctx.agents.get("a_short_storyboard")
    a11 = ctx.agents["a11"]
    _short_upload_result = None
    _short_script = None

    try:
        if a_short_script is None or a_short_storyboard is None:
            raise FileNotFoundError("Short agent files not found \u2014 shorts disabled")

        logger.info(f"\n{'='*60}")
        logger.info("  SHORTS PIPELINE \u2014 STARTING")
        logger.info(f"{'='*60}")

        _short_script = runner.run_short_stage(
            "short_script", "Short Script",
            a_short_script.run,
            ctx.research or ctx.state.get("stage_1", {}),
            ctx.angle or ctx.state.get("stage_2", {}),
        )

        short_storyboard = runner.run_short_stage(
            "short_storyboard", "Short Storyboard",
            a_short_storyboard.run,
            _short_script or ctx.state.get("stage_short_script", {}),
        )

        short_audio = runner.run_short_stage(
            "short_audio", "Short Audio",
            run_short_audio,
            _short_script or ctx.state.get("stage_short_script", {}),
        )

        short_storyboard_with_images = runner.run_short_stage(
            "short_images", "Short Images",
            run_short_images,
            short_storyboard or ctx.state.get("stage_short_storyboard", {}),
        )

        runner.run_short_stage(
            "short_convert", "Short Convert",
            run_short_convert,
            short_storyboard_with_images or ctx.state.get("stage_short_images", {}),
            short_audio or ctx.state.get("stage_short_audio", {}),
        )

        runner.run_short_stage(
            "short_render", "Short Render",
            run_short_render,
            ctx.topic,
        )

        def do_short_upload():
            sc = _short_script or ctx.state.get("stage_short_script", {})
            short_videos = sorted(glob.glob(str(OUTPUT_DIR / "*_SHORT.mp4")))
            if not short_videos:
                raise FileNotFoundError("No SHORT.mp4 found \u2014 run Short Render first.")
            video_path = short_videos[-1]
            _title = sc.get("short_title", f"{ctx.topic} #Shorts")
            _tags = sc.get("short_tags", [])
            _description = sc.get("short_description", "")
            thumb = None
            for img_name in ["short_scene_00_ai.jpg", "short_scene_01_ai.jpg"]:
                candidate = REMOTION_PUBLIC / img_name
                if candidate.exists():
                    thumb = str(candidate)
                    break
            from providers.registry import get_provider_name
            if get_provider_name("upload") != "youtube":
                return a11.upload_with_provider(video_path, _title, _description, _tags, thumb)
            return a11.upload_video(video_path, _title, _description, _tags,
                                    thumbnail_path=thumb, privacy=a11._youtube_privacy("public"))

        _short_upload_result = runner.run_short_stage("short_upload", "Short Upload", do_short_upload)

        logger.info(f"\n{'='*60}")
        logger.info("  SHORTS PIPELINE \u2014 COMPLETE")
        logger.info(f"{'='*60}")

        if _short_upload_result:
            _save_short_to_supabase(ctx, _short_script, _short_upload_result)
            try:
                from server import notify
                sc = _short_script or ctx.state.get("stage_short_script", {})
                notify.notify_short_complete(
                    sc.get("short_title", ctx.topic),
                    _short_upload_result.get("url", "")
                )
            except Exception:
                pass

    except Exception as short_err:
        logger.warning(f"\n[Pipeline] \u26a0\ufe0f  SHORT PIPELINE FAILED: {short_err}", exc_info=True)
        logger.info("[Pipeline] Continuing with long video pipeline...")
        try:
            from server import notify
            notify.notify_pipeline_failed(ctx.topic, "Shorts Pipeline", str(short_err))
        except Exception:
            pass

    return _short_upload_result, _short_script


def _save_short_to_supabase(ctx, _short_script, _short_upload_result):
    """Save short video record to Supabase."""
    try:
        from clients import supabase_client
        sc = _short_script or ctx.state.get("stage_short_script", {})
        short_audio_data = ctx.state.get("stage_short_audio", {})
        supabase_client.save_video(
            topic=ctx.topic,
            title=sc.get("short_title", f"{ctx.topic} #Shorts"),
            youtube_url=_short_upload_result.get("url", ""),
            youtube_id=_short_upload_result.get("video_id", ""),
            script_path="",
            video_path="",
            duration_seconds=short_audio_data.get("total_duration_seconds", 50) if isinstance(short_audio_data, dict) else 50,
            word_count=sc.get("word_count", 130) if isinstance(sc, dict) else 130,
            pipeline_state={
                "is_short": True, "era": ctx.state.get("era", "other"),
                "parent_topic": ctx.topic,
                "parent_youtube_id": (ctx.state.get("stage_13") or {}).get("video_id", ""),
                "youtube_upload_date": datetime.now().strftime("%Y-%m-%d"),
                "production_params": {
                    "voice_speed": short_audio_data.get("production_params", {}).get("voice_speed", 0.88),
                    "hook_speed": short_audio_data.get("production_params", {}).get("hook_speed", 0.92),
                    "voice_stability": short_audio_data.get("production_params", {}).get("voice_stability", 0.38),
                    "voice_style": short_audio_data.get("production_params", {}).get("voice_style", 0.60),
                    "similarity_boost": short_audio_data.get("production_params", {}).get("similarity_boost", 0.82),
                    "hook_stability": short_audio_data.get("production_params", {}).get("hook_stability", 0.28),
                    "hook_style": short_audio_data.get("production_params", {}).get("hook_style", 0.75),
                    "hook_similarity_boost": short_audio_data.get("production_params", {}).get("hook_similarity_boost", 0.85),
                    "tail_buffer_sec": short_audio_data.get("production_params", {}).get("tail_buffer_sec", 1.5),
                },
            },
        )
        logger.info("[Pipeline] \u2713 Short saved to Supabase")
    except Exception as e:
        logger.warning(f"[Pipeline] Short save to Supabase failed (non-critical): {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Wave 1: SEO + Scenes + Compliance
# ══════════════════════════════════════════════════════════════════════════════

def _run_compliance(ctx: PipelineContext, runner: StageRunner):
    """Compliance check — FIXED: uses mark_metadata for thread-safe state writes."""
    if not ctx.script or runner.done(8):
        return None
    try:
        from core import compliance_checker
        compliance = compliance_checker.run(ctx.script, ctx.topic)
        risk = compliance.get("risk_level", "green")
        flags = compliance.get("flags", [])
        if risk == "red":
            logger.warning(f"[Compliance] RED \u2014 {len(flags)} demonetization risks found")
            safe_script = compliance.get("safe_script", "")
            if safe_script and len(safe_script.split()) > 500:
                logger.info("[Compliance] Safe alternatives applied to script")
                return {"safe_script": safe_script, "risk": risk, "flags": flags}
            else:
                logger.warning("[Compliance] \u26a0\ufe0f  Could not auto-fix \u2014 review flags manually")
                for flag in flags[:5]:
                    logger.warning(f"  - [{flag.get('severity','?')}] {flag.get('category','')}: {flag.get('text_excerpt','')[:80]}")
        elif risk == "yellow":
            logger.warning(f"[Compliance] YELLOW \u2014 {len(flags)} minor flags (continuing)")
            for flag in flags[:3]:
                logger.warning(f"  - {flag.get('category','')}: {flag.get('suggestion','')[:80]}")
        else:
            logger.info("[Compliance] GREEN \u2014 script is monetization-safe \u2713")
        # FIX: use mark_metadata for thread-safe write (was bare state[] + save_state without lock)
        runner.mark_metadata("compliance", {"risk_level": risk, "flag_count": len(flags)})
        return {"risk": risk, "flags": flags}
    except Exception as _comp_err:
        logger.warning(f"[Compliance] Check skipped: {_comp_err}")
        return None


def _apply_compliance(ctx: PipelineContext, runner: StageRunner, compliance_result) -> None:
    """Apply compliance result to ctx.script, enforce the RED gate, persist.

    Must run BEFORE Scene Breakdown (stage 7): scene-aware audio narrates each
    scene's ``narration`` field from stage 7, so the safe wording has to be in
    ctx.script before stage 7 splits it into scenes.
    """
    # Fatal gate: RED compliance with no auto-fix
    if compliance_result and compliance_result.get("risk") == "red":
        if not compliance_result.get("safe_script"):
            raise RuntimeError(
                f"Compliance gate FAILED: {len(compliance_result.get('flags', []))} RED flags "
                "could not be auto-fixed. Review flagged content before proceeding."
            )

    # Apply compliance modifications to script (must happen before Scenes + TTS)
    if compliance_result and compliance_result.get("safe_script") and ctx.script:
        ctx.script["full_script"] = compliance_result["safe_script"]
        # Persist the patched script back into stage_4 so a resume (which
        # reloads ctx.script from state["stage_4"]) never sees the unpatched text.
        runner.mark(4, ctx.script)
        runner.mark_metadata("compliance", {
            "risk_level": compliance_result.get("risk", "red"),
            "flag_count": len(compliance_result.get("flags", [])),
            "safe_script_applied": True,
        })
        logger.info("[Compliance] Patched script persisted to stage_4 before Scene Breakdown")


def _run_wave1(ctx: PipelineContext, runner: StageRunner) -> None:
    """Wave 1: Compliance first (patches script), then SEO(6) + Scenes(7) in parallel."""
    a06 = ctx.agents["a06"]
    a07 = ctx.agents["a07"]

    # Compliance runs first (single LLM call) so SEO/Scenes see the safe script.
    logger.info("\n[Pipeline] \u2500\u2500 Wave 1a: Compliance \u2500\u2500")
    compliance_result = _run_compliance(ctx, runner)
    _apply_compliance(ctx, runner, compliance_result)

    logger.info("\n[Pipeline] \u2500\u2500 Parallel DAG Wave 1b: SEO + Scenes \u2500\u2500")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="dag_w1") as dag_pool:
        seo_future = dag_pool.submit(runner.run_stage, 6, "SEO", a06.run, ctx.script, ctx.verification, ctx.angle)
        scenes_future = dag_pool.submit(runner.run_stage, 7, "Scene Breakdown", a07.run, ctx.script, ctx.verification)

        ctx.seo = seo_future.result()
        ctx.scenes_data = scenes_future.result()

    _check_and_warn(check_seo(ctx.seo or {}), "SEO")
    _check_and_warn(check_scenes(ctx.scenes_data or {}), "Scenes")

    # SEO post-processing
    if ctx.seo and ctx.series_plan:
        suffix = ctx.series_plan.get("part_1_title_suffix", "(Part 1)")
        title = ctx.seo.get("recommended_title", "")
        if title and "Part" not in title:
            ctx.seo["recommended_title"] = f"{title} {suffix}"
            logger.info(f"[Series] Title updated: {ctx.seo['recommended_title'][:70]}")

    if ctx.seo and not ctx.state.get("seo_title_b"):
        variants = ctx.seo.get("title_variants", [])
        if isinstance(variants, list) and len(variants) > 1:
            runner.mark_metadata("seo_title_b",
                variants[1].get("title", "") if isinstance(variants[1], dict) else str(variants[1]))
            logger.info(f"[Pipeline] A/B title B stored: {ctx.state['seo_title_b'][:60]}")

    if ctx.script and not ctx.state.get("script_path"):
        final_script_path = OUTPUT_DIR / f"{ctx.ts}_{ctx.slug}_FINAL_SCRIPT.txt"
        with open(final_script_path, "w") as f:
            f.write(f"THE OBSIDIAN ARCHIVE\n{'='*60}\n")
            f.write(f"Title: {ctx.seo.get('recommended_title', ctx.topic) if ctx.seo else ctx.topic}\n\n")
            f.write(ctx.script.get("full_script", ""))
        logger.info(f"[Pipeline] Script saved: {final_script_path.name}")
        runner.mark_metadata("script_path", str(final_script_path))


# ══════════════════════════════════════════════════════════════════════════════
# Wave 2: Visual Continuity + TTS Formatting
# ══════════════════════════════════════════════════════════════════════════════

def _run_wave2(ctx: PipelineContext, runner: StageRunner) -> None:
    """Visual Continuity + TTS formatting (sequential, after Wave 1)."""
    a07b = ctx.agents.get("a07b")
    a_tts_format = ctx.agents.get("a_tts_format")

    # Visual Continuity (pass parent visual bible for series continuity)
    if a07b and ctx.scenes_data:
        try:
            parent_bible = None
            if ctx.parent_context and ctx.parent_context.get("visual_bible"):
                parent_bible = ctx.parent_context["visual_bible"]
                logger.info("[Pipeline] Series continuity: injecting parent visual bible into 07b")
            enhanced = a07b.run(ctx.scenes_data, parent_visual_bible=parent_bible)
            if enhanced and enhanced.get("scenes"):
                ctx.scenes_data = enhanced
                runner.mark(7, ctx.scenes_data)
                logger.info(f"[Pipeline] Visual Continuity: visual bible created, {len(enhanced.get('scenes', []))} scenes enhanced")
        except Exception as vc_err:
            logger.warning(f"[Pipeline] Visual Continuity failed (non-fatal): {vc_err}")

    # TTS Formatting — produces two versions:
    # 1. tts_script: phonetic respellings for ElevenLabs audio generation
    # 2. display_script: original spellings for captions/word_timestamps
    ctx.tts_script = ctx.script
    ctx.display_script = ctx.script
    if ctx.script and not runner.done(8):
        if a_tts_format:
            try:
                formatted = a_tts_format.run(ctx.script)
                if formatted and formatted.get("full_script"):
                    # Display text uses full_script with any phonetic respellings
                    # reverted (original spellings for captions). Respellings are
                    # single whitespace-free tokens, so word counts stay aligned
                    # with the TTS text and audio.py's positional caption override
                    # can map respelled words back to the original spelling.
                    _display_text = _undo_respellings(
                        formatted["full_script"], _pronunciation_map(a_tts_format))
                    ctx.display_script = {**ctx.script, "full_script": _display_text}
                    # TTS text uses tts_script if available (phonetic respellings),
                    # falls back to full_script (no pronunciation changes)
                    tts_text = formatted.get("tts_script", formatted["full_script"])
                    ctx.tts_script = {**ctx.script, "full_script": tts_text}
                    changes = formatted.get("changes_made", [])
                    logger.info(f"[TTS Format] \u2713 {len(changes)} changes applied for spoken delivery")
                else:
                    logger.warning("[TTS Format] Warning: empty output \u2014 using clean_script fallback")
            except Exception as fmt_err:
                logger.warning(f"[TTS Format] Warning: {fmt_err} \u2014 using clean_script fallback")
        ctx.tts_script = {**ctx.tts_script, "full_script": clean_script(ctx.tts_script.get("full_script", ""))}
        ctx.display_script = {**ctx.display_script, "full_script": clean_script(ctx.display_script.get("full_script", ""))}


# ══════════════════════════════════════════════════════════════════════════════
# Wave 3: Audio + Footage + Thumbnail
# ══════════════════════════════════════════════════════════════════════════════

def _pronunciation_map(a_tts_format) -> dict:
    """Original term -> phonetic respelling, from the loaded TTS format agent.

    Only whitespace-free respellings are kept so a respelling never changes
    the word count (captions are aligned positionally).
    """
    pmap = getattr(a_tts_format, "PRONUNCIATION_MAP", None) if a_tts_format else None
    if not isinstance(pmap, dict):
        return {}
    return {
        k: v for k, v in pmap.items()
        if isinstance(k, str) and isinstance(v, str) and k and v
        and not any(c.isspace() for c in k + v)
    }


def _undo_respellings(text: str, pron_map: dict) -> str:
    """Revert phonetic respellings in ``text`` back to the original terms."""
    if not text or not pron_map:
        return text
    for term, respelling in sorted(pron_map.items(), key=lambda kv: -len(kv[1])):
        text = text.replace(respelling, term)
    return text


def _respell_scenes_for_audio(scenes_data, tts_script, pron_map: dict):
    """Return a copy of scenes_data whose ``narration`` carries the same
    phonetic respellings the TTS format agent applied to the TTS script.

    Scene-aware audio (>=3 narrated scenes) speaks scene["narration"], not the
    TTS script, so without this the respellings only reached the legacy path.
    Mirrors the agent: only terms it actually respelled, first occurrence only
    (across all scenes in order), case-insensitive. The input is not mutated —
    captions/display, footage and manifest keep the original spelling.
    """
    if not pron_map or not isinstance(scenes_data, dict) or not scenes_data.get("scenes"):
        return scenes_data
    tts_text = (tts_script or {}).get("full_script", "") if isinstance(tts_script, dict) else ""
    active = {t: r for t, r in pron_map.items() if r in tts_text}
    if not active:
        return scenes_data

    import re as _re
    new_scenes = [dict(s) if isinstance(s, dict) else s for s in scenes_data["scenes"]]
    applied = 0
    for term, respelling in active.items():
        pattern = _re.compile(_re.escape(term), _re.IGNORECASE)
        for sc in new_scenes:
            if not isinstance(sc, dict):
                continue
            narration = sc.get("narration") or ""
            if pattern.search(narration):
                sc["narration"] = pattern.sub(respelling, narration, count=1)
                applied += 1
                break  # first occurrence only
    if not applied:
        return scenes_data
    logger.info(f"[TTS Format] {applied} pronunciation respelling(s) applied to scene narration (audio only)")
    return {**scenes_data, "scenes": new_scenes}


def _audio_scene_input(ctx: PipelineContext):
    """Scenes passed to stage 8: respelled narration for TTS, originals elsewhere."""
    try:
        return _respell_scenes_for_audio(
            ctx.scenes_data, ctx.tts_script,
            _pronunciation_map(ctx.agents.get("a_tts_format")),
        )
    except Exception as e:
        logger.warning(f"[TTS Format] Scene respelling skipped (non-fatal): {e}")
        return ctx.scenes_data


def _generate_thumbnail_task(ctx: PipelineContext):
    """Generate thumbnail (runs in thread pool or sequentially)."""
    from agents import thumbnail_agent
    _script = ctx.script or ctx.state.get("stage_4", {})
    _angle = ctx.angle or ctx.state.get("stage_2", {})
    return thumbnail_agent.run(ctx.seo, _script, _angle)


def _run_wave3(ctx: PipelineContext, runner: StageRunner) -> None:
    """Wave 3 DAG: Audio(8) + Footage(9) + Thumbnail in parallel."""
    a09 = ctx.agents["a09"]
    _need_audio = not (8 < ctx.from_stage or runner.done(8))
    _need_footage = not (9 < ctx.from_stage or runner.done(9))

    if _need_audio and _need_footage:
        logger.info("\n[Pipeline] Running Audio + Footage + Thumbnail in parallel...")
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="dag_w3") as executor:
            _audio_future = executor.submit(runner.run_stage, 8, "Audio Production", run_audio, ctx.tts_script, _audio_scene_input(ctx), ctx.display_script)
            _footage_future = executor.submit(runner.run_stage, 9, "Footage Hunting", a09.run, ctx.scenes_data or {})

            _thumb_future = None
            if ctx.seo and not ctx.state.get("thumbnail"):
                _thumb_future = executor.submit(_generate_thumbnail_task, ctx)

            ctx.audio_data = _audio_future.result()
            ctx.footage_data = _footage_future.result()

            if _thumb_future:
                try:
                    thumb_result = _thumb_future.result(timeout=120)
                    if thumb_result and thumb_result.get("thumbnail_path"):
                        runner.mark_metadata("thumbnail", thumb_result)
                        logger.info(f"[Pipeline] Thumbnail score: {thumb_result.get('score', '?')}/30")
                except Exception as e:
                    logger.warning(f"[Pipeline] Thumbnail agent warning: {e}")
    else:
        ctx.audio_data = runner.run_stage(8, "Audio Production", run_audio, ctx.tts_script, _audio_scene_input(ctx), ctx.display_script)
        ctx.footage_data = runner.run_stage(9, "Footage Hunting", a09.run, ctx.scenes_data or {})
        if ctx.seo and not ctx.state.get("thumbnail"):
            try:
                thumb_result = _generate_thumbnail_task(ctx)
                if thumb_result and thumb_result.get("thumbnail_path"):
                    runner.mark_metadata("thumbnail", thumb_result)
                    logger.info(f"[Pipeline] Thumbnail score: {thumb_result.get('score', '?')}/30")
            except Exception as e:
                logger.warning(f"[Pipeline] Thumbnail agent warning: {e}")

    _check_and_warn(check_audio(ctx.audio_data or {}), "Audio")


# ══════════════════════════════════════════════════════════════════════════════
# Manifest + Render + QA
# ══════════════════════════════════════════════════════════════════════════════

def _build_manifest(ctx: PipelineContext, runner: StageRunner) -> None:
    """Build or load media manifest."""
    if not runner.done(9):
        # Build manifest from footage data, then merge in Agent 07 scene fields
        # (narrative_function, claim_confidence, visual_treatment, motion graphics, etc.)
        # Footage data (stage 9) has visuals; scenes_data (stage 7) has narrative metadata.
        _footage_scenes = ctx.footage_data.get("scenes", []) if ctx.footage_data else []
        _scene_scenes = ctx.scenes_data.get("scenes", []) if ctx.scenes_data else []
        # Merge stage 7 fields into footage scenes by index
        _merge_keys = [
            "narrative_function", "claim_confidence", "visual_treatment",
            "is_reveal_moment", "is_breathing_room", "show_map", "show_timeline",
            "lower_third", "key_text", "key_text_type", "retention_hook",
            "year", "location", "characters_mentioned",
        ]
        # Keys where stage 7 should OVERRIDE stage 9's version
        _override_keys = {"characters_mentioned", "is_reveal_moment", "is_breathing_room"}
        for i, fs in enumerate(_footage_scenes):
            if i < len(_scene_scenes):
                for mk in _merge_keys:
                    if mk in _scene_scenes[i]:
                        if mk in _override_keys or mk not in fs:
                            fs[mk] = _scene_scenes[i][mk]
        ctx.manifest = {
            "topic": ctx.topic,
            "title": ctx.seo.get("recommended_title", ctx.topic) if ctx.seo else ctx.topic,
            "audio": ctx.audio_data,
            "scenes": _footage_scenes,
            "credits": ctx.footage_data.get("credits", []) if ctx.footage_data else [],
            "total_duration_seconds": ctx.audio_data.get("total_duration_seconds", 0) if ctx.audio_data else 0,
        }
        if ctx.scenes_data and ctx.scenes_data.get("visual_bible"):
            ctx.manifest["visual_bible"] = ctx.scenes_data["visual_bible"]
        with open(MEDIA_DIR / "media_manifest.json", "w") as f:
            json.dump(ctx.manifest, f, indent=2)
        runner.mark_metadata("manifest", ctx.manifest)
    else:
        if (MEDIA_DIR / "media_manifest.json").exists():
            with open(MEDIA_DIR / "media_manifest.json") as f:
                ctx.manifest = json.load(f)
        else:
            ctx.manifest = ctx.state.get("manifest", {})
            if not ctx.manifest.get("scenes"):
                footage_saved = ctx.state.get("stage_9", {}) or {}
                if footage_saved.get("scenes"):
                    _audio_saved = ctx.state.get("stage_8", {}) or {}
                    ctx.manifest = {
                        "topic": ctx.topic,
                        "title": (ctx.seo or ctx.state.get("stage_6", {})).get("recommended_title", ctx.topic),
                        "audio": _audio_saved,
                        "scenes": footage_saved.get("scenes", []),
                        "credits": footage_saved.get("credits", []),
                        "total_duration_seconds": _audio_saved.get("total_duration_seconds", 0),
                    }
                    logger.info("[Pipeline] Reconstructed manifest from stage_9 state data")
                else:
                    logger.warning("[Pipeline] WARNING: manifest has no scenes and no stage_9 data \u2014 re-run from stage 9")


def _run_render_phase(ctx: PipelineContext, runner: StageRunner) -> None:
    """Pre-render validation, conversion, and rendering."""
    _audio_for_convert = ctx.audio_data or ctx.state.get("stage_8", {})
    if not (_audio_for_convert or {}).get("total_duration_seconds"):
        raise RuntimeError(
            "[Stage 11] audio_data.total_duration_seconds missing \u2014 "
            "stage 8 did not complete. Re-run with --from-stage 8"
        )

    # Tier 0: Pre-render data validation
    try:
        from core.quality_gates import run_tier0_prerender
        _t0_outputs = {
            "script": ctx.script or ctx.state.get("stage_4", {}),
            "scenes": ctx.scenes_data or ctx.state.get("stage_7", {}),
            "audio": _audio_for_convert,
            "seo": ctx.seo or ctx.state.get("stage_6", {}),
        }
        t0 = run_tier0_prerender(_t0_outputs)
        if not t0["passed"]:
            logger.error(f"\n[QA Tier 0] FAILED \u2014 {len(t0['errors'])} error(s):")
            for err in t0["errors"]:
                logger.error(f"  \u2717 {err}")
            raise RuntimeError("[QA Tier 0] Pre-render validation failed \u2014 fix errors before rendering.")
        if t0["warnings"]:
            logger.warning(f"[QA Tier 0] {len(t0['warnings'])} warning(s):")
            for w in t0["warnings"]:
                logger.warning(f"  \u26a0 {w}")
        else:
            logger.info("[QA Tier 0] Pre-render validation passed.")
    except ImportError:
        pass
    except RuntimeError:
        raise
    except Exception as t0_err:
        logger.warning(f"[QA Tier 0] Check failed (non-fatal): {t0_err}")

    runner.run_stage(11, "Remotion Conversion", run_convert, ctx.manifest, _audio_for_convert, ctx.topic, ctx.state.get("era", ""))
    runner.run_stage(12, "Video Render", run_render, ctx.topic)


def _run_qa_tiers(ctx: PipelineContext, runner: StageRunner) -> None:
    """Post-render QA tiers 1 and 2."""
    _audio_for_convert = ctx.audio_data or ctx.state.get("stage_8", {})

    # Tier 1: Post-render technical validation
    try:
        from core.quality_gates import run_tier1_postrender
        _video_path = ctx.state.get("stage_12", "")
        _script_for_t1 = ctx.script or ctx.state.get("stage_4", {})
        t1 = run_tier1_postrender(_video_path, _audio_for_convert, _script_for_t1)
        if not t1["passed"]:
            logger.error(f"\n[QA Tier 1] FAILED \u2014 {len(t1['errors'])} error(s):")
            for err in t1["errors"]:
                logger.error(f"  \u2717 {err}")
        if t1["warnings"]:
            for w in t1["warnings"]:
                logger.warning(f"  \u26a0 {w}")
        if t1.get("metrics"):
            logger.info(f"[QA Tier 1] Metrics: {json.dumps(t1['metrics'])}")
        if t1["passed"]:
            logger.info("[QA Tier 1] Post-render validation passed.")
        runner.mark_metadata("qa_tier1", t1)
    except ImportError:
        pass
    except Exception as t1_err:
        logger.warning(f"[QA Tier 1] Check failed (non-fatal): {t1_err}")

    # Tier 2: Content quality (visual-narration sync)
    try:
        from core.quality_gates import run_tier2_content
        _video_path = ctx.state.get("stage_12", "")
        _scenes_for_t2 = ctx.scenes_data or ctx.state.get("stage_7", {})
        _script_for_t2 = ctx.script or ctx.state.get("stage_4", {})
        t2 = run_tier2_content(_video_path, _script_for_t2, _scenes_for_t2)
        if t2["warnings"]:
            for w in t2["warnings"]:
                logger.warning(f"  \u26a0 {w}")
        logger.info(f"[QA Tier 2] Sync score: {t2['sync_score']*100:.0f}% \u2014 {'PASSED' if t2['passed'] else 'BELOW THRESHOLD'}")
        runner.mark_metadata("qa_tier2", t2)
    except ImportError:
        pass
    except Exception as t2_err:
        logger.warning(f"[QA Tier 2] Check failed (non-fatal): {t2_err}")
