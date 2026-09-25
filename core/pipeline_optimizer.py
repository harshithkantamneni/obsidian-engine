"""
Pipeline Optimizer — post-run quality analysis and improvement suggestions.

Runs after each successful pipeline completion.
Analyzes: stage timings, output quality, prompt effectiveness, cross-run trends.
Writes a structured report to lessons_learned.json and prints a summary.
"""

import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timezone

sys.path.append(str(Path(__file__).resolve().parent.parent))

try:
    from core.pipeline_config import SCORING_CONFIG, SCORING_THRESHOLDS
except ImportError:
    SCORING_CONFIG = {}
    SCORING_THRESHOLDS = {}

BASE_DIR     = Path(__file__).resolve().parent.parent
OUTPUT_DIR   = BASE_DIR / "outputs"
LESSONS_FILE = BASE_DIR / "lessons_learned.json"

# ── Stage metadata ─────────────────────────────────────────────────────────────
STAGE_META = {
    1:  {"name": "Research",            "agent": "01_research_agent.py",       "critical": True},
    2:  {"name": "Originality",         "agent": "02_originality_agent.py",    "critical": True},
    3:  {"name": "Narrative",           "agent": "03_narrative_architect.py",        "critical": True},
    4:  {"name": "Script",              "agent": "04_script_writer.py",              "critical": True},
    5:  {"name": "Verification",        "agent": "05_fact_verification_agent.py",    "critical": True},
    6:  {"name": "SEO",                 "agent": "06_seo_agent.py",                  "critical": False},
    7:  {"name": "Scene Breakdown",     "agent": "07_scene_breakdown_agent.py",      "critical": True},
    8:  {"name": "Audio",               "agent": None,                         "critical": True},
    9:  {"name": "Footage",             "agent": "09_footage_hunter.py",       "critical": False},
    10: {"name": "Images",              "agent": None,                         "critical": False},
    11: {"name": "Remotion Conversion", "agent": None,                         "critical": True},
    12: {"name": "Video Render",        "agent": None,                         "critical": True},
    13: {"name": "Upload",              "agent": None,                         "critical": True},
}

STAGE_NAMES = {k: v["name"] for k, v in STAGE_META.items()}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_agent_prompt(agent_file: str) -> str:
    """Load the SYSTEM_PROMPT from an agent file without executing it."""
    path = BASE_DIR / agent_file
    if not path.exists():
        return ""
    try:
        source = path.read_text()
        # Extract SYSTEM_PROMPT string via AST-safe regex
        m = re.search(
            r'SYSTEM_PROMPT\s*=\s*(?:f)?"""(.*?)"""',
            source, re.DOTALL
        ) or re.search(
            r'SYSTEM_PROMPT\s*=\s*(?:f)?\'\'\'(.*?)\'\'\'',
            source, re.DOTALL
        )
        return m.group(1).strip()[:3000] if m else ""
    except Exception:
        return ""


