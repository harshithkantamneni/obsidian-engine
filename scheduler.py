#!/usr/bin/env python3
"""
The Obsidian Archive — Scheduler
Usage:
  python3 scheduler.py --once       # run one video now
  python3 scheduler.py --discover   # discover topics only
  python3 scheduler.py --daemon     # run forever (Railway)
  python3 scheduler.py --status     # show queue + recent videos
  python3 scheduler.py --health     # run health check now
"""
import sys
import os
import json
import time
import subprocess
import glob
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from intel import youtube_growth

from core.json_compat import apply_lenient_json
apply_lenient_json()

load_dotenv(dotenv_path=Path(__file__).parent / ".env", interpolate=False)
sys.path.append(str(Path(__file__).parent))

# Allow OAuth over HTTP on Railway (internal proxy handles HTTPS externally)
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# Restore YouTube token from env var (Railway secret)
_token_json = os.getenv("YOUTUBE_TOKEN_JSON", "")
if _token_json:
    token_path = Path(__file__).parent / "youtube_token.json"
    token_path.write_text(_token_json)

VIDEOS_PER_WEEK = 1
PUBLISH_DAYS    = ["tuesday"]  # Reduced to 1x/week until ElevenLabs resets April 7th.
# PUBLISH_DAYS  = ["tuesday", "friday"]  # Restore when quota resets
PUBLISH_TIME    = "09:00"  # Default fallback — overridden by get_optimal_publish_time() if data exists
DISCOVER_TIME   = "08:00"

INSIGHTS_FILE = Path(__file__).parent / "channel_insights.json"

# ── Pending post-upload tasks (video_id -> list of scheduled actions) ─────────
_pending_post_upload = {}


# ── Helper: escape Markdown for Telegram ──────────────────────────────────────

def _md_escape(text: str) -> str:
    """Escape Markdown special characters for Telegram messages."""
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, f'\\{ch}')
    return text


# ── Helper: atomic JSON write ─────────────────────────────────────────────────

def _atomic_write_json(path: Path, data: dict):
    """Write JSON atomically via temp file + rename to prevent corruption."""
    import tempfile
    path = Path(path).resolve()  # follow symlinks into the Docker ./data mount
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ══════════════════════════════════════════════════════════════════════════════
# 1. ENHANCED PUBLISH TIME OPTIMIZATION
# ══════════════════════════════════════════════════════════════════════════════


def _compute_optimal_publish_window(insights: dict) -> tuple:
    """
    Returns (best_day, best_hour, confidence) based on multi-signal analysis.

    Signals analyzed:
    - Traffic source mix (browse-heavy vs search-heavy)
    - Day-of-week correlation with first_48h_velocity
    - Audience timezone / demographics
    - Seasonal performance patterns
    """
    best_day = 2   # Wednesday default
    best_hour = 14  # 2 PM default
    confidence = "low"

    try:
        top_videos = insights.get("top_performing_videos", [])
        retention = insights.get("retention_analysis", {})

        # ── Signal 1: Traffic source awareness ────────────────────────────
        # If browse-heavy, optimize for peak browse hours (evening/weekend).
        # If search-heavy, time matters less (search is 24/7).
        traffic_sources = insights.get("traffic_sources", {})
        browse_pct = traffic_sources.get("browse", {}).get("pct", 0)
        search_pct = traffic_sources.get("search", {}).get("pct", 0)

        if browse_pct > 40:
            # Browse-heavy: evening prime time (6-9 PM) performs best
            best_hour = 18
            print("[Scheduler] Traffic source signal: browse-heavy channel, targeting evening prime time")
        elif search_pct > 40:
            # Search-heavy: time matters less, but afternoon still optimal
            best_hour = 14
            print("[Scheduler] Traffic source signal: search-heavy channel, time less critical")

        # ── Signal 2: Day-of-week analysis via first_48h_velocity ─────────
        day_velocity = {}
        for v in top_videos:
            pub_day = v.get("publish_day_of_week")
            velocity = v.get("first_48h_velocity", 0) or v.get("views", 0)
            if pub_day is not None and velocity > 0:
                if pub_day not in day_velocity:
                    day_velocity[pub_day] = {"total_velocity": 0, "count": 0}
                day_velocity[pub_day]["total_velocity"] += velocity
                day_velocity[pub_day]["count"] += 1

        if day_velocity:
            best_day_by_velocity = max(
                day_velocity,
                key=lambda d: day_velocity[d]["total_velocity"] / day_velocity[d]["count"]
            )
            best_day = best_day_by_velocity
            if day_velocity[best_day]["count"] >= 3:
                confidence = "moderate"
            print(f"[Scheduler] Day-of-week signal: day {best_day} has highest velocity "
                  f"(n={day_velocity[best_day]['count']})")

        # ── Signal 3: Audience timezone optimization ──────────────────────
        demographics = insights.get("demographics", {})
        primary_region = demographics.get("primary_region", "")

        # Optimize for audience timezone prime time (7-9 PM local)
        region_offsets = {
            "US_EAST": -5,    # EST: 7PM EST = 00:00 UTC next day
            "US_WEST": -8,    # PST: 7PM PST = 03:00 UTC next day
            "INDIA": 5.5,     # IST: 7PM IST = 13:30 UTC
            "UK": 0,          # GMT: 7PM GMT = 19:00 UTC
            "EUROPE": 1,      # CET: 7PM CET = 18:00 UTC
        }
        if primary_region and primary_region.upper() in region_offsets:
            offset = region_offsets[primary_region.upper()]
            # Target 7 PM local = 19:00 local
            utc_hour = int((19 - offset) % 24)
            best_hour = utc_hour
            print(f"[Scheduler] Audience timezone signal: {primary_region} -> targeting {best_hour:02d}:00 UTC")
        elif not primary_region:
            # Default assumption: Indian audience (IST = UTC+5:30)
            # 7 PM IST = 13:30 UTC, round to 14:00
            if not traffic_sources:  # don't override traffic source signal
                best_hour = 14

        # ── Signal 4: Seasonal adjustment ─────────────────────────────────
        # History content may spike around school seasons (Aug-Oct, Jan-Mar)
        current_month = datetime.now(timezone.utc).month
        school_months = {1, 2, 3, 8, 9, 10}  # school season months
        if current_month in school_months:
            # During school season, earlier publish captures student audience
            if best_hour > 15:
                best_hour = max(best_hour - 2, 12)
                print(f"[Scheduler] Seasonal signal: school season, shifting 2h earlier to {best_hour:02d}:00")

        # ── Check explicit publish_time_analysis (overrides heuristics) ───
        pub_analysis = retention.get("publish_time_analysis", {})
        if pub_analysis:
            explicit_day = pub_analysis.get("best_day_of_week")
            explicit_hour = pub_analysis.get("best_hour")
            if explicit_day is not None and explicit_hour is not None:
                best_day = int(explicit_day)
                best_hour = int(explicit_hour)
                confidence = "high"
                print(f"[Scheduler] Explicit publish_time_analysis overrides: day={best_day}, hour={best_hour}")

        # ── Confidence assessment (skip if already set to high by explicit override) ──
        if confidence != "high":
            n_videos = insights.get("data_quality", {}).get("videos_analyzed", 0)
            if n_videos >= 10 and len(day_velocity) >= 3:
                confidence = "high"
            elif n_videos >= 5:
                confidence = "moderate"

    except Exception as e:
        print(f"[Scheduler] Optimal publish window computation error: {e}")

    return (best_day, best_hour, confidence)


