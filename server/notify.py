"""
Notification system for The Obsidian Archive pipeline.
Supports Discord (webhook) and Telegram (bot).
Set env vars in .env — all functions fail silently if not configured.

  DISCORD_WEBHOOK_URL   — Discord channel webhook URL
  TELEGRAM_BOT_TOKEN    — from @BotFather
  TELEGRAM_CHAT_ID      — your personal chat ID (send /start to the bot then
                          visit https://api.telegram.org/bot<TOKEN>/getUpdates)
"""

import os
import json
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", interpolate=False)
WEBHOOK_URL   = os.getenv("DISCORD_WEBHOOK_URL", "")
TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")


# ── Telegram ───────────────────────────────────────────────────────────────────

def _tg(text: str, parse_mode: str = "Markdown") -> bool:
    """Send a Telegram message. Returns True on success."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text,
                  "parse_mode": parse_mode, "disable_web_page_preview": True},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False

def _send(payload: dict) -> bool:
    """POST to Discord webhook. Returns True on success."""
    if not WEBHOOK_URL:
        return False
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        return r.status_code in (200, 204)
    except Exception:
        return False

def notify_pipeline_start(topic: str) -> None:
    """Notify when pipeline starts."""
    _send({"embeds": [{"title": "⚡ Pipeline Started",
        "description": f"**Topic:** {topic}",
        "color": 0x8B5CF6, "footer": {"text": "The Obsidian Archive"}}]})
    _tg(f"⚡ *Pipeline Started*\n`{topic}`")

def notify_pipeline_complete(topic: str, title: str, youtube_url: str, elapsed_minutes: float, short_url: str = "") -> None:
    """Notify on successful pipeline completion with cost and structure details."""
    desc = f"**Topic:** {topic}\n**Title:** {title}\n**Duration:** {elapsed_minutes:.1f} min"
    if youtube_url:
        desc += f"\n**Long Video:** {youtube_url}"
    if short_url:
        desc += f"\n**Short:** {short_url}"
    _send({"embeds": [{"title": "✅ Pipeline Complete",
        "description": desc, "color": 0x10B981,
        "footer": {"text": "The Obsidian Archive"}}]})

    tg = f"✅ *Pipeline Complete* — {elapsed_minutes:.1f} min\n*{title}*"
    if youtube_url:
        tg += f"\n[Watch]({youtube_url})"
    if short_url:
        tg += f"  |  [Short]({short_url})"

    # Add cost and structure details from latest state file
    try:
        outputs_dir = Path(__file__).resolve().parent.parent / "outputs"
        state_files = sorted(outputs_dir.glob("*_state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if state_files:
            with open(state_files[0]) as f:
                state = json.load(f)
            costs = state.get("costs", {})
            cost = costs.get("usd_total") if isinstance(costs, dict) else None
            if cost is not None:
                tg += f"\n💰 Cost: ${float(cost):.2f}"
            # Structure summary from pipeline state
            ps = state.get("pipeline_state") or state
            stage_3 = ps.get("stage_3") or {}
            stage_4 = ps.get("stage_4") or {}
            structure = stage_3.get("structure_type", "")
            word_count = stage_4.get("word_count", 0)
            if structure:
                tg += f"\n📐 Structure: {structure}"
            if word_count:
                tg += f" | {word_count} words"
    except Exception:
        pass

    _tg(tg)

def notify_pipeline_failed(topic: str, stage: str, error: str) -> None:
    """Notify on pipeline failure."""
    _send({"embeds": [{"title": "❌ Pipeline Failed",
        "description": f"**Topic:** {topic}\n**Stage:** {stage}\n**Error:** {error[:500]}",
        "color": 0xEF4444, "footer": {"text": "The Obsidian Archive"}}]})
    _tg(f"❌ *Pipeline Failed*\nStage: {stage}\n`{error[:300]}`")

def notify_quality_warn(stage: str, warnings: list) -> None:
    """Notify on quality warnings (only if >= 3 warnings)."""
    if len(warnings) < 3:
        return
    bullet = "\n".join(f"• {w}" for w in warnings[:10])
    _send({"embeds": [{"title": f"⚠️ Quality Warnings — {stage}",
        "description": bullet, "color": 0xF59E0B,
        "footer": {"text": "The Obsidian Archive"}}]})
    _tg(f"⚠️ *Quality Warnings — {stage}*\n{len(warnings)} warning(s):\n" +
        "\n".join(f"• {w}" for w in warnings[:10]))

def notify_error_spike(agent: str, error_type: str, count: int, sample_trace: str):
    """Telegram + Discord alert when errors spike."""
    severity = "\U0001f534 CRITICAL" if count >= 10 else "\U0001f7e0 SPIKE"
    _tg(
        f"{severity} *Error Spike: {agent}*\n"
        f"`{error_type}` \u2014 {count} occurrence(s)\n"
        f"```\n{sample_trace[:400]}\n```"
    )
    _send({"embeds": [{"title": f"{severity} Error Spike: {agent}",
        "description": f"**{error_type}** \u2014 {count} time(s)\n```\n{sample_trace[:500]}\n```",
        "color": 0xEF4444 if count >= 10 else 0xFF8C00,
        "footer": {"text": "The Obsidian Archive"}}]})


def notify_short_complete(short_title: str, short_url: str) -> None:
    """Notify when short video is uploaded."""
    _send({"embeds": [{"title": "📱 Short Uploaded",
        "description": f"**Title:** {short_title}\n**URL:** {short_url}",
        "color": 0xF472B6, "footer": {"text": "The Obsidian Archive"}}]})
    _tg(f"📱 *Short Uploaded*\n*{short_title}*\n{short_url}")


def notify_trend_alert(topic: str, score: float, sources: list) -> None:
    """Notify when a trending topic is detected."""
    sources_str = ", ".join(sources or [])
    _send({"embeds": [{"title": "🔎 Trend Alert",
        "description": f"**Topic:** {topic}\n**Score:** {score:.2f}\n**Sources:** {sources_str}",
        "color": 0x06B6D4, "footer": {"text": "The Obsidian Archive"}}]})
    _tg(f"🔎 *Trend Alert*\n`{topic}`\nScore: {score:.2f} | Sources: {sources_str}")


def notify_community_teaser(topic: str, title: str, upload_eta_hours: int = 24):
    """Send community post teaser text to Telegram for manual posting."""
    teaser = (
        "\U0001f4e2 *Community Post Teaser*\n\n"
        f"New video dropping in ~{upload_eta_hours}h:\n"
        f"_{title}_\n\n"
        f"Suggested poll question:\n"
        "\"Which part of this story shocks you most?\"\n\n"
        "Copy this to YouTube Community tab."
    )
    _tg(teaser)


def send_weekly_report() -> bool:
    """
    Build and send the weekly summary to Telegram (and Discord).
    Pulls data from: Supabase videos table, lessons_learned.json,
    ElevenLabs credit API, and the Supabase topics queue.
    """
    from datetime import datetime, timedelta, timezone
    lines = []
    lines.append("📊 *OBSIDIAN ARCHIVE — WEEKLY REPORT*")
    lines.append(f"_{datetime.now(timezone.utc).strftime('%a %d %b %Y')}_\n")

    # ── Videos published this week ────────────────────────────────────────────
    try:
        from clients.supabase_client import get_client
        client = get_client()
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        vids = client.table("videos").select("title,youtube_url,created_at") \
                     .gte("created_at", week_ago).order("created_at", desc=True).execute()
        rows = vids.data or []
        if rows:
            lines.append(f"🎬 *Videos this week: {len(rows)}*")
            for v in rows:
                title = v.get("title") or "Untitled"
                url   = v.get("youtube_url", "")
                lines.append(f"  • [{title}]({url})" if url else f"  • {title}")
        else:
            lines.append("🎬 *Videos this week: 0*")
    except Exception as e:
        lines.append(f"🎬 Videos: unavailable ({e})")

    # ── YouTube analytics (latest available) ─────────────────────────────────
    try:
        from clients.supabase_client import get_client
        client = get_client()
        anl = client.table("analytics").select("views,watch_time_hours,recorded_at") \
                    .order("recorded_at", desc=True).limit(1).execute()
        if anl.data:
            a = anl.data[0]
            lines.append(
                f"\n📈 *Channel Analytics*\n"
                f"  Views: {a.get('views', 0):,}\n"
                f"  Watch time: {a.get('watch_time_hours', 0):.1f}h"
            )
    except Exception:
        pass

    # ── Optimizer grades this week ────────────────────────────────────────────
    try:
        lessons_path = Path(__file__).resolve().parent.parent / "lessons_learned.json"
        if lessons_path.exists():
            with open(lessons_path) as f:
                lessons = json.load(f)
            week_ago_str = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            runs = [r for r in (lessons.get("optimizer_runs") or [])
                    if r.get("timestamp", "") >= week_ago_str]
            if runs:
                grade_emoji = {"A": "🟢", "B": "🔵", "C": "🟡", "D": "🟠", "F": "🔴"}
                lines.append("\n🧠 *Optimizer Grades*")
                for r in runs:
                    g = r.get("overall_grade", "?")
                    lines.append(f"  {grade_emoji.get(g,'⚪')} {g} — {(r.get('topic') or '')[:40]}")
                    actions = r.get("top_3_priority_actions") or []
                    if actions:
                        lines.append(f"    → {actions[0]}")

            # Doctor interventions this week
            ivs = [i for i in (lessons.get("doctor_interventions") or [])
                   if i.get("timestamp", "") >= week_ago_str]
            if ivs:
                fixed   = sum(1 for i in ivs if "success" in i.get("outcome", ""))
                failed  = len(ivs) - fixed
                lines.append(
                    f"\n🩺 *Doctor Interventions:* {len(ivs)} total "
                    f"({fixed} fixed, {failed} failed)"
                )
                # Show any failures
                for iv in ivs:
                    if "failed" in iv.get("outcome", "") or "aborted" in iv.get("outcome", ""):
                        lines.append(f"  ✗ Stage {iv.get('stage_num')} — {iv.get('stage_name')}: {iv.get('category')}")
    except Exception as e:
        lines.append(f"\n🧠 Optimizer/Doctor: unavailable ({e})")

    # ── ElevenLabs credits ────────────────────────────────────────────────────
    try:
        elevenlabs_key = os.getenv("ELEVENLABS_API_KEY", "")
        if elevenlabs_key:
            r = requests.get("https://api.elevenlabs.io/v1/user",
                             headers={"xi-api-key": elevenlabs_key}, timeout=10)
            if r.status_code == 200:
                sub       = json.loads(r.text, strict=False).get("subscription", {})
                limit     = sub.get("character_limit", 1)
                used      = sub.get("character_count", 0)
                remaining = limit - used
                pct_used  = (used / limit) * 100 if limit else 0
                bar       = "█" * int(pct_used / 10) + "░" * (10 - int(pct_used / 10))
                status    = "🔴" if remaining < 50000 else "🟡" if remaining < 150000 else "🟢"
                lines.append(
                    f"\n{status} *ElevenLabs Credits*\n"
                    f"  `{bar}` {pct_used:.0f}% used\n"
                    f"  {remaining:,} chars remaining"
                )
    except Exception:
        pass

    # ── Music analytics ────────────────────────────────────────────────────
    try:
        insights_path = Path(__file__).resolve().parent.parent / "channel_insights.json"
        if insights_path.exists():
            with open(insights_path) as f:
                _music_insights = json.load(f)
            mp = _music_insights.get("music_performance", {})
            if mp and mp.get("sample_size", 0) >= 3:
                lines.append("\n🎵 *Music Analytics*")
                mood_perf = mp.get("mood_performance", {})
                if mood_perf:
                    best_mood = max(mood_perf, key=lambda m: mood_perf[m].get("avg_retention", 0))
                    lines.append(f"  Best mood: {best_mood} ({mood_perf[best_mood]['avg_retention']}% retention)")
                src = mp.get("source_distribution", {})
                if src:
                    lines.append(f"  Sources: {', '.join(f'{k}={v}' for k, v in src.items())}")
                adapt = mp.get("adaptation_impact", {})
                if adapt and adapt.get("adapted_count", 0) > 0:
                    lines.append(f"  Adapted: {adapt['adapted_count']} videos ({adapt.get('adapted_avg_retention', 0):.1f}% retention)")
                recs = mp.get("recommendations", [])
                if recs:
                    lines.append(f"  💡 {recs[0]}")

        # Epidemic Sound API key status
        es_key = os.getenv("EPIDEMIC_SOUND_API_KEY", "")
        if es_key:
            try:
                from clients.epidemic_client import EpidemicSoundClient
                if not EpidemicSoundClient().check_key_valid():
                    lines.append("\n⚠️ *Epidemic Sound API key expired* — regenerate at epidemicsound.com")
            except Exception:
                pass
    except Exception:
        pass

    # ── Queue depth ───────────────────────────────────────────────────────────
    try:
        from clients.supabase_client import get_client
        client = get_client()
        q = client.table("topics").select("id", count="exact") \
                  .eq("status", "queued").execute()
        count = q.count or len(q.data or [])
        lines.append(f"\n📋 *Queue:* {count} topics ready")
    except Exception:
        pass

    # ── Cost data from latest state file ────────────────────────────────────
    try:
        outputs_dir = Path(__file__).resolve().parent.parent / "outputs"
        state_files = sorted(outputs_dir.glob("*_state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if state_files:
            with open(state_files[0]) as f:
                state = json.load(f)
            costs = state.get("costs", {})
            cost = costs.get("usd_total") if isinstance(costs, dict) else None
            if cost is None:
                cost = state.get("total_cost") or state.get("cost_usd") or state.get("cost")
            if cost is not None:
                lines.append(f"\n💰 *Latest Run Cost:* ${float(cost):.2f}")
    except Exception:
        pass

    # ── Failed / dead-letter topics ──────────────────────────────────────────
    try:
        from clients.supabase_client import get_client
        client = get_client()
        failed_q = client.table("topics").select("id", count="exact") \
                         .eq("status", "failed").execute()
        failed_count = failed_q.count or len(failed_q.data or [])
        dl_q = client.table("topics").select("id", count="exact") \
                     .eq("status", "dead_letter").execute()
        dl_count = dl_q.count or len(dl_q.data or [])
        if failed_count or dl_count:
            lines.append(
                f"\n⚠️ *Failed Topics:* {failed_count}  |  *Dead-letter:* {dl_count}"
            )
    except Exception:
        pass

    # ── Subscriber count & growth ────────────────────────────────────────────
    try:
        insights_path = Path(__file__).resolve().parent.parent / "channel_insights.json"
        if insights_path.exists():
            with open(insights_path) as f:
                insights = json.load(f)

            ch_stats = insights.get("channel_stats", {})
            if ch_stats:
                subs = ch_stats.get("subscriber_count", 0)
                views = ch_stats.get("total_views", 0)
                lines.append(
                    f"\n👥 *Subscribers:* {subs:,}  |  *Total Views:* {views:,}"
                )

            # Comment sentiment summary
            sentiment = insights.get("comment_sentiment", {})
            if sentiment:
                total_c = sentiment.get("total_comments", 0)
                pos = sentiment.get("positive_pct", 0)
                neg = sentiment.get("negative_pct", 0)
                reqs = sentiment.get("audience_topic_requests", [])
                lines.append(
                    f"\n💬 *Comment Sentiment* ({total_c} comments)\n"
                    f"  👍 {pos}% positive  |  👎 {neg}% negative"
                )
                if reqs:
                    lines.append("  Audience requests: " + ", ".join(reqs[:3]))

            # Content quality intelligence summary
            cqc = insights.get("content_quality_correlation", {})
            if cqc and cqc.get("sample_size", 0) >= 2:
                claude_analysis = cqc.get("claude_analysis", {})
                strongest = claude_analysis.get("strongest_signals", [])
                if strongest:
                    lines.append("\n🎯 *Content Quality Insights*")
                    for signal in strongest[:3]:
                        lines.append(f"  • {signal}")
                recs = claude_analysis.get("agent_recommendations", {})
                if recs:
                    lines.append("\n📝 *What the data says:*")
                    if recs.get("narrative_architect"):
                        lines.append(f"  _Structure:_ {recs['narrative_architect']}")
                    if recs.get("script_writer"):
                        lines.append(f"  _Script:_ {recs['script_writer']}")
                surprises = claude_analysis.get("surprising_findings", [])
                if surprises:
                    lines.append(f"  ⚡ {surprises[0]}")

            # DNA confidence summary
            dna_conf = insights.get("dna_confidence_updates", {})
            dna_reason = insights.get("dna_reasoning", {})
            if dna_conf:
                dna_labels = {
                    "open_mid_action_hook": "Mid-action hook",
                    "twist_reveal_ending": "Twist ending",
                    "present_tense_narration": "Present tense",
                    "dark_thumbnail_aesthetic": "Dark thumbnails",
                    "10_15_min_standard_length": "10-15 min length",
                    "ancient_medieval_priority": "Ancient/medieval",
                }
                lines.append("\n🧬 *Creative DNA Confidence*")
                for key, label in dna_labels.items():
                    score = dna_conf.get(key)
                    if score is not None:
                        pct = int(score * 100)
                        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                        reason = dna_reason.get(key, "")
                        line = f"  `{bar}` {pct}% {label}"
                        if reason:
                            line += f"\n    _{reason}_"
                        lines.append(line)
    except Exception:
        pass

    # ── Shorts published this week ───────────────────────────────────────────
    try:
        from clients.supabase_client import get_client
        client = get_client()
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        # Shorts are identified by duration (< 65s) since there's no is_short column
        shorts_q = client.table("videos").select("title,youtube_url,created_at,duration_seconds") \
                         .lt("duration_seconds", 65) \
                         .gte("created_at", week_ago) \
                         .order("created_at", desc=True).execute()
        shorts_rows = shorts_q.data or []
        if shorts_rows:
            lines.append(f"\n📱 *Shorts this week: {len(shorts_rows)}*")
            for s in shorts_rows[:5]:
                s_title = s.get("title") or "Untitled Short"
                s_url = s.get("youtube_url", "")
                lines.append(f"  • [{s_title}]({s_url})" if s_url else f"  • {s_title}")
    except Exception:
        pass

    lines.append("\n_The Obsidian Archive_")
    message = "\n".join(lines)

    # Send to Telegram
    tg_ok = _tg(message)

    # Also send condensed version to Discord
    try:
        _send({"embeds": [{"title": "📊 Weekly Report",
            "description": message.replace("*", "**")[:3900],
            "color": 0x8B5CF6, "footer": {"text": "The Obsidian Archive"}}]})
    except Exception:
        pass

    return tg_ok
