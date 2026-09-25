"""Phase Post — upload, analytics, notifications, cleanup."""

from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from core.log import get_logger
from core.paths import BASE_DIR
from pipeline.state import save_state
from pipeline.helpers import _cleanup_after_upload
from pipeline.loader import load_agent

logger = get_logger(__name__)

if TYPE_CHECKING:
    from concurrent.futures import Future
    from pipeline.context import PipelineContext
    from pipeline.runner import StageRunner


def run_post_phase(
    ctx: PipelineContext,
    runner: StageRunner,
    shorts_future: Future | None,
    shorts_executor: object | None,
) -> None:
    """Upload, analytics, notifications, cleanup. Final phase of pipeline."""

    # ── Predictive performance scoring ────────────────────────────────────────
    _run_predictive_scoring(ctx)

    # ── Render verification ───────────────────────────────────────────────────
    _run_render_verification(ctx)

    # ── Community teaser before upload ────────────────────────────────────────
    if not runner.done(13):
        try:
            from server import notify
            _seo = ctx.seo or ctx.state.get("stage_6", {}) or {}
            notify.notify_community_teaser(ctx.topic, _seo.get("recommended_title", ctx.topic))
        except Exception:
            pass

    # ── Stage 13: Upload ──────────────────────────────────────────────────────
    a11 = ctx.agents["a11"]

    # Use thumbnail agent's scored output if available (War Room v5 fix C2)
    _thumb_data = ctx.state.get("thumbnail") or {}
    _thumbnail_path = _thumb_data.get("thumbnail_path") if isinstance(_thumb_data, dict) else None

    def do_upload():
        return a11.run(
            ctx.seo or ctx.state.get("stage_6", {}),
            ctx.manifest,
            ctx.verification or ctx.state.get("stage_5", {}),
            research_data=ctx.research or ctx.state.get("stage_1", {}),
            privacy="public",
            thumbnail_path=_thumbnail_path,
        )

    runner.run_stage(13, "Upload", do_upload)
    _on_youtube = _uploaded_to_youtube(ctx)
    if not _on_youtube:
        logger.info("[Pipeline] Upload provider is not YouTube — skipping YouTube analytics, "
                    "comment analysis, param observation and community posts")

    # ── Record topic as covered ───────────────────────────────────────────────
    _record_topic(ctx)

    # ── Save to Supabase ──────────────────────────────────────────────────────
    _save_to_supabase(ctx)

    # ── Store param observation (needs YouTube retention data) ────────────────
    if _on_youtube:
        _store_param_observation(ctx)

    # ── Analytics agent (reads YouTube stats) ────────────────────────────────
    if _on_youtube:
        try:
            a12 = load_agent(Path("12_analytics_agent.py"))
            logger.info("[Pipeline] Running analytics agent...")
            a12.run()
        except Exception as analytics_err:
            logger.warning(f"[Pipeline] Analytics agent warning: {analytics_err}")

    # ── Comment analysis ──────────────────────────────────────────────────────
    if _on_youtube:
        _run_comment_analysis(ctx)

    # ── Localization (opt-in) ─────────────────────────────────────────────────
    if os.getenv("LOCALIZATION_ENABLED", "").lower() == "true":
        _run_localization(ctx)

    # ── Cost tracking finalization ────────────────────────────────────────────
    _finalize_costs(ctx)

    # ── Final state assembly ──────────────────────────────────────────────────
    elapsed = round(time.time() - ctx.start_time, 1)
    ctx.state["pipeline_status"] = "COMPLETE"
    ctx.state["elapsed_seconds"] = elapsed
    _log_api_costs(ctx)
    save_state(ctx.state, ctx.state_path)

    # ── Collect shorts result ─────────────────────────────────────────────────
    short_upload_result = None
    try:
        if shorts_future is not None:
            short_upload_result, _short_script_bg = shorts_future.result(timeout=900)
    except Exception as _shorts_err:
        logger.warning(f"[Shorts] Background pipeline completed with: {_shorts_err}")
        short_upload_result = None
    finally:
        if shorts_executor is not None:
            shorts_executor.shutdown(wait=False)

    # ── Completion notification ───────────────────────────────────────────────
    try:
        from server import notify
        seo_saved = ctx.seo or ctx.state.get("stage_6", {}) or {}
        upload_saved = ctx.state.get("stage_13", {}) or {}
        short_url = ""
        if short_upload_result:
            short_url = short_upload_result.get("url", "")
        notify.notify_pipeline_complete(
            topic=ctx.topic,
            title=seo_saved.get("recommended_title", ctx.topic),
            youtube_url=upload_saved.get("url", ""),
            elapsed_minutes=elapsed / 60,
            short_url=short_url,
        )
    except Exception:
        pass

    # ── Community engagement ──────────────────────────────────────────────────
    if _on_youtube:
        _run_community_engagement(ctx)

    # ── Quality checks ────────────────────────────────────────────────────────
    _run_quality_report(ctx)

    # ── Pipeline optimizer analysis ──────────────────────────────────────────
    _run_optimizer_analysis(ctx)

    # ── Post-upload cleanup ───────────────────────────────────────────────────
    try:
        from core.pipeline_config import CLEANUP_AFTER_UPLOAD
    except ImportError:
        CLEANUP_AFTER_UPLOAD = True

    upload_ok = bool(ctx.state.get("stage_13", {}).get("video_id"))
    if CLEANUP_AFTER_UPLOAD and upload_ok:
        _cleanup_after_upload(ctx.state, ctx.state_path)

    logger.info(f"\n{'='*60}")
    logger.info(f"  PIPELINE COMPLETE in {elapsed/60:.1f} minutes")
    if ctx.seo:
        logger.info(f"  Title: {ctx.seo.get('recommended_title', '')}")
    logger.info(f"  State: {ctx.state_path.name}")
    logger.info(f"{'='*60}\n")


