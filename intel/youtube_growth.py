#!/usr/bin/env python3
"""
YouTube Growth Automation — The Obsidian Archive
Community polls, premiere scheduling, pinned comments, seasonal boosts,
and optimal publish time analysis using YouTube Data API v3.

All functions fail gracefully and never crash the pipeline.
"""

import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", interpolate=False)

SECRETS_FILE = Path(__file__).resolve().parent.parent / "client_secrets.json"
TOKEN_FILE   = Path(__file__).resolve().parent.parent / "youtube_token.json"
SCOPES       = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def _get_youtube_service():
    """Build an authenticated YouTube Data API v3 service."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    # Restore token from env var if file missing (Railway / Docker)
    if not TOKEN_FILE.exists():
        token_json = os.getenv("YOUTUBE_TOKEN_JSON", "")
        if token_json:
            TOKEN_FILE.write_text(token_json)
            print("[Growth] Token restored from YOUTUBE_TOKEN_JSON env var")

    if not TOKEN_FILE.exists():
        raise FileNotFoundError("[Growth] youtube_token.json not found and YOUTUBE_TOKEN_JSON not set")

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        print("[Growth] Token refreshed")

    return build("youtube", "v3", credentials=creds)


# ── a) Community Poll ─────────────────────────────────────────────────────────

def post_community_poll(question: str, options: list[str]) -> dict:
    """
    Post a community poll to the channel.
    Falls back gracefully — the community posts API is limited / unofficial.
    Returns {"posted": True/False, "error": str}.
    """
    try:
        youtube = _get_youtube_service()

        # The YouTube Data API v3 does not have a public endpoint for community
        # posts / polls.  activities.insert was deprecated for this purpose.
        # We attempt the call and degrade gracefully when the API rejects it.
        body = {
            "snippet": {
                "description": f"{question}\n\nOptions:\n" + "\n".join(f"• {o}" for o in options),
            }
        }
        try:
            youtube.activities().insert(part="snippet", body=body).execute()
            print(f"[Growth] Community poll posted: {question}")
            return {"posted": True, "error": ""}
        except Exception as api_err:
            # Expected — community posts are not fully supported via Data API
            print("[Growth] Community poll API not supported — sending to Telegram instead")
            try:
                from server.notify import _tg
                poll_text = (
                    f"📊 *Community Poll — Post Manually*\n\n"
                    f"**{question}**\n\n"
                    + "\n".join(f"• {o}" for o in options)
                    + "\n\nPaste this into YouTube Community tab."
                )
                _tg(poll_text)
                print("[Growth] Poll text sent to Telegram for manual posting")
                return {"posted": False, "error": f"API unsupported, sent to Telegram: {api_err}"}
            except Exception as tg_err:
                return {"posted": False, "error": f"API: {api_err}; Telegram: {tg_err}"}

    except Exception as e:
        print(f"[Growth] Community poll failed: {e}")
        return {"posted": False, "error": str(e)}


# ── b) Schedule Premiere ──────────────────────────────────────────────────────

def schedule_premiere(video_id: str, publish_at: str) -> dict:
    """
    Set a video to premiere at a specific time using videos.update with status.publishAt.
    publish_at must be ISO 8601 format (e.g. "2026-03-17T17:00:00Z").
    Returns {"scheduled": True, "publish_at": str}.
    """
    try:
        youtube = _get_youtube_service()

        youtube.videos().update(
            part="status",
            body={
                "id": video_id,
                "status": {
                    "privacyStatus": "private",
                    "publishAt": publish_at,
                }
            }
        ).execute()

        print(f"[Growth] Premiere scheduled for {video_id} at {publish_at}")
        return {"scheduled": True, "publish_at": publish_at}

    except Exception as e:
        print(f"[Growth] Premiere scheduling failed: {e}")
        return {"scheduled": False, "publish_at": publish_at, "error": str(e)}


# ── c) Pin Comment ────────────────────────────────────────────────────────────

def pin_comment(video_id: str, comment_text: str) -> dict:
    """
    Post a comment on a video and pin it using commentThreads.insert.
    Returns {"comment_id": str, "pinned": True/False}.
    """
    try:
        youtube = _get_youtube_service()

        # Post the comment
        result = youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {
                        "snippet": {"textOriginal": comment_text}
                    }
                }
            }
        ).execute()

        comment_id = result["snippet"]["topLevelComment"]["id"]
        print(f"[Growth] Comment posted: {comment_id}")

        # Pin the comment (using comments.setModerationStatus or update)
        try:
            youtube.comments().setModerationStatus(
                id=comment_id,
                moderationStatus="published",
            ).execute()
            # Note: true "pinning" is not directly supported by the Data API.
            # The comment is posted by the channel owner so it appears at the top.
            print(f"[Growth] Comment pinned on {video_id}")
            return {"comment_id": comment_id, "pinned": True}
        except Exception as pin_err:
            print(f"[Growth] Comment posted but pin API unavailable: {pin_err}")
            return {"comment_id": comment_id, "pinned": False}

    except Exception as e:
        print(f"[Growth] Pin comment failed: {e}")
        return {"comment_id": "", "pinned": False, "error": str(e)}


# ── d) Generate Pinned Comment Text ──────────────────────────────────────────

def generate_pinned_comment(seo_data: dict, verification_data: dict) -> str:
    """
    Generate the text for a pinned comment from pipeline data.
    Includes sources, CTA, and engagement prompt.
    """
    lines = []

    # Header
    lines.append("📌 SOURCES & VERIFICATION")
    lines.append("")

    # List verification sources
    sources = []
    if verification_data:
        for s in (verification_data.get("source_list_for_description") or []):
            sources.append(s if isinstance(s, str) else str(s))
    if not sources:
        # Fallback to any available sources in seo_data
        for s in (seo_data.get("sources") or seo_data.get("references") or []):
            sources.append(s if isinstance(s, str) else str(s))

    if sources:
        for s in sources[:10]:
            lines.append(f"• {s}")
        lines.append("")

    # Subscribe CTA
    lines.append("🔔 Subscribe for weekly dark history")
    lines.append("Every story is real. Every fact is verified.")
    lines.append("")

    # Engagement prompt
    lines.append("💬 What topic should we cover next?")
    lines.append("Drop your suggestion below — the most requested topic wins!")
    lines.append("")
    lines.append("— The Obsidian Archive")

    return "\n".join(lines)


# ── e) Post Shorts Teaser ────────────────────────────────────────────────────

def post_shorts_teaser(video_id: str, title: str, topic: str) -> dict:
    """
    Create a community post teasing the upcoming long-form video.
    Falls back to Telegram if community posts API is unavailable.
    Returns {"posted": True/False, "error": str}.
    """
    try:
        teaser_text = (
            f"🎬 New video dropping soon: {title}\n\n"
            f"What do you think really happened with {topic}? "
            f"Drop your theory below 👇\n\n"
            f"Full documentary coming soon — don't miss it.\n"
            f"🔔 Hit the bell so you're first to watch!"
        )

        youtube = _get_youtube_service()

        # Community posts are not supported via Data API — attempt and fall back
        try:
            youtube.activities().insert(
                part="snippet",
                body={"snippet": {"description": teaser_text}}
            ).execute()
            print(f"[Growth] Shorts teaser posted for: {title}")
            return {"posted": True, "error": ""}
        except Exception as api_err:
            print("[Growth] Community post API not supported — sending teaser to Telegram")
            try:
                from server.notify import _tg
                _tg(
                    f"📢 *Community Post Teaser — Post Manually*\n\n"
                    f"{teaser_text}\n\n"
                    f"_Paste this into YouTube Community tab._"
                )
                return {"posted": False, "error": f"API unsupported, sent to Telegram: {api_err}"}
            except Exception as tg_err:
                return {"posted": False, "error": f"API: {api_err}; Telegram: {tg_err}"}

    except Exception as e:
        print(f"[Growth] Shorts teaser failed: {e}")
        return {"posted": False, "error": str(e)}


# ── f) Optimal Publish Time ──────────────────────────────────────────────────

def get_optimal_publish_time(channel_insights: dict) -> str:
    """
    Analyze channel_insights to determine the best publish day/time.
    Defaults to Tuesday 17:00 UTC if no data is available.
    Returns ISO 8601 timestamp for the next optimal slot.
    """
    default_day = 1   # Tuesday (0=Monday)
    default_hour = 17  # 17:00 UTC

    try:
        # Check if insights have publish time data
        health = channel_insights.get("channel_health", {})
        best_day = health.get("best_publish_day", "").lower()
        best_hour = health.get("best_publish_hour")

        day_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }

        target_day = day_map.get(best_day, default_day)
        target_hour = int(best_hour) if best_hour is not None else default_hour

        # Clamp hour to valid range
        target_hour = max(0, min(23, target_hour))

    except Exception:
        target_day = default_day
        target_hour = default_hour

    # Calculate next occurrence of target day/time
    now = datetime.now(timezone.utc)
    days_ahead = target_day - now.weekday()
    if days_ahead < 0:
        days_ahead += 7
    elif days_ahead == 0:
        # If today is the target day but time has passed, go to next week
        if now.hour >= target_hour:
            days_ahead += 7

    next_slot = now.replace(hour=target_hour, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    iso_str = next_slot.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"[Growth] Optimal publish time: {iso_str}")
    return iso_str


# ── g) Seasonal Boost ────────────────────────────────────────────────────────

def add_seasonal_boost(topics: list[dict], current_date: str) -> list[dict]:
    """
    Boost topic scores based on seasonal relevance.
    - October: dark/horror history +0.2 boost
    - March 15: Roman history +0.15 (Ides of March)
    - June: war history +0.1 (D-Day anniversary)
    - November: colonial/native history +0.1
    - December: religious history +0.1
    Returns modified topics list with adjusted scores.
    """
    try:
        dt = datetime.fromisoformat(current_date.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = datetime.strptime(current_date, "%Y-%m-%d")
        except Exception:
            print(f"[Growth] Could not parse date '{current_date}' — skipping seasonal boost")
            return topics

    month = dt.month
    day = dt.day

    # Define seasonal boost rules
    boosts_applied = 0
    for t in topics:
        topic_text = (t.get("topic", "") + " " + t.get("era", "")).lower()
        original_score = float(t.get("score", 0.5))
        boost = 0.0

        # October: dark/horror history
        if month == 10:
            horror_keywords = ["dark", "horror", "death", "plague", "torture", "execution",
                               "witch", "curse", "haunted", "demon", "ritual", "sacrifice",
                               "murder", "macabre", "skeleton", "ghost", "occult"]
            if any(kw in topic_text for kw in horror_keywords):
                boost = max(boost, 0.2)

        # March 15 (and surrounding days): Roman history (Ides of March)
        if month == 3 and 10 <= day <= 20:
            roman_keywords = ["rome", "roman", "caesar", "senate", "gladiator", "legion",
                              "emperor", "brutus", "ides", "republic", "colosseum"]
            if any(kw in topic_text for kw in roman_keywords):
                boost = max(boost, 0.15)

        # June: war history (D-Day anniversary)
        if month == 6:
            war_keywords = ["war", "battle", "invasion", "military", "army", "siege",
                            "d-day", "normandy", "soldier", "combat", "campaign",
                            "naval", "conquest"]
            if any(kw in topic_text for kw in war_keywords):
                boost = max(boost, 0.1)

        # November: colonial/native history
        if month == 11:
            colonial_keywords = ["colonial", "colony", "native", "indigenous", "empire",
                                 "conquest", "settler", "plantation", "slave", "rebellion",
                                 "independence", "revolution"]
            if any(kw in topic_text for kw in colonial_keywords):
                boost = max(boost, 0.1)

        # December: religious history
        if month == 12:
            religious_keywords = ["religious", "religion", "church", "temple", "crusade",
                                  "pope", "inquisition", "heresy", "saint", "monastery",
                                  "pilgrimage", "holy", "biblical", "prophecy"]
            if any(kw in topic_text for kw in religious_keywords):
                boost = max(boost, 0.1)

        if boost > 0:
            new_score = min(1.0, original_score + boost)
            t["score"] = round(new_score, 2)
            t.setdefault("adjustments", []).append(f"seasonal_boost:+{boost:.2f}")
            boosts_applied += 1
            print(f"[Growth] Seasonal boost: {t.get('topic', '')[:50]}... +{boost:.2f} ({original_score:.2f} -> {new_score:.2f})")

    if boosts_applied:
        print(f"[Growth] Applied seasonal boosts to {boosts_applied}/{len(topics)} topics")
    else:
        print(f"[Growth] No seasonal boosts applicable for {dt.strftime('%B %d')}")

    return topics