def get_optimal_publish_time() -> tuple:
    """
    Load channel_insights.json and determine the optimal publish time.
    Returns (day_of_week: int, hour: int, minute: int) tuple.
    day_of_week: 0=Monday, 1=Tuesday, ..., 6=Sunday
    Falls back to Wednesday 2:00 PM EST (historically optimal for educational content).
    """
    try:
        from intel.channel_insights import load_insights
        insights = load_insights()
        if not insights:
            print("[Scheduler] No channel insights -- using default publish time")
            return (2, 14, 0)  # Wednesday 2:00 PM

        # Use multi-signal analysis
        best_day, best_hour, confidence = _compute_optimal_publish_window(insights)
        print(f"[Scheduler] Optimal publish window: day={best_day}, hour={best_hour:02d}:00, confidence={confidence}")
        return (best_day, best_hour, 0)

    except Exception as e:
        print(f"[Scheduler] Optimal publish time analysis failed: {e}")

    # Default fallback: Wednesday 2:00 PM EST
    print("[Scheduler] Using default optimal publish time: Wednesday 14:00")
    return (2, 14, 0)


def adjust_schedule_from_data():
    """
    Called at scheduler startup. Read insights, determine optimal publish window,
    update the schedule configuration. Prints the adjustment.
    """
    global PUBLISH_DAYS, PUBLISH_TIME

    DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    original_days = list(PUBLISH_DAYS)
    original_time = PUBLISH_TIME

    try:
        day_of_week, hour, minute = get_optimal_publish_time()

        # Update publish time
        PUBLISH_TIME = f"{hour:02d}:{minute:02d}"

        # Update publish day — keep the same number of publish days, but shift to optimal day
        optimal_day = DAY_NAMES[day_of_week]
        if len(PUBLISH_DAYS) == 1:
            PUBLISH_DAYS = [optimal_day]
        elif len(PUBLISH_DAYS) >= 2:
            # Keep two days: optimal day + 3 days later (spread across the week)
            second_day = DAY_NAMES[(day_of_week + 3) % 7]
            PUBLISH_DAYS = [optimal_day, second_day]

        if PUBLISH_DAYS != original_days or PUBLISH_TIME != original_time:
            print("[Scheduler] Schedule adjusted from data:")
            print(f"  Days: {original_days} -> {PUBLISH_DAYS}")
            print(f"  Time: {original_time} -> {PUBLISH_TIME}")
        else:
            print(f"[Scheduler] Schedule unchanged: {PUBLISH_DAYS} at {PUBLISH_TIME}")

    except Exception as e:
        print(f"[Scheduler] Schedule adjustment failed (keeping defaults): {e}")

# ── Era classification for topic sequencing ──────────────────────────


def _get_channel_avg_ctr() -> float:
    """Read channel average CTR from channel_insights.json. Falls back to 4.0%."""
    try:
        from intel.channel_insights import load_insights
        insights = load_insights()
        avg_ctr = insights.get("channel_health", {}).get("avg_ctr_pct", 0)
        if avg_ctr > 0:
            return avg_ctr
    except Exception:
        pass
    return 4.0


def _get_channel_avg_views() -> float:
    """Read channel average views from channel_insights.json. Falls back to 0."""
    try:
        from intel.channel_insights import load_insights
        insights = load_insights()
        return insights.get("channel_health", {}).get("avg_views_per_video", 0)
    except Exception:
        return 0


def _recent_experiments_underperformed(client, n: int = 3) -> bool:
    """
    Check if the last `n` experiment videos all underperformed
    (views < 50% of channel average). Returns True if so.
    """
    avg_views = _get_channel_avg_views()
    if avg_views <= 0:
        return False  # not enough data to judge
    try:
        # Experiment videos have pipeline_state containing "experiment": true
        vids = client.table("videos").select("id, pipeline_state")\
            .order("created_at", desc=True).limit(50).execute()
        exp_ids = []
        for v in (vids.data or []):
            ps = v.get("pipeline_state") or {}
            if isinstance(ps, str):
                try:
                    ps = json.loads(ps)
                except Exception:
                    ps = {}
            if ps.get("experiment") or ps.get("is_experiment"):
                exp_ids.append(v["id"])
            if len(exp_ids) >= n:
                break
        if len(exp_ids) < n:
            return False  # not enough experiment history
        threshold = avg_views * 0.5
        for vid_id in exp_ids:
            row = client.table("analytics").select("views")\
                .eq("video_id", vid_id).limit(1).execute()
            views = (row.data[0]["views"] if row.data else 0) or 0
            if views >= threshold:
                return False  # at least one experiment performed OK
        return True  # all n experiments underperformed
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# 4. DYNAMIC EXPERIMENT CADENCE
# ══════════════════════════════════════════════════════════════════════════════


def _compute_experiment_cadence(insights: dict, client) -> int:
    """
    Dynamic experiment frequency based on channel state.

    Factors:
    - Channel growth rate: growing fast? experiment more (every 4th)
    - Recent experiment performance: last 3 experiments did well? keep cadence
    - Content diversity: if all recent videos are same era, force experiment sooner
    - Queue depth: deep queue? can afford more experiments
    - Subscriber velocity: gaining subs fast? audience is forgiving of experiments

    Returns: N (experiment every Nth video), range 3-10
    """
    cadence = 5  # default baseline

    try:
        channel_health = insights.get("channel_health", {})

        # ── Factor 1: Subscriber velocity ─────────────────────────────────
        # If gaining subs fast, audience is engaged and forgiving of experiments
        avg_subs_per_video = channel_health.get("avg_subscribers_gained_per_video", 0)
        if avg_subs_per_video >= 10:
            cadence -= 1  # experiment more often
            print("[Scheduler] Experiment cadence: high sub gain -> more experiments")
        elif avg_subs_per_video <= 1:
            cadence += 1  # play it safe
            print("[Scheduler] Experiment cadence: low sub gain -> fewer experiments")

        # ── Factor 2: Recent experiment performance ───────────────────────
        try:
            if client and _recent_experiments_underperformed(client, n=3):
                cadence += 2
                print("[Scheduler] Experiment cadence: last 3 experiments underperformed -> throttling")
        except Exception:
            pass

        # ── Factor 3: Content diversity (era monotony check) ──────────────
        try:
            if client:
                recent = client.table("videos").select("topic").order("created_at", desc=True).limit(10).execute()
                recent_topics = [v.get("topic", "") for v in (recent.data or [])]
                if recent_topics:
                    from intel.era_classifier import classify_era
                    recent_eras = [classify_era(t) for t in recent_topics]
                    unique_eras = set(recent_eras)
                    if len(unique_eras) <= 2 and len(recent_eras) >= 5:
                        cadence -= 1  # force diversity
                        print(f"[Scheduler] Experiment cadence: only {len(unique_eras)} eras in last {len(recent_eras)} videos -> experiment sooner")
        except Exception:
            pass

        # ── Factor 4: Queue depth ─────────────────────────────────────────
        try:
            if client:
                q = client.table("topics").select("id", count="exact").eq("status", "queued").execute()
                queue_depth = q.count if hasattr(q, 'count') and q.count else len(q.data or [])
                if queue_depth >= 15:
                    cadence -= 1  # deep queue, can afford experiments
                    print(f"[Scheduler] Experiment cadence: deep queue ({queue_depth}) -> more experiments")
                elif queue_depth <= 3:
                    cadence += 1  # shallow queue, be conservative
                    print(f"[Scheduler] Experiment cadence: shallow queue ({queue_depth}) -> fewer experiments")
        except Exception:
            pass

        # ── Factor 5: Channel growth rate (views trending up/down) ────────
        avg_views = channel_health.get("avg_views_per_video", 0)
        top_quartile = channel_health.get("top_quartile_view_threshold", 0)
        if top_quartile > 0 and avg_views > 0:
            # If top quartile is much higher than average, there's high variance
            # which means experiments have upside
            if top_quartile > avg_views * 2:
                cadence -= 1
                print("[Scheduler] Experiment cadence: high view variance -> experiments have upside")

    except Exception as e:
        print(f"[Scheduler] Experiment cadence computation error: {e}")

    # Clamp to valid range
    cadence = max(3, min(10, cadence))
    print(f"[Scheduler] Dynamic experiment cadence: every {cadence}th video")
    return cadence