# ══════════════════════════════════════════════════════════════════════════════
# Helper functions (extracted from inline code)
# ══════════════════════════════════════════════════════════════════════════════

def _uploaded_to_youtube(ctx: PipelineContext) -> bool:
    """True if stage 13 published to YouTube.

    Stage 13 results record the upload provider under "provider"; results
    without it predate the provider system, when uploads always went to YouTube.
    """
    res = ctx.state.get("stage_13") or {}
    if not isinstance(res, dict):
        return False
    return res.get("provider", "youtube") == "youtube"


def _youtube_id(ctx: PipelineContext) -> str:
    """Stage 13 video id if it is a YouTube id, else ""."""
    res = ctx.state.get("stage_13") or {}
    if not isinstance(res, dict) or not _uploaded_to_youtube(ctx):
        return ""
    return res.get("video_id", "")

def _run_predictive_scoring(ctx: PipelineContext) -> None:
    try:
        insights_file = BASE_DIR / "channel_insights.json"
        if insights_file.exists():
            _insights = json.loads(insights_file.read_text())
            _seo = ctx.seo or ctx.state.get("stage_6", {}) or {}
            _script = ctx.script or ctx.state.get("stage_4", {}) or {}

            score = 0
            reasons = []

            top_eras = _insights.get("era_performance_ranking", [])
            if top_eras:
                topic_lower = ctx.topic.lower()
                for i, era in enumerate(top_eras[:5]):
                    era_name = era.get("era", "") if isinstance(era, dict) else str(era)
                    if era_name.lower() in topic_lower:
                        score += 5 - i
                        reasons.append(f"Era match: {era_name} (rank {i+1})")
                        break

            title = _seo.get("recommended_title", "")
            if 50 <= len(title) <= 65:
                score += 3
                reasons.append("Optimal title length")
            elif len(title) < 40:
                score -= 1
                reasons.append("Title may be too short")

            wc = len(_script.get("full_script", "").split())
            if 1400 <= wc <= 2000:
                score += 2
                reasons.append(f"Good script length ({wc} words)")

            if len(_seo.get("tags", [])) >= 10:
                score += 1
                reasons.append("Strong tag coverage")

            logger.info(f"[Pipeline] Predictive score: {score}/11")
            for r in reasons:
                logger.info(f"  + {r}")
            ctx.state["predictive_score"] = {"score": score, "max": 11, "reasons": reasons}
            save_state(ctx.state, ctx.state_path)
    except Exception as pred_err:
        logger.warning(f"[Pipeline] Predictive scoring warning: {pred_err}")