def _load_recent_states(current_path: Path, limit: int = 20) -> list[dict]:
    """Load the N most recent state files (excluding current run)."""
    all_states = sorted(
        OUTPUT_DIR.glob("*_state.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    results = []
    for p in all_states:
        if p == current_path:
            continue
        try:
            with open(p) as f:
                results.append(json.load(f))
        except Exception:
            pass
        if len(results) >= limit:
            break
    return results


def _timing_analysis(state: dict, past_states: list) -> dict:
    """Compare this run's per-stage timings against recent averages."""
    timings = state.get("stage_timings", {})
    if not timings:
        return {"available": False}

    # Build historical averages
    hist = {}
    for ps in past_states:
        for k, v in (ps.get("stage_timings") or {}).items():
            hist.setdefault(k, []).append(v)

    analysis = {}
    total = sum(timings.values())
    for k, t in sorted(timings.items(), key=lambda x: -x[1]):
        try:
            stage_name = STAGE_NAMES.get(int(k), f"Stage {k}")
        except (ValueError, TypeError):
            stage_name = f"Stage {k}"
        avg = round(sum(hist[k]) / len(hist[k]), 1) if k in hist else None
        delta = round(t - avg, 1) if avg else None
        flag = ""
        if delta and delta > 30:
            flag = "⚠ SLOW"
        elif delta and delta < -30:
            flag = "✓ FASTER"
        analysis[k] = {
            "stage": stage_name,
            "seconds": t,
            "pct_of_total": round(t / total * 100, 1) if total else 0,
            "avg_seconds": avg,
            "delta_vs_avg": delta,
            "flag": flag,
        }
    return {"available": True, "stages": analysis, "total_seconds": round(total, 1)}


def _quality_analysis(state: dict) -> dict:
    """Run all quality_gates checks against the state outputs."""
    try:
        import core.quality_gates as qg
    except ImportError:
        return {"available": False}

    results = {}

    def _check(key, fn, *args):
        try:
            issues = fn(*args)
            results[key] = {"warnings": issues, "score": max(0, 10 - len(issues) * 2)}
        except Exception as e:
            results[key] = {"warnings": [f"Check failed: {e}"], "score": 5}

    if state.get("stage_4"):
        _check("script",     qg.quality_script,   state["stage_4"])
        _check("script_len", lambda d: (
            [] if 1000 <= len(d.get("full_script","").split()) <= 2500
            else ["Script length out of range"]
        ), state["stage_4"])
    if state.get("stage_1"):
        _check("research",   qg.quality_research,  state["stage_1"])
    if state.get("stage_2"):
        _check("angle",      qg.quality_angle,     state["stage_2"])
    if state.get("stage_6"):
        _check("seo",        qg.quality_seo,       state["stage_6"])
    if state.get("stage_7"):
        _check("scenes",     qg.quality_scenes,    state["stage_7"])
    if state.get("stage_8"):
        _check("audio",      qg.quality_audio,     state["stage_8"])

    # B-roll prompt quality
    manifest = state.get("manifest") or state.get("stage_9") or {}
    scenes = manifest.get("scenes", [])
    if scenes:
        prompt_scores = []
        for s in scenes:
            prompt = (s.get("visual", {}).get("prompt", "")
                      or s.get("image_prompt", "")
                      or s.get("ai_prompt", ""))
            if prompt:
                prompt_scores.append(qg.score_broll_prompt(prompt))
        if prompt_scores:
            avg_score = round(sum(prompt_scores) / len(prompt_scores), 1)
            low = [i for i, sc in enumerate(prompt_scores) if sc < 4]
            results["broll_prompts"] = {
                "avg_score": avg_score,
                "total": len(prompt_scores),
                "low_quality_count": len(low),
                "score": min(10, int(avg_score)),
                "warnings": [f"{len(low)} B-roll prompts scored below 4/10"] if low else [],
            }

    # Professional checks (new)
    if state.get("stage_8"):
        audio_path = state["stage_8"].get("audio_path", "")
        if audio_path:
            _check("audio_tech", qg.quality_audio_technical, audio_path)
    video_path = state.get("stage_12", "")
    if video_path:
        _check("video_tech", qg.quality_video_technical, video_path)
    if state.get("stage_8") and state.get("stage_7"):
        _check("av_sync", qg.quality_audio_video_sync, state["stage_8"], state["stage_7"])
    if state.get("stage_4"):
        _check("content_policy", qg.quality_content_policy, state["stage_4"])
        _check("sentiment", qg.quality_script_sentiment, state["stage_4"])
        _check("narrative", qg.quality_narrative_structure, state["stage_4"])
    if state.get("stage_4") and state.get("stage_1"):
        _check("plagiarism", qg.quality_plagiarism, state["stage_4"], state["stage_1"])
    if state.get("stage_4") and state.get("stage_8"):
        _check("duration_var", qg.quality_duration_variance, state["stage_4"], state["stage_8"])
    if state.get("stage_6"):
        _check("thumbnail", qg.quality_thumbnail, state["stage_6"])
        _check("seo_complete", qg.quality_seo_completeness, state["stage_6"])
    if state.get("stage_1"):
        _check("source_auth", qg.quality_source_authority, state["stage_1"])
    if manifest and scenes:
        _check("licensing", qg.quality_image_licensing, manifest)

    # Cross-pipeline consistency
    cross_outputs = {
        "scenes": state.get("stage_7", {}),
        "images": manifest or {},
        "audio": state.get("stage_8", {}),
        "seo": state.get("stage_6", {}),
    }
    _check("cross_pipeline", qg.quality_cross_pipeline, cross_outputs)

    overall = round(sum(r.get("score", 5) for r in results.values()) / len(results), 1) if results else 5
    return {"available": True, "checks": results, "overall_score": overall}


def _cross_run_trends(state: dict, past_states: list) -> dict:
    """Identify trends across recent runs."""
    if not past_states:
        return {"runs_available": 0}

    def _word_count(s):
        script = s.get("stage_4", {}) or {}
        return len(script.get("full_script", "").split())

    def _duration(s):
        audio = s.get("stage_8", {}) or {}
        return audio.get("total_duration_seconds", 0)

    def _total_time(s):
        t = s.get("stage_timings", {})
        return round(sum(t.values()), 1) if t else 0

    runs = [state] + past_states
    return {
        "runs_available": len(past_states),
        "avg_word_count":  round(sum(_word_count(s) for s in runs) / len(runs)),
        "avg_duration_min": round(sum(_duration(s) for s in runs) / len(runs) / 60, 1),
        "avg_pipeline_min": round(sum(_total_time(s) for s in runs) / len(runs) / 60, 1),
        "this_run": {
            "word_count":   _word_count(state),
            "duration_min": round(_duration(state) / 60, 1),
            "pipeline_min": round(_total_time(state) / 60, 1),
        },
    }


def _scoring_config_analysis(state: dict) -> dict:
    """Analyze scoring thresholds against actual channel data."""
    if not SCORING_THRESHOLDS:
        return {"available": False}

    insights_file = BASE_DIR / "channel_insights.json"
    if not insights_file.exists():
        return {"available": False, "reason": "no channel_insights.json"}

    try:
        with open(insights_file) as f:
            insights = json.load(f)
    except Exception:
        return {"available": False, "reason": "could not load insights"}

    suggestions = []
    traffic = insights.get("traffic_sources", {})
    demographics = insights.get("audience_demographics", {})
    dq = insights.get("data_quality", {})
    video_count = dq.get("videos_analyzed", 0)

    # Check traffic dominance thresholds against actual distribution
    search_pct = traffic.get("search", {}).get("pct", 0)
    browse_pct = traffic.get("browse", {}).get("pct", 0)

    search_thresh = SCORING_THRESHOLDS.get("traffic_search_dominant_pct", 50)
    if search_pct > 0 and abs(search_pct - search_thresh) < 5:
        suggestions.append({
            "threshold": "traffic_search_dominant_pct",
            "current": search_thresh,
            "actual_value": search_pct,
            "suggestion": f"Search traffic is {search_pct:.0f}%, close to threshold {search_thresh}%. Consider adjusting.",
        })

    browse_thresh = SCORING_THRESHOLDS.get("traffic_browse_dominant_pct", 50)
    if browse_pct > 0 and abs(browse_pct - browse_thresh) < 5:
        suggestions.append({
            "threshold": "traffic_browse_dominant_pct",
            "current": browse_thresh,
            "actual_value": browse_pct,
            "suggestion": f"Browse traffic is {browse_pct:.0f}%, close to threshold {browse_thresh}%. Consider adjusting.",
        })

    # Check demographic threshold
    top_countries = demographics.get("top_countries", [])
    demo_thresh = SCORING_THRESHOLDS.get("demographic_min_audience_pct", 20)
    near_miss_countries = [c for c in top_countries
                          if demo_thresh * 0.6 < c.get("pct", 0) < demo_thresh]
    if near_miss_countries:
        codes = ", ".join(c.get("country", "?") for c in near_miss_countries)
        suggestions.append({
            "threshold": "demographic_min_audience_pct",
            "current": demo_thresh,
            "actual_value": [{"country": c.get("country"), "pct": c.get("pct")} for c in near_miss_countries],
            "suggestion": f"Countries {codes} are near the {demo_thresh}% threshold but excluded. Consider lowering.",
        })

    # Check queue thresholds
    q_high = SCORING_THRESHOLDS.get("queue_high_threshold", 15)
    if video_count > 15 and q_high < 20:
        suggestions.append({
            "threshold": "queue_high_threshold",
            "current": q_high,
            "suggestion": "Channel is mature (15+ videos). Consider raising queue_high_threshold to build a deeper topic buffer.",
        })

    return {
        "available": True,
        "threshold_count": len(SCORING_THRESHOLDS),
        "maturity_tier": "early" if video_count < 5 else "growing" if video_count < SCORING_CONFIG.get("maturity_threshold", 15) else "mature",
        "video_count": video_count,
        "suggestions": suggestions,
        "active_config": {
            "scoring_config": SCORING_CONFIG,
            "threshold_count": len(SCORING_THRESHOLDS),
        },
    }


def _deep_analysis_with_claude(state: dict, timings: dict,
                                quality: dict, trends: dict,
                                scoring: dict = None) -> dict:
    """
    Ask Claude Sonnet to do a deep multi-dimensional analysis and produce
    prioritised, actionable improvement recommendations.
    """
    try:
        from clients.claude_client import call_claude, SONNET

        # Collect agent prompts (truncated)
        prompts_summary = {}
        for num, meta in STAGE_META.items():
            if meta["agent"]:
                p = _load_agent_prompt(meta["agent"])
                if p:
                    prompts_summary[f"stage_{num}_{meta['name']}"] = p[:800]

        # Collect stage outputs (truncated)
        outputs_summary = {}
        for num in range(1, 10):
            data = state.get(f"stage_{num}")
            if data and isinstance(data, dict):
                # Summarise: remove huge fields, keep structure
                summary = {}
                for k, v in data.items():
                    if isinstance(v, str) and len(v) > 500:
                        summary[k] = v[:300] + "..."
                    elif isinstance(v, list):
                        summary[k] = v[:5]
                    else:
                        summary[k] = v
                outputs_summary[f"stage_{num}"] = summary

        # Quality warnings
        all_warnings = []
        for check_name, check_data in (quality.get("checks") or {}).items():
            for w in check_data.get("warnings", []):
                all_warnings.append(f"{check_name}: {w}")

        # Timing bottlenecks
        timing_bottlenecks = []
        if timings.get("available"):
            for k, t in timings.get("stages", {}).items():
                if t.get("flag"):
                    timing_bottlenecks.append(
                        f"Stage {k} ({t['stage']}): {t['seconds']}s "
                        f"({t['flag']}, avg {t.get('avg_seconds')}s)"
                    )

        result = call_claude(
            system_prompt=(
                "You are a senior content strategy and engineering consultant for "
                "a YouTube channel with an automated AI video pipeline. "
                "Your job is to analyze a completed pipeline run and "
                "produce actionable, prioritized recommendations across four dimensions:\n\n"
                "1. PROMPT QUALITY — Are agent system prompts clear, specific, well-constrained?\n"
                "2. OUTPUT QUALITY — Are the stage outputs good? What patterns suggest improvement?\n"
                "3. PERFORMANCE — Which stages are slow, costly, or inefficient?\n"
                "4. CONTENT STRATEGY — Does the video concept, angle, and SEO show strong potential?\n\n"
                "Return JSON with this exact structure:\n"
                "{\n"
                '  "overall_grade": "A/B/C/D/F",\n'
                '  "summary": "2-3 sentence overall assessment",\n'
                '  "prompt_recommendations": [\n'
                '    {"stage": "name", "issue": "...", "suggestion": "...", "priority": "high/med/low"}\n'
                "  ],\n"
                '  "output_recommendations": [\n'
                '    {"stage": "name", "issue": "...", "suggestion": "...", "priority": "high/med/low"}\n'
                "  ],\n"
                '  "performance_recommendations": [\n'
                '    {"area": "...", "issue": "...", "suggestion": "...", "impact": "high/med/low"}\n'
                "  ],\n"
                '  "content_recommendations": [\n'
                '    {"area": "...", "issue": "...", "suggestion": "...", "priority": "high/med/low"}\n'
                "  ],\n"
                '  "top_3_priority_actions": ["action 1", "action 2", "action 3"]\n'
                "}\n\n"
                "Be specific — reference actual content from the outputs. "
                "No generic advice. Each recommendation must be actionable."
            ),
            user_prompt=(
                f"TOPIC: {state.get('topic', 'unknown')}\n\n"
                f"QUALITY WARNINGS ({len(all_warnings)} total):\n"
                + ("\n".join(all_warnings[:20]) if all_warnings else "  None") + "\n\n"
                "TIMING BOTTLENECKS:\n"
                + ("\n".join(timing_bottlenecks) if timing_bottlenecks else "  None") + "\n\n"
                f"CROSS-RUN TRENDS:\n{json.dumps(trends, indent=2)}\n\n"
                f"STAGE OUTPUTS (summarised):\n{json.dumps(outputs_summary, indent=2)[:4000]}\n\n"
                f"AGENT PROMPTS (summarised):\n{json.dumps(prompts_summary, indent=2)[:4000]}"
                + (f"\n\nSCORING CONFIG ANALYSIS:\n{json.dumps(scoring, indent=2)[:1500]}" if scoring and scoring.get("available") else "")
            ),
            model=SONNET,
            max_tokens=3000,
            expect_json=True,
        )
        return result if isinstance(result, dict) else {}
    except Exception as e:
        print(f"[Optimizer] Claude analysis failed: {e}")
        return {}


def _save_report(state: dict, report: dict, timings: dict, quality: dict,
                  scoring: dict = None):
    """Write the full optimizer report to lessons_learned.json."""
    entry = {
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "topic":         state.get("topic", ""),
        "overall_grade": report.get("overall_grade", "?"),
        "summary":       report.get("summary", ""),
        "quality_score": quality.get("overall_score", 0),
        "pipeline_time_min": round(timings.get("total_seconds", 0) / 60, 1),
        "top_3_priority_actions": report.get("top_3_priority_actions", []),
        "prompt_recommendations":      report.get("prompt_recommendations", []),
        "output_recommendations":      report.get("output_recommendations", []),
        "performance_recommendations": report.get("performance_recommendations", []),
        "content_recommendations":     report.get("content_recommendations", []),
        "timing_detail":               timings.get("stages", {}),
        "quality_detail":              quality.get("checks", {}),
        "scoring_suggestions":         scoring if scoring else {},
    }
    try:
        data = {}
        if LESSONS_FILE.exists():
            with open(LESSONS_FILE) as f:
                data = json.load(f)
        runs = data.setdefault("optimizer_runs", [])
        runs.append(entry)
        data["optimizer_runs"] = runs[-100:]  # keep last 100 for long-term trend analysis
        tmp = LESSONS_FILE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(LESSONS_FILE)
        print("[Optimizer] Report saved to lessons_learned.json")
    except Exception as e:
        print(f"[Optimizer] Could not save report: {e}")


def _print_summary(report: dict, timings: dict, quality: dict):
    """Print a human-readable summary to the pipeline log."""
    print(f"\n{'='*60}")
    print("  PIPELINE OPTIMIZER REPORT")
    print(f"{'='*60}")
    grade = report.get("overall_grade", "?")
    score = quality.get("overall_score", "?")
    total = timings.get("total_seconds", 0)
    print(f"  Grade: {grade}   Quality score: {score}/10   "
          f"Pipeline time: {total/60:.1f}min")
    if report.get("summary"):
        print(f"\n  {report['summary']}")

    actions = report.get("top_3_priority_actions", [])
    if actions:
        print("\n  TOP PRIORITIES:")
        for i, a in enumerate(actions, 1):
            print(f"    {i}. {a}")

    # Show high-priority recommendations
    all_recs = (
        report.get("prompt_recommendations", []) +
        report.get("output_recommendations", []) +
        report.get("content_recommendations", [])
    )
    high = [r for r in all_recs if r.get("priority") == "high"]
    if high:
        print(f"\n  HIGH PRIORITY ({len(high)}):")
        for r in high[:4]:
            stage = r.get("stage") or r.get("area", "")
            print(f"    [{stage}] {r.get('issue','')} → {r.get('suggestion','')}")

    # Timing bottlenecks
    if timings.get("available"):
        slow = [(k, v) for k, v in timings.get("stages", {}).items()
                if v.get("flag") == "⚠ SLOW"]
        if slow:
            print("\n  SLOW STAGES:")
            for k, v in slow:
                print(f"    Stage {k} ({v['stage']}): {v['seconds']}s "
                      f"(avg {v['avg_seconds']}s, +{v['delta_vs_avg']}s)")

    print(f"{'='*60}\n")


# ── Public API ─────────────────────────────────────────────────────────────────

def analyze(state: dict, state_path: Path):
    """
    Run the full optimizer analysis on a completed pipeline run.
    Prints a summary and saves the report to lessons_learned.json.
    """
    print(f"\n[Optimizer] 🔍 Analyzing pipeline run: {state.get('topic', '')}")

    past_states = _load_recent_states(state_path)
    timings     = _timing_analysis(state, past_states)
    quality     = _quality_analysis(state)
    trends      = _cross_run_trends(state, past_states)
    scoring     = _scoring_config_analysis(state)

    print(f"[Optimizer] Quality score: {quality.get('overall_score', '?')}/10  "
          f"Comparing against {trends.get('runs_available', 0)} past runs")

    report = _deep_analysis_with_claude(state, timings, quality, trends, scoring=scoring)

    if report:
        _print_summary(report, timings, quality)
        _save_report(state, report, timings, quality, scoring=scoring)
    else:
        print("[Optimizer] Deep analysis unavailable — basic quality check saved")
        _save_report(state, {}, timings, quality, scoring=scoring)