# ══════════════════════════════════════════════════════════════════════════════
# 2. RE-ENGAGEMENT SIGNAL DETECTION
# ══════════════════════════════════════════════════════════════════════════════


def check_reengagement_opportunities():
    """
    Identify videos that could benefit from title/thumbnail refresh.

    Signals:
    - Video with rising impressions but declining CTR -> thumbnail/title is stale
    - Video with high search traffic but low CTR -> title doesn't match search intent
    - Old video getting sudden traffic spike -> trending topic, add cards/endscreen
    - Video with high retention but low views -> distribution problem, not content problem
    """
    print(f"\n{'='*60}\n  RE-ENGAGEMENT CHECK -- {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*60}")
    try:
        from intel.channel_insights import load_insights
        from server.notify import _tg

        insights = load_insights()
        if not insights:
            print("[Scheduler] No insights available for re-engagement check")
            return

        channel_health = insights.get("channel_health", {})
        avg_views = channel_health.get("avg_views_per_video", 0)
        avg_ctr = channel_health.get("avg_ctr_pct", 0)
        avg_retention = channel_health.get("avg_retention_pct", 0)

        if avg_views <= 0:
            print("[Scheduler] Not enough data for re-engagement analysis")
            return

        opportunities = []

        # Check Supabase for video set
        try:
            from clients.supabase_client import get_client
            client = get_client()
            result = client.table("videos").select(
                "id, title, youtube_id, created_at"
            ).order("created_at", desc=True).limit(30).execute()
            all_db_videos = result.data or []

            # Get analytics for each
            for v in all_db_videos:
                yt_id = v.get("youtube_id", "")
                if not yt_id:
                    continue

                anl = client.table("analytics").select("views, ctr_pct, avg_view_percentage, impressions")\
                    .eq("video_id", v["id"]).limit(1).execute()
                if not anl.data:
                    continue

                a = anl.data[0]
                views = a.get("views", 0) or 0
                ctr = a.get("ctr_pct", 0) or 0
                retention = a.get("avg_view_percentage", 0) or 0
                impressions = a.get("impressions", 0) or 0
                title = v.get("title", "Unknown")
                studio_link = f"https://studio.youtube.com/video/{yt_id}/edit"

                # Signal: High retention but low views -> distribution problem
                if retention > avg_retention * 1.2 and views < avg_views * 0.5 and views > 0:
                    opportunities.append({
                        "video_title": title,
                        "youtube_id": yt_id,
                        "studio_link": studio_link,
                        "signal": "high_retention_low_views",
                        "action": "Thumbnail/title refresh -- content is good but not getting clicks",
                        "metrics": f"Retention: {retention:.0f}% (avg {avg_retention:.0f}%), Views: {views} (avg {avg_views:.0f})",
                    })

                # Signal: Rising impressions but low CTR -> stale thumbnail
                if impressions > 0 and ctr > 0 and ctr < avg_ctr * 0.7:
                    opportunities.append({
                        "video_title": title,
                        "youtube_id": yt_id,
                        "studio_link": studio_link,
                        "signal": "low_ctr_with_impressions",
                        "action": "Title/thumbnail not compelling enough for the impressions received",
                        "metrics": f"CTR: {ctr:.1f}% (avg {avg_ctr:.1f}%), Impressions: {impressions}",
                    })

                # Signal: High views but low retention -> hook problem
                if views > avg_views * 1.5 and retention > 0 and retention < avg_retention * 0.7:
                    opportunities.append({
                        "video_title": title,
                        "youtube_id": yt_id,
                        "studio_link": studio_link,
                        "signal": "high_views_low_retention",
                        "action": "Good distribution but losing viewers -- consider adding chapters or cards to related content",
                        "metrics": f"Views: {views} (avg {avg_views:.0f}), Retention: {retention:.0f}% (avg {avg_retention:.0f}%)",
                    })

        except Exception as e:
            print(f"[Scheduler] Re-engagement DB check error: {e}")

        # Store opportunities in channel_insights.json
        if opportunities:
            try:
                current_insights = json.loads(INSIGHTS_FILE.read_text()) if INSIGHTS_FILE.exists() else {}
                current_insights["reengagement_opportunities"] = {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "opportunities": opportunities[:10],  # cap at 10
                }
                _atomic_write_json(INSIGHTS_FILE, current_insights)
            except Exception as e:
                print(f"[Scheduler] Could not store re-engagement data: {e}")

            # Send Telegram notification
            lines = ["🔄 *Re-engagement Opportunities*\n"]
            for opp in opportunities[:5]:
                lines.append(
                    f"*{_md_escape(opp['video_title'][:50])}*\n"
                    f"  Signal: {opp['signal'].replace('_', ' ')}\n"
                    f"  Action: {opp['action']}\n"
                    f"  {opp['metrics']}\n"
                    f"  [Studio]({opp['studio_link']})\n"
                )
            _tg("\n".join(lines))
            print(f"[Scheduler] Found {len(opportunities)} re-engagement opportunities")
        else:
            print("[Scheduler] No re-engagement opportunities found")

    except Exception as e:
        print(f"[Scheduler] Re-engagement check error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 5. POST-UPLOAD AUTOMATION
# ══════════════════════════════════════════════════════════════════════════════


def run_post_upload_sequence(video_id: str, upload_time: datetime):
    """
    Scheduled sequence of post-upload actions:

    T+0h:   Community post teaser (already exists in pipeline)
    T+1h:   Check first-hour metrics (if available)
    T+24h:  First-day performance check + Telegram report
    T+48h:  A/B title test (already exists)
    T+48h:  Tag optimization (already exists)
    T+168h: One-week performance report + Telegram summary
    """
    try:
        # Register pending tasks
        _pending_post_upload[video_id] = {
            "upload_time": upload_time.isoformat(),
            "tasks": {
                "first_hour": False,
                "first_day": False,
                "one_week": False,
            }
        }
        print(f"[Scheduler] Post-upload sequence registered for {video_id}")

    except Exception as e:
        print(f"[Scheduler] Post-upload sequence registration error: {e}")


def _check_post_upload_tasks():
    """Check and execute any pending post-upload tasks. Called from daemon loop."""
    if not _pending_post_upload:
        return

    now = datetime.now(timezone.utc)

    for video_id, info in list(_pending_post_upload.items()):
        try:
            upload_time = datetime.fromisoformat(info["upload_time"])
            if upload_time.tzinfo is None:
                upload_time = upload_time.replace(tzinfo=timezone.utc)
            tasks = info["tasks"]
            elapsed = now - upload_time

            # T+1h: First-hour metrics check
            if not tasks["first_hour"] and elapsed >= timedelta(hours=1):
                tasks["first_hour"] = True
                _check_first_hour_metrics(video_id)

            # T+24h: First-day performance report
            if not tasks["first_day"] and elapsed >= timedelta(hours=24):
                tasks["first_day"] = True
                _send_first_day_report(video_id)

            # T+168h (1 week): One-week performance report
            if not tasks["one_week"] and elapsed >= timedelta(hours=168):
                tasks["one_week"] = True
                _send_one_week_report(video_id)
                # Clean up -- all tasks done
                del _pending_post_upload[video_id]
                continue

        except Exception as e:
            print(f"[Scheduler] Post-upload task error for {video_id}: {e}")


def _check_first_hour_metrics(video_id: str):
    """Check first-hour metrics for a newly uploaded video."""
    try:
        from server.notify import _tg
        print(f"[Scheduler] Checking first-hour metrics for {video_id}")

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        TOKEN_FILE = Path(__file__).parent / "youtube_token.json"
        if not TOKEN_FILE.exists():
            return

        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            with open(TOKEN_FILE, "w") as _tf:
                _tf.write(creds.to_json())

        yt = build("youtube", "v3", credentials=creds)
        result = yt.videos().list(part="statistics,snippet", id=video_id).execute()
        items = result.get("items", [])
        if not items:
            return

        stats = items[0].get("statistics", {})
        title = items[0].get("snippet", {}).get("title", "Unknown")
        views = int(stats.get("viewCount", 0))
        likes = int(stats.get("likeCount", 0))

        _tg(
            f"⏱ *First Hour Check*\n"
            f"_{_md_escape(title[:60])}_\n"
            f"Views: {views:,} | Likes: {likes:,}"
        )
        print(f"[Scheduler] First hour: {video_id} has {views} views, {likes} likes")

    except Exception as e:
        print(f"[Scheduler] First-hour metrics check error: {e}")


def _send_first_day_report(video_id: str):
    """Send 24-hour performance report via Telegram."""
    try:
        from server.notify import _tg
        print(f"[Scheduler] Generating 24h report for {video_id}")

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        TOKEN_FILE = Path(__file__).parent / "youtube_token.json"
        if not TOKEN_FILE.exists():
            return

        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            with open(TOKEN_FILE, "w") as _tf:
                _tf.write(creds.to_json())

        yt = build("youtube", "v3", credentials=creds)
        yt_anl = build("youtubeAnalytics", "v2", credentials=creds)

        # Get video title
        vid_result = yt.videos().list(part="snippet,statistics", id=video_id).execute()
        items = vid_result.get("items", [])
        if not items:
            return

        title = items[0].get("snippet", {}).get("title", "Unknown")
        stats = items[0].get("statistics", {})
        views = int(stats.get("viewCount", 0))

        # Get CTR and retention from analytics
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        try:
            r = yt_anl.reports().query(
                ids="channel==MINE",
                startDate=start_date, endDate=end_date,
                metrics="views,impressions,averageViewPercentage",
                filters=f"video=={video_id}",
            ).execute()
            rows = r.get("rows", [])
            anl_views = int(rows[0][0]) if rows and len(rows[0]) > 0 else views
            impressions = int(rows[0][1]) if rows and len(rows[0]) > 1 else 0
            retention = float(rows[0][2]) if rows and len(rows[0]) > 2 else 0
            ctr = round((anl_views / impressions) * 100, 1) if impressions > 0 else 0
        except Exception:
            ctr = 0
            retention = 0

        # Compare against channel averages
        avg_ctr = _get_channel_avg_ctr()
        avg_views = _get_channel_avg_views()
        views_per_hour = round(views / 24, 1)

        if avg_views > 0:
            if views > avg_views * 1.2:
                status = "above avg"
            elif views < avg_views * 0.8:
                status = "below avg"
            else:
                status = "on track"
        else:
            status = "no baseline"

        _tg(
            f"📊 *24h Performance Report*\n"
            f"Title: _{_md_escape(title[:60])}_\n"
            f"Views: {views:,} (velocity: {views_per_hour}/h)\n"
            f"CTR: {ctr}% (channel avg: {avg_ctr:.1f}%)\n"
            f"Retention: {retention:.0f}%\n"
            f"Status: *{status}*"
        )
        print(f"[Scheduler] 24h report sent for {video_id}: {views} views, CTR {ctr}%, status={status}")

    except Exception as e:
        print(f"[Scheduler] First-day report error: {e}")


def _send_one_week_report(video_id: str):
    """Send one-week performance summary via Telegram."""
    try:
        from server.notify import _tg
        print(f"[Scheduler] Generating 1-week report for {video_id}")

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        TOKEN_FILE = Path(__file__).parent / "youtube_token.json"
        if not TOKEN_FILE.exists():
            return

        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            with open(TOKEN_FILE, "w") as _tf:
                _tf.write(creds.to_json())

        yt = build("youtube", "v3", credentials=creds)
        vid_result = yt.videos().list(part="snippet,statistics", id=video_id).execute()
        items = vid_result.get("items", [])
        if not items:
            return

        title = items[0].get("snippet", {}).get("title", "Unknown")
        stats = items[0].get("statistics", {})
        views = int(stats.get("viewCount", 0))
        likes = int(stats.get("likeCount", 0))
        comments = int(stats.get("commentCount", 0))

        avg_views = _get_channel_avg_views()
        performance = ""
        if avg_views > 0:
            ratio = views / avg_views
            if ratio >= 1.5:
                performance = "Breakout performer"
            elif ratio >= 1.0:
                performance = "Above average"
            elif ratio >= 0.5:
                performance = "Below average"
            else:
                performance = "Underperformer"

        # Check if this was an experiment video
        is_experiment = False
        try:
            from clients.supabase_client import get_client
            client = get_client()
            vid_row = client.table("videos").select("pipeline_state").eq("youtube_id", video_id).limit(1).execute()
            if vid_row.data:
                ps = vid_row.data[0].get("pipeline_state") or {}
                if isinstance(ps, str):
                    try:
                        ps = json.loads(ps)
                    except Exception:
                        ps = {}
                is_experiment = ps.get("experiment") or ps.get("is_experiment")
        except Exception:
            pass

        header = "🧪 *Experiment 1-Week Report*" if is_experiment else "📊 *1-Week Performance Report*"

        msg = (
            f"{header}\n"
            f"Title: _{_md_escape(title[:60])}_\n"
            f"Views: {views:,} | Likes: {likes:,} | Comments: {comments:,}\n"
        )
        if avg_views > 0:
            msg += f"vs Channel Avg: {views/avg_views:.1f}x ({performance})\n"
        if is_experiment and avg_views > 0:
            pct = round(((views - avg_views) / avg_views) * 100, 1)
            msg += f"Experiment result: {pct:+.1f}% vs channel average"

        _tg(msg)
        print(f"[Scheduler] 1-week report sent for {video_id}: {views} views ({performance})")

    except Exception as e:
        print(f"[Scheduler] One-week report error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 6. ANALYTICS FRESHNESS ENHANCEMENT
# ══════════════════════════════════════════════════════════════════════════════


def _ensure_fresh_analytics(max_age_hours: int = 48):
    """
    Before any pipeline run, ensure analytics are fresh.

    - Default max age: 48h (reduced from 72h)
    - If insights file is missing: run full analytics
    - If stale: run incremental analytics (only new videos + channel stats)
    - Track last analytics run time to avoid redundant runs
    """
    try:
        from intel.channel_insights import is_insights_fresh, load_insights

        if not INSIGHTS_FILE.exists():
            print("[Scheduler] Channel insights missing -- running full analytics")
            run_analytics()
            return

        if not is_insights_fresh(max_age_hours=max_age_hours):
            insights = load_insights()
            age_str = insights.get("generated_at", "unknown")
            print(f"[Scheduler] Channel insights stale (generated: {age_str}, max age: {max_age_hours}h) -- running analytics")
            run_analytics()
        else:
            print(f"[Scheduler] Channel insights are fresh (max age: {max_age_hours}h)")

    except Exception as e:
        print(f"[Scheduler] Analytics freshness check failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 7. HEALTH CHECK JOB
# ══════════════════════════════════════════════════════════════════════════════


def run_health_check():
    """
    Daily system health check. Sends Telegram alert if issues found.

    Checks:
    - ElevenLabs credit balance (alert if < 1 video worth)
    - YouTube API token validity (alert if refresh fails)
    - Supabase connectivity (alert if unreachable)
    - Queue depth (alert if empty or very deep > 30)
    - Last successful pipeline run (alert if > 10 days ago)
    - Disk space / output directory size
    """
    print(f"\n{'='*60}\n  HEALTH CHECK -- {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*60}")

    issues = []
    info = []

    # ── Check 1: ElevenLabs credits ───────────────────────────────────────
    try:
        import requests as _req
        key = os.getenv("ELEVENLABS_API_KEY", "")
        if key:
            r = _req.get("https://api.elevenlabs.io/v1/user",
                         headers={"xi-api-key": key}, timeout=10)
            if r.status_code == 401:
                issues.append("ElevenLabs: API key invalid or expired (401)")
            elif r.status_code == 200:
                sub = json.loads(r.text, strict=False).get("subscription", {})
                limit = sub.get("character_limit", 1)
                used = sub.get("character_count", 0)
                remaining = limit - used
                videos_left = max(remaining, 0) // 10000
                if videos_left < 1:
                    issues.append(f"ElevenLabs: only {remaining:,} chars remaining (< 1 video)")
                else:
                    info.append(f"ElevenLabs: {remaining:,} chars remaining (~{videos_left} videos)")
        else:
            issues.append("ElevenLabs: API key not set")
    except Exception as e:
        issues.append(f"ElevenLabs: check failed ({e})")

    # ── Check 2: YouTube API token ────────────────────────────────────────
    try:
        TOKEN_FILE = Path(__file__).parent / "youtube_token.json"
        if TOKEN_FILE.exists():
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
            if creds and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
                with open(TOKEN_FILE, "w") as _tf:
                    _tf.write(creds.to_json())
                info.append("YouTube: token refreshed successfully")
            elif creds and creds.valid:
                info.append("YouTube: token valid")
            else:
                issues.append("YouTube: token invalid and cannot be refreshed")
        else:
            issues.append("YouTube: token file missing")
    except Exception as e:
        issues.append(f"YouTube: token refresh failed ({str(e)[:100]})")

    # ── Check 3: Supabase connectivity ────────────────────────────────────
    try:
        from clients.supabase_client import get_client
        client = get_client()
        # Simple ping -- select 1 row
        client.table("topics").select("id").limit(1).execute()
        info.append("Supabase: connected")
    except Exception as e:
        issues.append(f"Supabase: unreachable ({str(e)[:100]})")

    # ── Check 4: Queue depth ──────────────────────────────────────────────
    try:
        from clients.supabase_client import get_client
        client = get_client()
        q = client.table("topics").select("id", count="exact").eq("status", "queued").execute()
        queue_depth = q.count if hasattr(q, 'count') and q.count else len(q.data or [])
        if queue_depth == 0:
            issues.append("Queue: EMPTY -- no topics queued")
        elif queue_depth > 30:
            issues.append(f"Queue: very deep ({queue_depth} topics) -- may indicate stale topics")
        else:
            info.append(f"Queue: {queue_depth} topics ready")
    except Exception as e:
        issues.append(f"Queue: check failed ({e})")

    # ── Check 5: Last successful pipeline run ─────────────────────────────
    try:
        from clients.supabase_client import get_client
        client = get_client()
        recent = client.table("videos").select("created_at")\
            .order("created_at", desc=True).limit(1).execute()
        if recent.data:
            last_ts = recent.data[0].get("created_at", "")
            try:
                last_run = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                days_ago = (datetime.now(timezone.utc) - last_run).days
                if days_ago > 10:
                    issues.append(f"Pipeline: last video was {days_ago} days ago")
                else:
                    info.append(f"Pipeline: last video {days_ago} days ago")
            except Exception:
                info.append(f"Pipeline: last run at {last_ts[:10]}")
        else:
            issues.append("Pipeline: no videos found in database")
    except Exception as e:
        issues.append(f"Pipeline: check failed ({e})")

    # ── Check 6: Output directory size ────────────────────────────────────
    try:
        from core.pipeline_config import CLEANUP_WARN_DISK_GB
    except ImportError:
        CLEANUP_WARN_DISK_GB = 5
    try:
        outputs_dir = Path(__file__).parent / "outputs"
        if outputs_dir.exists():
            total_size = sum(f.stat().st_size for f in outputs_dir.rglob("*") if f.is_file())
            size_gb = total_size / (1024 ** 3)
            if size_gb > CLEANUP_WARN_DISK_GB:
                issues.append(f"Disk: outputs directory is {size_gb:.1f} GB (limit: {CLEANUP_WARN_DISK_GB} GB)")
            else:
                info.append(f"Disk: outputs directory {size_gb:.1f} GB")
    except Exception as e:
        info.append(f"Disk: check failed ({e})")

    # ── Report results ────────────────────────────────────────────────────
    for item in info:
        print(f"[Health] OK: {item}")
    for issue in issues:
        print(f"[Health] ISSUE: {issue}")

    # Send Telegram alert only if there are issues
    if issues:
        try:
            from server.notify import _tg
            lines = ["🏥 *System Health Alert*\n"]
            for issue in issues:
                lines.append(f"  - {issue}")
            if info:
                lines.append("\n_Healthy:_")
                for item in info:
                    lines.append(f"  + {item}")
            _tg("\n".join(lines))
        except Exception:
            pass
    else:
        print("[Health] All systems healthy")


# ══════════════════════════════════════════════════════════════════════════════
# EXISTING FUNCTIONS (preserved)
# ══════════════════════════════════════════════════════════════════════════════


def discover_topics():
    print(f"\n{'='*60}\n  TOPIC DISCOVERY -- {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*60}")

    # Verify competitive intel freshness (summary now injected directly in topic_discovery's run())
    try:
        from intel.competitive_intel import load_competitive_intel
        intel = load_competitive_intel()
        gen_at = intel.get("generated_at", "")
        if gen_at:
            print(f"[Discovery] Competitive intel available (generated {gen_at})")
        else:
            print("[Discovery] No competitive intel available — signals will be skipped")
    except Exception as e:
        print(f"[Discovery] Competitive intel check failed (non-fatal): {e}")

    import importlib.util
    spec = importlib.util.spec_from_file_location("disc", Path(__file__).parent / "agents" / "00_topic_discovery.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = mod.run()

    # Apply seasonal boosts to discovered topics
    try:
        topics = result.get("topics", []) if isinstance(result, dict) else []
        if topics:
            current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            youtube_growth.add_seasonal_boost(topics, current_date)
            print(f"[Scheduler] Seasonal boost applied to {len(topics)} discovered topics")
    except Exception as e:
        print(f"[Scheduler] Seasonal boost failed (non-critical): {e}")

    return result

def retry_dead_letters():
    """Re-queue topics that failed > 24h ago if they haven't been retried twice."""
    try:
        from clients.supabase_client import get_client
        client = get_client()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        # Use processed_at (set on failure) to check when the topic actually failed,
        # falling back to created_at for rows that predate the processed_at column
        result = client.table("topics").select("*").eq("status", "failed").execute()
        # Filter client-side: prefer processed_at, fall back to created_at
        rows = []
        for row in result.data or []:
            ts = row.get("processed_at") or row.get("created_at") or ""
            if ts and ts < cutoff:
                rows.append(row)
        for row in rows:
            retry_count = row.get("retry_count", 0) or 0
            if retry_count < 2:
                update_fields = {"status": "queued", "retry_count": retry_count + 1}
                try:
                    client.table("topics").update(update_fields).eq("id", row["id"]).execute()
                except Exception:
                    # retry_count column may not exist -- just re-queue without tracking
                    client.table("topics").update({"status": "queued"}).eq("id", row["id"]).execute()
                print(f"[Scheduler] Dead letter retry: {row.get('topic', '')[:50]} (attempt {retry_count + 1})")
            elif retry_count >= 2:
                client.table("topics").update({
                    "status": "dead_letter",
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", row["id"]).execute()
                print(f"[Scheduler] Moved to dead letter: {row.get('topic', '')[:50]}")
    except Exception as e:
        print(f"[Scheduler] Dead letter check error: {e}")


def run_one_video():
    print(f"\n{'='*60}\n  SCHEDULED RUN -- {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*60}")
    try:
        _run_one_video_inner()
    except Exception as e:
        print(f"[Scheduler] x Unhandled error in run_one_video: {e}")
        try:
            from server.notify import _tg
            _tg(f"\u274c *Scheduler Error*\n`{str(e)[:200]}`")
        except Exception:
            pass

def _run_one_video_inner():
    retry_dead_letters()
    from clients.supabase_client import get_next_topic, mark_topic_status, get_client

    # ── Experiment budget: dynamic cadence based on channel state ──
    try:
        client = get_client()
        total_result = client.table("videos").select("id", count="exact").execute()
        total_videos = total_result.count if hasattr(total_result, 'count') and total_result.count else len(total_result.data or [])
    except Exception as e:
        print(f"[Scheduler] Could not count total videos: {e}")
        total_videos = 0
        client = None

    # Use dynamic experiment cadence (defaults from pipeline_config)
    try:
        from core.pipeline_config import SCORING_CONFIG as _sc
        _cadence_default = _sc.get("experiment_cadence_default", 5)
        _cadence_throttled = _sc.get("experiment_cadence_throttled", 8)
    except ImportError:
        _cadence_default, _cadence_throttled = 5, 8
    experiment_cadence = _cadence_default
    try:
        from intel.channel_insights import load_insights
        insights = load_insights()
        if insights and client:
            experiment_cadence = _compute_experiment_cadence(insights, client)
        elif client and _recent_experiments_underperformed(client, n=3):
            experiment_cadence = _cadence_throttled
            print(f"[Scheduler] Last 3 experiments underperformed -- reducing experiment frequency to every {_cadence_throttled}th video")
    except Exception as e:
        print(f"[Scheduler] Experiment budget check failed (non-fatal): {e}")

    is_experiment = (total_videos % experiment_cadence == (experiment_cadence - 1))
    if is_experiment:
        print(f"[Scheduler] Experiment video (#{total_videos + 1} -- cadence 1/{experiment_cadence})")

    # Ensure analytics intelligence is fresh before generating a video (48h threshold)
    _ensure_fresh_analytics(max_age_hours=48)

    topic_row = get_next_topic()
    if not topic_row:
        print("[Scheduler] Queue empty -- running discovery first...")
        discover_topics()
        topic_row = get_next_topic()
    if not topic_row:
        print("[Scheduler] No topics available. Stopping.")
        return

    topic    = topic_row["topic"]
    topic_id = topic_row["id"]
    print(f"[Scheduler] Topic: {topic}")
    # Note: get_next_topic() already claimed the topic via claim_topic() -- no need to re-mark

    # Pass series metadata to pipeline if present
    _series_meta_path = None
    topic_metadata = topic_row.get("metadata") or {}
    if topic_metadata.get("series_part"):
        try:
            import tempfile
            _series_meta_path = Path(tempfile.mktemp(suffix="_series_meta.json"))
            _series_meta_path.write_text(json.dumps(topic_metadata))
            print(f"[Scheduler] Series Part {topic_metadata['series_part']} — metadata written to {_series_meta_path}")
        except Exception as _me:
            print(f"[Scheduler] Could not write series metadata (non-fatal): {_me}")
            _series_meta_path = None

    try:
        cmd = [sys.executable, "run_pipeline.py", topic]
        if is_experiment:
            cmd.append("--experiment")
        if _series_meta_path:
            cmd.extend(["--series-meta", str(_series_meta_path)])
        result = subprocess.run(cmd, cwd=str(Path(__file__).parent), timeout=7200)  # 2h max
        if result.returncode == 2:
            # Infrastructure failure (credits, API key) -- put topic back so it runs next week
            mark_topic_status(topic_id, "queued")
            print(f"[Scheduler] Topic re-queued (infrastructure block): {topic}")
        elif result.returncode == 0:
            mark_topic_status(topic_id, "done")
            # Note: save_video is called by run_pipeline.py itself -- no need to duplicate here
            print(f"[Scheduler] Done: {topic}")

            # ── Growth automation: pinned comment + shorts teaser ──
            video_id = None
            try:
                state_files = sorted(glob.glob(str(Path(__file__).parent / "outputs" / "*_state.json")),
                                     key=lambda p: Path(p).stat().st_mtime, reverse=True)
                if state_files:
                    with open(state_files[0]) as _sf:
                        state = json.load(_sf)
                    upload_data = state.get("stage_13") or {}
                    video_id = upload_data.get("video_id", "")
                    if upload_data.get("provider", "youtube") != "youtube":
                        video_id = None  # saved locally / custom uploader — no YouTube follow-ups
                    seo_data = state.get("stage_6") or {}
                    verification_data = state.get("stage_5") or {}

                    # Pin comment with sources and engagement prompt
                    if video_id:
                        try:
                            comment_text = youtube_growth.generate_pinned_comment(seo_data, verification_data)
                            youtube_growth.pin_comment(video_id, comment_text)
                        except Exception as e:
                            print(f"[Scheduler] Pin comment failed (non-critical): {e}")

                    # Post shorts teaser if a short was also uploaded
                    short_url = (state.get("stage_short_upload") or state.get("short_upload") or {}).get("url", "")
                    if short_url and video_id:
                        try:
                            title = seo_data.get("recommended_title", topic)
                            youtube_growth.post_shorts_teaser(video_id, title, topic)
                        except Exception as e:
                            print(f"[Scheduler] Shorts teaser failed (non-critical): {e}")
            except Exception as e:
                print(f"[Scheduler] Growth automation failed (non-critical): {e}")

            # ── Register post-upload sequence ──
            if video_id:
                try:
                    run_post_upload_sequence(video_id, datetime.now(timezone.utc))
                except Exception as e:
                    print(f"[Scheduler] Post-upload sequence failed (non-critical): {e}")
        else:
            mark_topic_status(topic_id, "failed")
            print(f"[Scheduler] Failed: {topic}")
            try:
                from server.notify import _tg
                _tg(f"\u274c *Pipeline Failed*\n_{_md_escape(topic[:80])}_\nCheck Railway logs.")
            except Exception:
                pass
    except subprocess.TimeoutExpired:
        mark_topic_status(topic_id, "failed")
        print(f"[Scheduler] Pipeline timed out after 2 hours: {topic}")
        try:
            from server.notify import _tg
            _tg(f"\u23f0 *Pipeline Timeout*\n_{_md_escape(topic[:80])}_\nKilled after 2 hours.")
        except Exception:
            pass
    except Exception as e:
        mark_topic_status(topic_id, "failed")
        print(f"[Scheduler] Error: {e}")
        try:
            from server.notify import _tg
            _tg(f"\u274c *Pipeline Error*\n`{str(e)[:200]}`")
        except Exception:
            pass

def show_status():
    from clients.supabase_client import list_queue, get_client
    list_queue()
    client = get_client()
    videos = client.table("videos").select("*").order("created_at",desc=True).limit(5).execute()
    print(f"{'─'*70}\n  RECENT VIDEOS\n{'─'*70}")
    for v in (videos.data or []):
        print(f"  {v['created_at'][:10]}  {v.get('title') or v['topic']}")
        if v.get("youtube_url"):
            print(f"             -> {v['youtube_url']}")
    print(f"{'─'*70}\n")

def run_analytics():
    print(f"\n{'='*60}\n  ANALYTICS RUN -- {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*60}")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("analytics", Path(__file__).parent / "agents" / "12_analytics_agent.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.run()
    except Exception as e:
        print(f"[Scheduler] Analytics run failed: {e}")
        return None


def send_weekly_report():
    print(f"\n{'='*60}\n  WEEKLY REPORT -- {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*60}")
    try:
        from server.notify import send_weekly_report as _send
        ok = _send()
        print(f"[Scheduler] Weekly report {'sent' if ok else 'sent (Telegram not configured)'}")
    except Exception as e:
        print(f"[Scheduler] Weekly report error: {e}")


def check_ab_titles():
    """
    Daily job: check CTR on videos that have an alternate title (title_b).
    If CTR < 4% after 48h, swap to title_b via YouTube API.
    """
    import glob
    import json
    from pathlib import Path
    from datetime import datetime, timedelta
    print(f"\n{'='*60}\n  A/B TITLE CHECK -- {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*60}")

    state_files = sorted(glob.glob(str(Path(__file__).parent / "outputs" / "*_state.json")))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

    for sf in state_files:
        try:
            with open(sf) as f:
                state = json.load(f)

            if not state.get("seo_title_b"):
                continue
            if state.get("ab_test_done"):
                continue

            upload = state.get("stage_13") or {}
            video_id = upload.get("video_id", "")
            if not video_id or upload.get("provider", "youtube") != "youtube":
                continue

            # Check upload age -- prefer uploaded_at from state, fall back to file mtime
            upload_ts = upload.get("uploaded_at") or state.get("finished_at")
            if upload_ts:
                try:
                    upload_time = datetime.fromisoformat(upload_ts.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    upload_time = datetime.fromtimestamp(Path(sf).stat().st_mtime, tz=timezone.utc)
            else:
                upload_time = datetime.fromtimestamp(Path(sf).stat().st_mtime, tz=timezone.utc)
            if upload_time > cutoff:
                print(f"[A/B] {video_id} too recent -- checking next time")
                continue

            # Get CTR via YouTube Analytics
            try:
                sys.path.append(str(Path(__file__).parent))
                from google.oauth2.credentials import Credentials
                from googleapiclient.discovery import build
                TOKEN_FILE = Path(__file__).parent / "youtube_token.json"
                if not TOKEN_FILE.exists():
                    continue
                _AB_SCOPES = [
                    "https://www.googleapis.com/auth/youtube",
                    "https://www.googleapis.com/auth/yt-analytics.readonly",
                ]
                creds   = Credentials.from_authorized_user_file(str(TOKEN_FILE))
                if creds and creds.expired and creds.refresh_token:
                    from google.auth.transport.requests import Request
                    creds.refresh(Request())
                    with open(TOKEN_FILE, "w") as _tf:
                        _tf.write(creds.to_json())
                yt      = build("youtube", "v3", credentials=creds)
                yt_anl  = build("youtubeAnalytics", "v2", credentials=creds)

                end_date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                start_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
                r = yt_anl.reports().query(
                    ids="channel==MINE",
                    startDate=start_date, endDate=end_date,
                    metrics="views,impressions",
                    filters=f"video=={video_id}",
                ).execute()
                rows = r.get("rows", [])
                # Calculate CTR from views/impressions (impressionClickThroughRate is not a valid v2 metric)
                views_ab = int(rows[0][0]) if rows and len(rows[0]) > 0 else 0
                impressions_ab = int(rows[0][1]) if rows and len(rows[0]) > 1 else 0
                ctr = round((views_ab / impressions_ab) * 100, 2) if impressions_ab > 0 else None

                if ctr is not None:
                    # Compare against 80% of channel average CTR (not a flat threshold)
                    channel_avg_ctr = _get_channel_avg_ctr()
                    swap_threshold = channel_avg_ctr * 0.80
                    print(f"[A/B] {video_id} CTR: {ctr:.2f}% (channel avg: {channel_avg_ctr:.1f}%, swap threshold: {swap_threshold:.1f}%)")
                    if ctr < swap_threshold:
                        title_b = state["seo_title_b"]
                        # Fetch current snippet to avoid blanking description/tags
                        current = yt.videos().list(part="snippet", id=video_id).execute()
                        cur_snippet = (current.get("items") or [{}])[0].get("snippet", {})
                        cur_snippet["title"] = title_b
                        cur_snippet.setdefault("categoryId", "27")
                        yt.videos().update(
                            part="snippet",
                            body={"id": video_id, "snippet": cur_snippet}
                        ).execute()
                        print(f"[A/B] Title swapped to B: {title_b[:60]}")
                        try:
                            from server.notify import _tg
                            _tg(f"🔄 *A/B Title Swap*\nCTR was {ctr:.1f}% -> swapped to:\n_{_md_escape(title_b)}_")
                        except Exception:
                            pass
                    else:
                        print(f"[A/B] CTR {ctr:.1f}% is above {swap_threshold:.1f}% threshold -- keeping title A")

                state["ab_test_done"] = True
                state["ab_ctr"] = ctr
                with open(sf, "w") as f:
                    json.dump(state, f, indent=2)

            except Exception as e:
                print(f"[A/B] Analytics failed for {video_id}: {e}")

        except Exception as e:
            print(f"[A/B] Error processing {sf}: {e}")


def post_community_poll():
    """Wednesday job: post a community poll with top eras from channel insights."""
    print(f"\n{'='*60}\n  COMMUNITY POLL -- {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*60}")
    try:
        from intel.channel_insights import load_insights
        insights = load_insights()
        era_perf = insights.get("era_performance", {})

        # Pick top 4 eras by avg views, fall back to defaults
        if era_perf:
            sorted_eras = sorted(era_perf.items(), key=lambda x: x[1].get("avg_views", 0), reverse=True)
            options = [era.replace("_", " ").title() for era, _ in sorted_eras[:4]]
        else:
            options = ["Ancient Rome", "Medieval Europe", "Mughal Empire", "Colonial India"]

        question = "Which era should we explore next?"
        result = youtube_growth.post_community_poll(question, options)
        print(f"[Scheduler] Community poll result: {result}")
    except Exception as e:
        print(f"[Scheduler] Community poll failed (non-critical): {e}")


def check_elevenlabs_credits():
    """Daily check: alert via Telegram if ElevenLabs credits are running low."""
    import requests as _req
    key = os.getenv("ELEVENLABS_API_KEY", "")
    if not key:
        return
    try:
        r = _req.get("https://api.elevenlabs.io/v1/user",
                     headers={"xi-api-key": key}, timeout=10)
        if r.status_code == 401:
            from server.notify import _tg
            _tg("\u26d4 *ElevenLabs API key invalid or quota exceeded (401)*")
            return
        if r.status_code != 200:
            return
        sub       = json.loads(r.text, strict=False).get("subscription", {})
        limit     = sub.get("character_limit", 1)
        used      = sub.get("character_count", 0)
        remaining = limit - used
        videos_left = max(remaining, 0) // 10000  # rough estimate
        print(f"[ElevenLabs] Credits: {remaining:,} remaining (~{videos_left} videos)")
        if remaining < 50000:
            from server.notify import _tg
            pct = (1 - remaining / limit) * 100 if limit else 0
            bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
            _tg(
                f"⚠️ *ElevenLabs Credits Low*\n"
                f"`{bar}` {pct:.0f}% used\n"
                f"{remaining:,} chars remaining (~{videos_left} videos left)\n"
                f"Top up at elevenlabs.io"
            )
    except Exception as e:
        print(f"[ElevenLabs] Credit check failed: {e}")


def run_tag_optimization():
    """Optimize tags on videos uploaded 24-48 hours ago."""
    try:
        from clients.supabase_client import get_client
        client = get_client()

        # Find videos uploaded 24-48 hours ago
        cutoff_start = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        cutoff_end = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        result = client.table("videos").select("youtube_id, title") \
            .gte("created_at", cutoff_start) \
            .lte("created_at", cutoff_end) \
            .execute()

        videos = result.data or []
        if not videos:
            return

        print(f"[Scheduler] Tag optimization: {len(videos)} videos in 24-48h window")

        from importlib.util import spec_from_file_location, module_from_spec
        spec = spec_from_file_location("uploader", Path(__file__).parent / "agents" / "11_youtube_uploader.py")
        uploader = module_from_spec(spec)
        spec.loader.exec_module(uploader)

        for v in videos:
            yt_id = v.get("youtube_id", "")
            if yt_id:
                print(f"[Scheduler] Optimizing tags: {v.get('title', yt_id)[:50]}")
                uploader.optimize_tags_post_upload(yt_id)

    except Exception as e:
        print(f"[Scheduler] Tag optimization failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 9. DAEMON MODE WITH NEW SCHEDULED JOBS
# ══════════════════════════════════════════════════════════════════════════════


def run_daemon():
    try:
        import schedule
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "schedule"], check=True)
        import schedule

    # Restore intel files from Supabase (survives Railway ephemeral disk)
    try:
        from core.utils import restore_all_intel_files
        restore_all_intel_files()
    except Exception as e:
        print(f"[Scheduler] Intel file restore warning: {e}")

    # Start the dashboard/webhook server FIRST so health checks pass
    try:
        from server.webhook_server import start_server
        start_server()
    except Exception as e:
        print(f"[Scheduler] Warning: could not start webhook server: {e}")

    # Download background music tracks on startup (skips if already present)
    try:
        from scripts import setup_music
        setup_music.run()
    except Exception as e:
        print(f"[Scheduler] Music setup warning: {e}")

    # Ensure music library is populated for all moods
    try:
        from media import music_manager
        music_manager.prefetch_all_moods()
    except Exception as e:
        print(f"[Scheduler] Music library prefetch warning: {e}")

    # Dynamic schedule adjustment from channel insights
    adjust_schedule_from_data()

    # ── Competitive intel + trend detection functions ───────────────────────
    def run_competitive_intel():
        """Daily crawl of competitor channels for content gaps."""
        try:
            from intel import competitive_intel
            competitive_intel.crawl_competitors()

            # Feed gaps into topic discovery
            from clients import supabase_client
            our_topics = [t.lower().strip() for t in supabase_client.get_all_topics_done()]
            gaps = competitive_intel.find_content_gaps(our_topics)

            if gaps:
                print(f"[Competitive] Found {len(gaps)} content gaps")
                # Build channel avg views for dynamic scoring
                intel = competitive_intel.load_competitive_intel()
                channel_avg = {}
                for ch_id, ch_data in intel.get("competitors", {}).items():
                    channel_avg[ch_data.get("name", "")] = ch_data.get("avg_views_recent_20", 0)

                # Queue top 3 gaps with dynamic scoring
                for gap in gaps[:3]:
                    gap_title = gap.get("title", "")  # FIX: was "topic"
                    if gap_title:
                        score = competitive_intel.compute_gap_score(gap, channel_avg)
                        supabase_client.add_topic(gap_title, source="competitive_gap", score=score)
                        print(f"  [Queued] {gap_title[:50]} (score={score})")
        except Exception as e:
            print(f"[Competitive] Intel crawl failed: {e}")

    def run_trend_detection():
        """Hourly trend scan -- triggers emergency pipeline for viral topics."""
        try:
            from intel import trend_detector
            trend_detector.run()
        except Exception as e:
            print(f"[Trends] Detection failed: {e}")

    def run_thumbnail_analysis():
        """Weekly thumbnail trend analysis from competitor channels."""
        try:
            from intel import competitive_intel
            patterns = competitive_intel.analyze_thumbnail_trends()
            if patterns:
                print(f"[Competitive] Analyzed {len(patterns)} thumbnail patterns")
        except Exception as e:
            print(f"[Competitive] Thumbnail analysis failed (non-fatal): {e}")

    print(f"\n{'='*60}\n  DAEMON MODE -- {VIDEOS_PER_WEEK}x/week\n{'='*60}\n")

    # ── Dashboard job tracking wrapper ────────────────────────────────────
    # Broadcasts job start/finish to the webhook server so dashboard shows
    # which background jobs are currently running.
    def _tracked(name, fn):
        """Wrap a scheduled job so it lights up in the dashboard."""
        from functools import wraps
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                from server.webhook_server import mark_job_running, mark_job_done
                mark_job_running(name)
            except Exception:
                pass
            try:
                return fn(*args, **kwargs)
            finally:
                try:
                    from server.webhook_server import mark_job_done
                    mark_job_done(name)
                except Exception:
                    pass
        return wrapper

    # ── Existing scheduled jobs ───────────────────────────────────────────
    schedule.every().monday.at(DISCOVER_TIME).do(_tracked("discovery", discover_topics))
    schedule.every().monday.at("10:00").do(_tracked("weekly_report", send_weekly_report))
    schedule.every().day.at("06:00").do(_tracked("analytics", run_analytics))
    schedule.every().day.at("07:00").do(_tracked("ab_test", check_ab_titles))
    schedule.every().day.at("07:30").do(_tracked("elevenlabs_check", check_elevenlabs_credits))
    schedule.every().wednesday.at("10:00").do(_tracked("community_poll", post_community_poll))
    schedule.every().day.at("05:00").do(_tracked("competitive_intel", run_competitive_intel))
    schedule.every().saturday.at("06:00").do(_tracked("thumbnail_analysis", run_thumbnail_analysis))
    schedule.every(3).hours.do(_tracked("trend_detection", run_trend_detection))
    schedule.every().day.at("14:00").do(_tracked("tag_optimization", run_tag_optimization))

    # ── New scheduled jobs ────────────────────────────────────────────────
    schedule.every().monday.at("11:00").do(_tracked("reengagement", check_reengagement_opportunities))
    schedule.every().day.at("07:30").do(_tracked("health_check", run_health_check))
    # ── Print schedule ────────────────────────────────────────────────────
    print("  Scheduled: Monday at 08:00 (topic discovery)")
    print("  Scheduled: Monday at 10:00 (weekly report -> Telegram)")
    print("  Scheduled: Monday at 11:00 (re-engagement check)")
    print("  Scheduled: Daily at 05:00 (competitive intelligence)")
    print("  Scheduled: Saturday at 06:00 (thumbnail analysis)")
    print("  Scheduled: Daily at 06:00 (analytics)")
    print("  Scheduled: Daily at 07:00 (A/B title check)")
    print("  Scheduled: Daily at 07:30 (ElevenLabs credit check + health check)")
    print("  Scheduled: Wednesday at 10:00 (community poll)")
    print("  Scheduled: Every 3 hours (trend detection)")
    print("  Scheduled: Daily at 14:00 (tag optimization)")
    for day in PUBLISH_DAYS:
        getattr(schedule.every(), day).at(PUBLISH_TIME).do(run_one_video)
        print(f"  Scheduled: {day.capitalize()} at {PUBLISH_TIME}")
    print("  Dynamic: Post-upload sequence (T+1h, T+24h, T+168h)")

    print("\n[Scheduler] Running... (Ctrl+C to stop)\n")
    while True:
        schedule.run_pending()
        # Check post-upload tasks on each loop iteration
        try:
            _check_post_upload_tasks()
        except Exception as e:
            print(f"[Scheduler] Post-upload task check error: {e}")
        time.sleep(60)

def _warn_if_youtube_upload_disabled():
    """Warn when YouTube credentials exist but uploads go elsewhere.

    Since uploads honour providers.upload.name (default: local), an existing
    YouTube deployment that never set it would silently stop publishing.
    """
    try:
        from providers.registry import get_provider_name
        has_token = bool(os.getenv("YOUTUBE_TOKEN_JSON")) or (Path(__file__).parent / "youtube_token.json").exists()
        name = get_provider_name("upload")
        if has_token and name != "youtube":
            print(f"[Scheduler] WARNING: YouTube credentials found but providers.upload.name is "
                  f"'{name}' — videos will NOT be published to YouTube. Set "
                  f"providers.upload.name: youtube in obsidian.yaml to publish.")
    except Exception:
        pass


if __name__ == "__main__":
    _warn_if_youtube_upload_disabled()
    if "--discover" in sys.argv:
        discover_topics()
    elif "--once" in sys.argv:
        run_one_video()
    elif "--status" in sys.argv:
        show_status()
    elif "--report" in sys.argv:
        send_weekly_report()
    elif "--ab-check" in sys.argv:
        check_ab_titles()
    elif "--credits" in sys.argv:
        check_elevenlabs_credits()
    elif "--poll" in sys.argv:
        post_community_poll()
    elif "--health" in sys.argv:
        run_health_check()
    elif "--reengagement" in sys.argv:
        check_reengagement_opportunities()
    elif "--daemon" in sys.argv:
        run_daemon()
    elif "--trends" in sys.argv:
        from intel import trend_detector
        trend_detector.run()
    elif "--competitive" in sys.argv:
        from intel import competitive_intel
        competitive_intel.crawl_competitors()
    else:
        print(__doc__)