def _run_render_verification(ctx: PipelineContext) -> None:
    try:
        from core.render_verification import verify_render_output
        _video_path = ctx.state.get("stage_12", "")
        if _video_path:
            try:
                from core.param_registry import get_active_params
                production_params = get_active_params(format="long")
            except Exception:
                production_params = {}
            ctx.state["production_params"] = production_params

            verification_result = verify_render_output(
                _video_path, production_params, ctx.state, format="long"
            )
            if verification_result:
                ctx.state["render_verification"] = verification_result.to_dict()
                logger.info(f"[Render Verify] Compliance: {verification_result.overall_compliance:.0%}")
                for dev in verification_result.deviations:
                    logger.warning(f"  \u26a0 {dev}")

            # ── Rendered audit (extract frames + silence beat audio for review) ──
            try:
                from core.rendered_audit import run_rendered_audit
                _audit_dir = str(Path(ctx.state_path).parent / "rendered_audit")
                _pqa_scenes = ctx.state.get("scenes", [])
                if not _pqa_scenes:
                    _s11 = ctx.state.get("stage_11", {})
                    if isinstance(_s11, dict):
                        _pqa_scenes = _s11.get("scenes", [])
                audit_result = run_rendered_audit(
                    _video_path, _pqa_scenes, _audit_dir
                )
                ctx.state["rendered_audit"] = audit_result
                _n_frames = len(audit_result.get("frames", []))
                _n_audio = len(audit_result.get("silence_beat_audio", []))
                if _n_frames or _n_audio:
                    logger.info(
                        f"[Rendered Audit] Extracted {_n_frames} frame(s)"
                        f" + {_n_audio} audio segment(s)"
                    )
            except Exception as audit_err:
                logger.warning(f"[Rendered Audit] Skipped: {audit_err}")

            save_state(ctx.state, ctx.state_path)
    except Exception as rv_err:
        logger.warning(f"[Pipeline] Render verification warning: {rv_err}")


def _record_topic(ctx: PipelineContext) -> None:
    try:
        from server import topic_store
        angle_data = ctx.angle or ctx.state.get("stage_2", {}) or {}
        seo_data = ctx.seo or ctx.state.get("stage_6", {}) or {}
        topic_store.record_topic(
            topic=ctx.topic,
            angle=angle_data.get("unique_angle", angle_data.get("chosen_angle", "")),
            title=seo_data.get("recommended_title", ctx.topic),
            youtube_id=_youtube_id(ctx),
        )
    except Exception as ts_err:
        logger.warning(f"[Pipeline] topic_store warning: {ts_err}")


def _save_to_supabase(ctx: PipelineContext) -> None:
    upload_result = ctx.state.get("stage_13", {})
    if upload_result:
        try:
            from clients import supabase_client
            seo_data = ctx.seo or ctx.state.get("stage_6", {}) or {}
            audio_saved = ctx.audio_data or ctx.state.get("stage_8", {}) or {}
            script_saved = ctx.script or ctx.state.get("stage_4", {}) or {}
            # Extract scene manifest from convert stage output BEFORE stripping
            # (War Room v5 fix C4 — Red Team Attack #1 mitigation)
            convert_data = ctx.state.get("stage_11", {})
            scene_manifest = None
            if isinstance(convert_data, dict):
                scene_manifest = convert_data.get("scene_manifest")
            # Fallback: read scene_manifest from video-data.json if not in state
            if scene_manifest is None:
                try:
                    import json as _json
                    _vd_path = Path(__file__).resolve().parent.parent / "remotion" / "src" / "video-data.json"
                    if _vd_path.exists():
                        _vd = _json.loads(_vd_path.read_text())
                        scene_manifest = _vd.get("scene_manifest")
                        if scene_manifest:
                            logger.info(f"[Pipeline] Scene manifest recovered from video-data.json ({len(scene_manifest)} scenes)")
                except Exception as vd_err:
                    logger.warning(f"[Pipeline] Could not read video-data.json for scene manifest: {vd_err}")
            # Strip stage_11 (full video-data dict, 300-480KB) to prevent payload overflow
            stripped_state = {k: v for k, v in ctx.state.items() if k != "stage_11"}
            supabase_client.save_video(
                topic=ctx.topic,
                title=seo_data.get("recommended_title", ctx.topic),
                youtube_url=upload_result.get("url", "") if _uploaded_to_youtube(ctx) else "",
                youtube_id=_youtube_id(ctx),
                script_path=ctx.state.get("script_path", ""),
                video_path="",
                duration_seconds=audio_saved.get("total_duration_seconds", 0),
                word_count=script_saved.get("word_count", 0) or len(script_saved.get("full_script", "").split()),
                pipeline_state=stripped_state,
                scene_manifest=scene_manifest,
            )
        except Exception as e:
            logger.warning(f"[Pipeline] could not save video to Supabase: {e}")


def _store_param_observation(ctx: PipelineContext) -> None:
    upload_result = ctx.state.get("stage_13", {})
    if upload_result:
        try:
            from core.param_history import store_observation
            rv = ctx.state.get("render_verification")
            store_observation(
                video_id=str((ctx.state.get("stage_13") or {}).get("video_id", "")),
                youtube_id=upload_result.get("video_id", ""),
                params=ctx.state.get("production_params", {}),
                era=ctx.state.get("era", "unknown"),
                render_verification=rv,
            )
            logger.info("[Pipeline] \u2713 Param observation stored for optimizer")
        except Exception as obs_err:
            logger.warning(f"[Pipeline] Param observation warning: {obs_err}")


def _run_comment_analysis(ctx: PipelineContext) -> None:
    try:
        from intel import comment_analyzer
        from clients import supabase_client as _sb
        recent = _sb.get_client().table("videos").select("youtube_id").order("created_at", desc=True).limit(3).execute()
        recent_ids = [v["youtube_id"] for v in (recent.data or []) if v.get("youtube_id")]
        if recent_ids:
            intel = comment_analyzer.get_comment_intelligence_block(recent_ids)
            if intel:
                ctx.state["comment_intelligence"] = intel
                logger.info(f"[Comments] Audience intelligence extracted from {len(recent_ids)} videos")
    except Exception as _com_err:
        logger.warning(f"[Comments] Analysis skipped: {_com_err}")


def _run_localization(ctx: PipelineContext) -> None:
    try:
        from media import localization_pipeline
        logger.info(f"\n{'='*60}")
        logger.info("  LOCALIZATION PIPELINE \u2014 STARTING")
        logger.info(f"{'='*60}")
        loc_results = localization_pipeline.run(ctx.topic, ctx.state)
        if loc_results:
            ctx.state["localization"] = {
                lang: {"video_path": r.get("video_path", "")}
                for lang, r in loc_results.items()
            }
            save_state(ctx.state, ctx.state_path)
            logger.info(f"[Localization] Complete: {', '.join(loc_results.keys())}")
    except Exception as _loc_err:
        logger.warning(f"[Localization] Failed (non-critical): {_loc_err}")


def _finalize_costs(ctx: PipelineContext) -> None:
    if ctx.cost_tracker:
        try:
            from clients.claude_client import get_session_costs as _get_costs
            session = _get_costs()
            usd_total = session.get("usd_total", 0.0)
            if usd_total > 0:
                ctx.cost_tracker.log_usd_cost(ctx.cost_run_id, "pipeline_total", "claude_all", usd_total)
            upload_res = ctx.state.get("stage_13", {}) or {}
            ctx.cost_tracker.end_run(ctx.cost_run_id, video_id=upload_res.get("video_id", ""))
            estimate = ctx.cost_tracker.get_cost_estimate(ctx.cost_run_id)
            if estimate:
                logger.info(f"[Cost] Estimated total: ${estimate.get('total_cost', 0):.2f}")
                ctx.state["cost_estimate"] = estimate
        except Exception as _cost_err:
            logger.warning(f"[Cost] Finalization warning: {_cost_err}")


def _log_api_costs(ctx: PipelineContext) -> None:
    try:
        from clients.claude_client import get_session_costs
        costs = get_session_costs()
        estimate = ctx.state.get("cost_estimate", {})
        if estimate:
            costs["usd_total"] = round(estimate.get("total_cost", costs["usd_total"]), 4)
            costs["per_stage"] = estimate.get("per_stage", {})
            costs["per_service"] = estimate.get("per_service", {})
        ctx.state["costs"] = costs
        logger.info(f"[Pipeline] Total costs this run: ${costs['usd_total']:.4f}")
    except Exception:
        pass


def _run_community_engagement(ctx: PipelineContext) -> None:
    try:
        from intel.community_engagement import run_post_upload as _community_post
        upload_saved = ctx.state.get("stage_13", {}) or {}
        seo_saved = ctx.seo or ctx.state.get("stage_6", {}) or {}
        yt_url = upload_saved.get("url", "")
        yt_id = upload_saved.get("video_id", "")
        if yt_url and yt_id:
            hook_line = ""
            stage_4 = ctx.state.get("stage_4") or {}
            if isinstance(stage_4, dict):
                hook_line = stage_4.get("hook", "")
            community_result = _community_post(
                video_id=yt_id,
                video_title=seo_saved.get("recommended_title", ctx.topic),
                video_url=yt_url,
                topic=ctx.topic,
                era=ctx.state.get("stage_1", {}).get("era", ""),
                hook=hook_line,
            )
            ctx.state["community_engagement"] = community_result
    except Exception as _ce_err:
        logger.warning(f"[Community] Post draft skipped: {_ce_err}")


def _run_quality_report(ctx: PipelineContext) -> None:
    try:
        from core import quality_gates as qg
        pipeline_outputs = {
            "research": ctx.state.get("stage_1", {}),
            "angle": ctx.state.get("stage_2", {}),
            "script": ctx.state.get("stage_4", {}),
            "scenes": ctx.state.get("stage_7", {}),
            "audio": ctx.state.get("stage_8", {}),
            "images": ctx.state.get("manifest", ctx.state.get("stage_9", {})),
            "seo": ctx.state.get("stage_6", {}),
            "thumbnail": ctx.state.get("thumbnail", {}),
            "video_path": ctx.state.get("stage_12", ""),
        }
        qc = qg.run_all_quality_checks(pipeline_outputs)
        if qc["warnings"]:
            logger.warning(f"\n[Quality Report] {qc['total_warnings']} warning(s):")
            for w in qc["warnings"]:
                logger.warning(f"  \u26a0 {w}")
            try:
                from server.notify import notify_quality_warn
                notify_quality_warn("Pipeline Quality Check", qc["warnings"])
            except Exception:
                pass
        else:
            logger.info("\n[Quality Report] All checks passed \u2014 no warnings.")
        logger.info(f"[Quality Metrics] {json.dumps(qc['metrics'], indent=2)}")
    except Exception as qe:
        logger.warning(f"[Quality Report] Could not run checks: {qe}")


def _run_optimizer_analysis(ctx: PipelineContext) -> None:
    """Run post-pipeline quality analysis and save to lessons_learned.json."""
    try:
        from core.pipeline_optimizer import analyze
        analyze(ctx.state, ctx.state_path)
    except Exception as opt_err:
        logger.warning(f"[Pipeline] Optimizer analysis skipped: {opt_err}")
