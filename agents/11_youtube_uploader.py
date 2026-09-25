#!/usr/bin/env python3
"""
Agent 11 — Uploader (stage 13)
Builds title/description/tags/thumbnail from the SEO data, then publishes via
the configured upload provider (providers.upload in obsidian.yaml):

  - "youtube": uploads to YouTube with SEO metadata and runs the YouTube-only
    follow-ups (sources comment, era playlist, endscreen/cards). First run
    opens a browser for OAuth; subsequent runs use the saved token.
  - anything else ("local" by default, or your own UploadProvider):
    provider.upload(video, title, description, tags, thumbnail).
"""
from __future__ import annotations

import os
import sys
import json
import glob
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

SECRETS_FILE = Path(__file__).resolve().parent.parent / "client_secrets.json"
TOKEN_FILE   = Path(__file__).resolve().parent.parent / "youtube_token.json"
# Environment variable alternative for deployed environments (e.g. Railway)
SECRETS_ENV  = os.getenv("GOOGLE_CLIENT_SECRETS_JSON")
SCOPES       = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

def _restore_token_from_env():
    """Re-write token file from YOUTUBE_TOKEN_JSON env var if available."""
    token_json = os.getenv("YOUTUBE_TOKEN_JSON", "")
    if token_json:
        TOKEN_FILE.write_text(token_json)
        print("[YouTube] Token restored from YOUTUBE_TOKEN_JSON env var")
        return True
    return False

def _is_headless():
    """Detect if running in a headless environment (Railway, Docker, CI)."""
    import sys
    # Explicit server environment indicators
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("DOCKER_CONTAINER"):
        return True
    # On macOS, DISPLAY is not set (X11 concept) — check for a different indicator
    if sys.platform == "darwin":
        return False  # macOS desktop always has a browser
    # On Linux, no DISPLAY means headless
    return not os.getenv("DISPLAY")

def get_credentials():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    elif _restore_token_from_env():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))

    # Check if token has all required scopes — re-auth on desktop if missing
    if creds and creds.valid and creds.scopes:
        missing = set(SCOPES) - set(creds.scopes)
        if missing:
            if _is_headless():
                print(f"[YouTube] WARNING: Token missing scopes: {missing}. "
                      "Re-authenticate locally and update YOUTUBE_TOKEN_JSON.")
            else:
                print(f"[YouTube] Token missing scopes {missing}, re-authenticating...")
                TOKEN_FILE.unlink(missing_ok=True)
                creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                raise RuntimeError(f"[YouTube] Token refresh failed: {e}. Re-authenticate manually.")
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
            print("[YouTube] Token refreshed and saved")
        elif _is_headless():
            # On Railway/Docker, we can't open a browser — try env var restore
            if not TOKEN_FILE.exists():
                _restore_token_from_env()
            if TOKEN_FILE.exists():
                creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
                if creds and creds.expired and creds.refresh_token:
                    try:
                        creds.refresh(Request())
                    except Exception as e:
                        raise RuntimeError(f"[YouTube] Token refresh failed: {e}. Update YOUTUBE_TOKEN_JSON.")
                    with open(TOKEN_FILE, "w") as f:
                        f.write(creds.to_json())
                    print("[YouTube] Token refreshed from env var")
            if not creds or not creds.valid:
                raise RuntimeError(
                    "[YouTube] Token expired and cannot re-authenticate in headless mode. "
                    "Update YOUTUBE_TOKEN_JSON env var in Railway with a fresh token."
                )
        else:
            if SECRETS_ENV:
                client_config = json.loads(SECRETS_ENV)
                flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            elif SECRETS_FILE.exists():
                flow = InstalledAppFlow.from_client_secrets_file(str(SECRETS_FILE), SCOPES)
            else:
                raise FileNotFoundError(
                    "YouTube OAuth credentials not found. Set GOOGLE_CLIENT_SECRETS_JSON "
                    "env var or place client_secrets.json in the project root."
                )
            creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
            print(f"[YouTube] Token saved: {TOKEN_FILE.name}")

    return creds

def generate_real_chapters(scenes_data: dict, audio_data: dict) -> list[dict]:
    """
    Generate YouTube chapter markers from real scene timings.
    Collapses down to 5-8 chapters for readability.
    """
    scenes = (scenes_data or {}).get("scenes", [])
    total = (audio_data or {}).get("total_duration_seconds", 0)
    if not scenes or not total:
        return []
    # Aim for 6-8 chapters; merge short scenes
    target_count = min(8, max(5, len(scenes) // 3))
    step = total / target_count if total and target_count else 0

    # If scenes lack start_time, compute cumulative timing from duration_seconds
    has_timing = any("start_time" in s for s in scenes)
    if not has_timing:
        cursor = 0.0
        for i, s in enumerate(scenes):
            scenes[i] = dict(s)
            scenes[i]["start_time"] = cursor
            cursor += float(s.get("duration_seconds", 0))

    chapters = []
    last_start = -999
    for scene in scenes:
        st = scene.get("start_time", 0)
        if st - last_start < step and chapters:
            continue
        last_start = st
        mins = int(st // 60)
        secs = int(st % 60)
        timestamp = f"{mins}:{secs:02d}"
        narration = scene.get("narration", "") or scene.get("narration_segment", "")
        words = narration.split()[:6]
        label = " ".join(words).rstrip(".,;:—") if words else f"Part {len(chapters)+1}"
        label = label[0].upper() + label[1:] if label else label
        chapters.append({"timestamp": timestamp, "label": label})
        if len(chapters) >= 8:
            break

    # First chapter must be 0:00
    if chapters:
        chapters[0]["timestamp"] = "0:00"
    return chapters


# ── Era classification & playlist management ─────────────────────────────────

from intel.era_classifier import classify_era

def get_or_create_playlist(youtube, era: str) -> str:
    """Get existing playlist for era, or create one."""
    PLAYLIST_NAMES = {
        "ancient_rome": "Ancient Rome's Darkest Secrets",
        "ancient_egypt": "Egypt's Hidden Horrors",
        "medieval": "Medieval Nightmares",
        "ancient_greece": "Ancient Greece Uncovered",
        "colonial": "Empire's Dark Legacy",
        "indian_history": "India's Untold History",
        "modern": "Modern History's Dark Chapters",
        "other": "The Obsidian Archive — Dark History",
    }
    title = PLAYLIST_NAMES.get(era, PLAYLIST_NAMES["other"])

    # Check if playlist already exists
    try:
        playlists = youtube.playlists().list(part="snippet", mine=True, maxResults=50).execute()
        for pl in playlists.get("items", []):
            if pl["snippet"]["title"] == title:
                return pl["id"]
    except Exception as e:
        print(f"[YouTube] Playlist lookup failed ({e}), skipping create to avoid duplicates")
        return None

    # Create new playlist
    body = {
        "snippet": {
            "title": title,
            "description": f"Dark untold history — {era.replace('_', ' ')}. Every story is real. Every fact is verified.",
        },
        "status": {"privacyStatus": "public"}
    }
    result = youtube.playlists().insert(part="snippet,status", body=body).execute()
    print(f"[YouTube] Created playlist: {title}")
    return result["id"]

def add_to_playlist(youtube, video_id: str, playlist_id: str):
    """Add video to playlist."""
    youtube.playlistItems().insert(
        part="snippet",
        body={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
    ).execute()
    print("[YouTube] Added to playlist")


# ── Endscreen, Cards & Playlist Strategy ──────────────────────────────────────

def get_related_video_id(current_topic: str, era: str) -> str:
    """Query Supabase videos table for the most viewed video in the same era. Return its youtube_id."""
    try:
        from clients.supabase_client import get_client
        client = get_client()
        # Query videos in the same era, ordered by views descending
        result = client.table("videos").select("youtube_id, topic, era, views") \
            .eq("era", era) \
            .order("views", desc=True) \
            .limit(10) \
            .execute()
        for row in (result.data or []):
            yt_id = row.get("youtube_id", "")
            row_topic = row.get("topic", "")
            # Skip the current video's topic
            if yt_id and row_topic.lower() != current_topic.lower():
                print(f"[YouTube] Related video found: {yt_id} ({row_topic[:50]})")
                return yt_id
    except Exception as e:
        print(f"[YouTube] Related video lookup failed (non-critical): {e}")
    return ""


def set_endscreen(video_id: str, youtube=None, recommended_video_id: str = None):
    """
    Add endscreen elements to a video. The YouTube Data API v3 does not have a
    programmatic endscreen endpoint, so we inject an endscreen CTA into the
    video description instead (with links to subscribe and recommended video).
    """
    try:
        if not youtube:
            creds = get_credentials()
            from googleapiclient.discovery import build as gbuild
            youtube = gbuild("youtube", "v3", credentials=creds)

        # Fetch current description
        resp = youtube.videos().list(part="snippet", id=video_id).execute()
        items = resp.get("items", [])
        if not items:
            print(f"[YouTube] Endscreen: video {video_id} not found")
            return

        snippet = items[0]["snippet"]
        current_desc = snippet.get("description", "")

        # Build endscreen CTA block
        endscreen_lines = [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "WATCH NEXT",
        ]
        if recommended_video_id:
            endscreen_lines.append(f"https://www.youtube.com/watch?v={recommended_video_id}")
        else:
            endscreen_lines.append("Check out our playlists for more dark history.")
        endscreen_lines.append("")
        endscreen_lines.append("SUBSCRIBE for new documentaries every week:")
        # Fetch actual channel ID, fall back to known handle
        try:
            ch_resp = youtube.channels().list(part="id", mine=True).execute()
            channel_id = ch_resp["items"][0]["id"] if ch_resp.get("items") else None
        except Exception:
            channel_id = None
        if channel_id:
            endscreen_lines.append(f"https://www.youtube.com/channel/{channel_id}?sub_confirmation=1")
        else:
            endscreen_lines.append("https://www.youtube.com/@ObsidianArchiveUnearthed?sub_confirmation=1")
        endscreen_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        endscreen_block = "\n".join(endscreen_lines)

        # Only append if not already present
        if "WATCH NEXT" not in current_desc:
            snippet["description"] = current_desc + endscreen_block
            snippet.setdefault("categoryId", "27")
            youtube.videos().update(
                part="snippet",
                body={"id": video_id, "snippet": snippet}
            ).execute()
            print("[YouTube] Endscreen CTA added to description")
        else:
            print("[YouTube] Endscreen CTA already present — skipping")

    except Exception as e:
        print(f"[YouTube] Endscreen failed (non-critical): {e}")


def create_playlist_if_needed(video_id: str, era: str, topic: str, youtube=None):
    """Check if a playlist exists for this era. If not, create it. Add video to the playlist."""
    try:
        if not youtube:
            creds = get_credentials()
            from googleapiclient.discovery import build as gbuild
            youtube = gbuild("youtube", "v3", credentials=creds)

        playlist_id = get_or_create_playlist(youtube, era)
        add_to_playlist(youtube, video_id, playlist_id)
        print(f"[YouTube] Video added to '{era}' playlist (topic: {topic[:50]})")
        return playlist_id
    except Exception as e:
        print(f"[YouTube] Playlist management failed (non-critical): {e}")
        return ""


def add_cards(video_id: str, related_ids: list, timestamps: list, youtube=None):
    """
    Add info cards at specified timestamps linking to related videos.
    Note: The YouTube Data API v3 does not support programmatic card insertion.
    Instead, we log the intended cards for manual addition via YouTube Studio,
    and append related video links to the description as a fallback.
    """
    try:
        if not related_ids or not timestamps:
            print("[YouTube] No cards to add (no related videos or timestamps)")
            return

        if not youtube:
            creds = get_credentials()
            from googleapiclient.discovery import build as gbuild
            youtube = gbuild("youtube", "v3", credentials=creds)

        # Log intended cards
        print(f"[YouTube] Cards intended ({len(related_ids)} videos at {len(timestamps)} timestamps):")
        for i, (vid, ts) in enumerate(zip(related_ids, timestamps)):
            mins = int(ts // 60)
            secs = int(ts % 60)
            print(f"  Card {i+1}: {mins}:{secs:02d} -> https://youtube.com/watch?v={vid}")

        # Fallback: append related video links to description
        resp = youtube.videos().list(part="snippet", id=video_id).execute()
        items = resp.get("items", [])
        if not items:
            return

        snippet = items[0]["snippet"]
        current_desc = snippet.get("description", "")

        if "RELATED VIDEOS" not in current_desc:
            related_lines = ["", "RELATED VIDEOS"]
            for vid in related_ids[:5]:
                related_lines.append(f"https://www.youtube.com/watch?v={vid}")
            snippet["description"] = current_desc + "\n".join(related_lines)
            snippet.setdefault("categoryId", "27")
            youtube.videos().update(
                part="snippet",
                body={"id": video_id, "snippet": snippet}
            ).execute()
            print("[YouTube] Related video links added to description")

    except Exception as e:
        print(f"[YouTube] Cards failed (non-critical): {e}")


def optimize_tags_post_upload(video_id: str, youtube=None):
    """
    Optimize tags based on YouTube Analytics search terms data.
    Called 24-48h after upload when search query data is available.
    """
    try:
        if not youtube:
            creds = get_credentials()
            from googleapiclient.discovery import build as gbuild
            youtube = gbuild("youtube", "v3", credentials=creds)

        # Get current video metadata
        resp = youtube.videos().list(part="snippet,statistics", id=video_id).execute()
        items = resp.get("items", [])
        if not items:
            print(f"[YouTube] Tag optimize: video {video_id} not found")
            return

        snippet = items[0]["snippet"]
        stats = items[0].get("statistics", {})
        current_tags = snippet.get("tags", [])
        views = int(stats.get("viewCount", 0))

        if views < 10:
            print(f"[YouTube] Tag optimize: too few views ({views}) — skipping")
            return

        # Try to get search terms from YouTube Analytics
        search_terms = []
        try:
            from googleapiclient.discovery import build as gbuild
            creds = get_credentials()
            yta = gbuild("youtubeAnalytics", "v2", credentials=creds)

            import datetime
            end_date = datetime.date.today().isoformat()
            start_date = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()

            report = yta.reports().query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                metrics="views",
                dimensions="insightTrafficSourceDetail",
                filters=f"insightTrafficSourceType==YT_SEARCH;video=={video_id}",
                maxResults=25,
                sort="-views",
            ).execute()

            for row in report.get("rows", []):
                term = row[0] if row else ""
                if term and len(term) >= 3:
                    search_terms.append(term)

        except Exception as e:
            print(f"[YouTube] Analytics query failed (non-critical): {e}")

        if not search_terms:
            print(f"[YouTube] No search terms found for {video_id} — keeping current tags")
            return

        # Merge: keep existing tags, add top search terms that aren't already tags
        existing_lower = {t.lower() for t in current_tags}
        new_tags = list(current_tags)  # preserve order
        added = 0
        for term in search_terms:
            if term.lower() not in existing_lower and len(term) < 100:
                new_tags.append(term)
                existing_lower.add(term.lower())
                added += 1
                if added >= 10:
                    break

        if added == 0:
            print("[YouTube] All search terms already in tags — no update needed")
            return

        # Apply sanitization and update
        new_tags = _sanitize_tags(new_tags)
        snippet["tags"] = new_tags
        snippet.setdefault("categoryId", "27")
        youtube.videos().update(
            part="snippet",
            body={"id": video_id, "snippet": snippet}
        ).execute()
        print(f"[YouTube] Tags optimized: added {added} search terms ({len(new_tags)} total tags)")

    except Exception as e:
        print(f"[YouTube] Tag optimization failed (non-critical): {e}")


def post_sources_comment(youtube, video_id: str, research_data: dict, verification_data: dict = None):
    """Post a sources/further reading comment on the video."""
    sources = []
    if verification_data:
        for s in (verification_data.get("source_list_for_description") or []):
            sources.append(s if isinstance(s, str) else str(s))
    if not sources and research_data:
        for s in (research_data.get("primary_sources") or [])[:8]:
            sources.append(s if isinstance(s, str) else str(s))
    if not sources:
        return

    text = "📚 SOURCES & FURTHER READING\n\n"
    text += "\n".join(f"• {s}" for s in sources[:10])
    text += "\n\n— The Obsidian Archive\n#history #darkhistory"

    try:
        youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {
                        "snippet": {"textOriginal": text}
                    }
                }
            }
        ).execute()
        print(f"[YouTube] ✓ Sources comment posted ({len(sources)} sources)")
    except Exception as e:
        print(f"[YouTube] Sources comment failed (non-critical): {e}")


def build_description(seo_data, verification_data=None):
    # description may be a dict {"hook_lines":..., "full_description":..., "hashtags":[...]}
    desc_raw    = seo_data.get("description", "")
    if isinstance(desc_raw, dict):
        hook_lines = (desc_raw.get("hook_lines") or "").strip()
        full_desc = (desc_raw.get("full_description") or "").strip()
        # Prepend hook_lines (visible before "Show More") before full description
        if hook_lines and full_desc:
            description = hook_lines + "\n\n" + full_desc
        else:
            description = full_desc or hook_lines or ""
        hashtags    = desc_raw.get("hashtags", [])
    else:
        description = desc_raw
        hashtags    = []

    # chapter_markers may be [{"timestamp":"0:00","label":"..."}] or ["0:00 label"]
    chapters_raw = seo_data.get("chapter_markers", [])
    chapters = []
    for ch in chapters_raw:
        if isinstance(ch, dict):
            ts    = ch.get("timestamp") or ch.get("time") or ""
            label = ch.get("label") or ch.get("title") or ""
            chapters.append(f"{ts} {label}".strip())
        elif isinstance(ch, str):
            chapters.append(ch)

    sources = []
    if verification_data:
        raw = verification_data.get("source_list_for_description", [])
        for s in raw:
            sources.append(s if isinstance(s, str) else str(s))

    lines = []
    if description:
        lines.append(description)
        lines.append("")

    if chapters:
        lines.append("CHAPTERS")
        for ch in chapters:
            lines.append(ch)
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("The Obsidian Archive — Dark untold history.")
    lines.append("Every story is real. Every fact is verified.")
    lines.append("")
    lines.append("Subscribe for new documentaries every week.")
    lines.append("")
    lines.append("This video was created with AI-assisted research, narration, and visuals.")
    lines.append("All historical claims are fact-verified against scholarly sources.")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    # Music attribution — only if music_manager provides one
    try:
        from media import music_manager
        attribution = music_manager.get_attribution()
        if attribution:
            lines.append(attribution)
    except Exception:
        pass

    # Image attribution
    try:
        audit_path = Path(__file__).resolve().parent.parent / "outputs" / "image_audit_log.json"
        if audit_path.exists():
            audit = json.loads(audit_path.read_text())
            wiki_count = sum(1 for s in audit.get("sources", []) if s.get("source") == "Wikimedia Commons")
            if wiki_count > 0:
                lines.append("Historical images sourced from Wikimedia Commons (public domain)")
    except Exception:
        pass

    if sources:
        lines.append("")
        lines.append("SOURCES & FURTHER READING")
        for s in sources[:10]:
            lines.append(f"• {s}")

    if hashtags:
        lines.append("")
        lines.append(" ".join(h if h.startswith("#") else f"#{h}" for h in hashtags[:10]))

    return "\n".join(lines)

def generate_thumbnail(manifest):
    """Use first AI image as thumbnail — resize to 1280x720."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[YouTube] Installing Pillow for thumbnail...")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "Pillow"], check=True)
        from PIL import Image, ImageDraw, ImageFont

    scenes = manifest.get("scenes", [])
    ai_image = None
    for scene in scenes:
        if scene.get("ai_image"):
            ai_image = scene["ai_image"]
            break

    if not ai_image:
        print("[YouTube] No AI image — generating text-only thumbnail")
        try:
            thumb_path = Path(__file__).resolve().parent.parent / "outputs" / "media" / "thumbnail.jpg"
            img = Image.new("RGB", (1280, 720), (15, 15, 20))
            draw = ImageDraw.Draw(img)

            # Try to get a decent font size
            try:
                font_large = ImageFont.load_default(size=72)
                font_small = ImageFont.load_default(size=36)
            except TypeError:
                font_large = ImageFont.load_default()
                font_small = font_large

            title = manifest.get("title", "The Obsidian Archive")

            # Draw a dark gradient overlay for depth
            for y_pos in range(720):
                alpha = int(40 + (y_pos / 720) * 60)
                draw.line([(0, y_pos), (1280, y_pos)], fill=(alpha, alpha // 3, alpha // 4))

            # Draw accent line
            draw.rectangle([(80, 280), (1200, 284)], fill=(200, 170, 100))

            # Word wrap title into 2-3 lines
            words = title.split()
            lines = []
            current_line = ""
            for w in words:
                test = f"{current_line} {w}".strip()
                if len(test) > 20 and current_line:
                    lines.append(current_line)
                    current_line = w
                else:
                    current_line = test
            if current_line:
                lines.append(current_line)

            # Draw title text with shadow (centered, above accent line)
            y = 300
            for line in lines[:3]:
                line_upper = line.upper()
                bbox = draw.textbbox((0, 0), line_upper, font=font_large)
                w = bbox[2] - bbox[0]
                x = (1280 - w) // 2
                # Shadow
                draw.text((x + 3, y + 3), line_upper, fill=(0, 0, 0), font=font_large)
                # Main text — warm gold
                draw.text((x, y), line_upper, fill=(220, 190, 130), font=font_large)
                y += 85

            # Channel name at bottom
            channel = "THE OBSIDIAN ARCHIVE"
            bbox = draw.textbbox((0, 0), channel, font=font_small)
            cw = bbox[2] - bbox[0]
            draw.text(((1280 - cw) // 2, 640), channel, fill=(160, 140, 110), font=font_small)

            img.save(thumb_path, "JPEG", quality=95)
            print(f"[YouTube] Fallback thumbnail saved: {thumb_path.name}")
            return str(thumb_path)
        except Exception as e:
            print(f"[YouTube] Fallback thumbnail failed: {e}")
            return None

    # Resolve path
    img_path = Path(ai_image)
    if not img_path.exists():
        img_path = Path(__file__).resolve().parent.parent / "outputs" / "media" / "assets" / Path(ai_image).name
    if not img_path.exists():
        print(f"[YouTube] Thumbnail image not found: {ai_image}")
        return None

    thumb_path = Path(__file__).resolve().parent.parent / "outputs" / "media" / "thumbnail.jpg"

    img = Image.open(img_path).convert("RGB")
    img = img.resize((1280, 720), Image.LANCZOS)

    # Dark overlay for text readability
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 120))
    img = img.convert("RGBA")
    img = Image.alpha_composite(img, overlay).convert("RGB")

    img.save(thumb_path, "JPEG", quality=95)
    print(f"[YouTube] Thumbnail saved: {thumb_path.name}")
    return str(thumb_path)

def _upload_captions(youtube, video_id: str, ctx) -> None:
    """Generate SRT from word timestamps and upload as English captions via YouTube API."""
    try:
        # Extract word_timestamps from ctx.state
        _state = getattr(ctx, "state", None) if ctx is not None else None
        if not _state:
            print("[YouTube] Captions: no pipeline state available — skipping")
            return

        word_timestamps = _state.get("word_timestamps", [])
        if not word_timestamps:
            stage_11 = _state.get("stage_11", {})
            if isinstance(stage_11, dict):
                word_timestamps = stage_11.get("word_timestamps", [])
        if not word_timestamps:
            audio_data = _state.get("audio_data", {})
            if isinstance(audio_data, dict):
                word_timestamps = audio_data.get("word_timestamps", [])
        if not word_timestamps:
            # Last resort: read timestamps.json directly from disk
            try:
                _ts_path = Path(__file__).resolve().parent.parent / "outputs" / "media" / "timestamps.json"
                if _ts_path.exists():
                    word_timestamps = json.loads(_ts_path.read_text()).get("words", [])
                    if word_timestamps:
                        print(f"[YouTube] Captions: loaded {len(word_timestamps)} words from timestamps.json")
            except Exception:
                pass

        if not word_timestamps:
            print("[YouTube] Captions: no word_timestamps found — skipping SRT upload")
            return

        # Generate SRT file
        from media.localization_pipeline import generate_srt
        srt_dir = Path(__file__).resolve().parent.parent / "outputs" / "captions"
        srt_dir.mkdir(parents=True, exist_ok=True)
        srt_path = str(srt_dir / f"{video_id}_en.srt")
        generate_srt(word_timestamps, srt_path)

        if not Path(srt_path).exists() or Path(srt_path).stat().st_size < 10:
            print("[YouTube] Captions: SRT file empty or missing after generation — skipping")
            return

        # Upload via YouTube Data API captions.insert()
        from googleapiclient.http import MediaFileUpload
        caption_body = {
            "snippet": {
                "videoId": video_id,
                "language": "en",
                "name": "English",
                "isDraft": False,
            }
        }
        media = MediaFileUpload(srt_path, mimetype="application/x-subrip")
        youtube.captions().insert(
            part="snippet",
            body=caption_body,
            media_body=media,
        ).execute()
        print(f"[YouTube] Captions uploaded: {Path(srt_path).name}")

    except Exception as e:
        print(f"[YouTube] Captions upload failed (non-critical): {e}")


def _sanitize_tags(raw_tags: list) -> list:
    """
    Clean tags to meet YouTube API requirements:
    - No special characters: < > " & = { } [ ] (cause invalidTags error)
    - No leading # (hashtags belong in description, not tags)
    - No commas inside a single tag
    - No angle brackets or HTML-like content
    - Each tag max 100 chars, strip whitespace
    - Only ASCII-printable + common unicode (no control chars)
    - Total combined character length ≤ 400 (conservative under YouTube's 500 limit)
    """
    import re
    clean = []
    for tag in raw_tags:
        if not isinstance(tag, str):
            continue
        tag = tag.strip().lstrip("#")
        # Remove HTML entities first (before stripping & character)
        tag = re.sub(r'&[a-zA-Z]+;', '', tag)
        tag = re.sub(r'&#\d+;', '', tag)
        # Strip all characters known to cause YouTube invalidTags errors
        tag = re.sub(r'[<>"&=\{\}\[\]\\|^~`]', '', tag)
        # Remove any control characters or non-printable chars
        tag = re.sub(r'[\x00-\x1f\x7f]', '', tag)
        tag = tag.split(",")[0].strip()     # take only first part if comma-separated
        tag = tag[:100]                     # per-tag char limit
        # Skip empty tags or tags that are just whitespace/punctuation
        tag = tag.strip()
        if tag and len(tag) >= 2:
            clean.append(tag)

    # Enforce 400 total character budget (conservative under YouTube's 500 limit)
    result, total = [], 0
    for tag in clean:
        if total + len(tag) + 1 > 400:
            break
        result.append(tag)
        total += len(tag) + 1  # +1 for comma separator
    return result


def upload_video(video_path, title, description, tags, thumbnail_path=None, privacy="public"):
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds   = get_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    print(f"[YouTube] Uploading: {Path(video_path).name}")
    print(f"[YouTube] Title: {title}")
    print(f"[YouTube] Privacy: {privacy}")
    print(f"[YouTube] Tags: {len(tags)}")

    body = {
        "snippet": {
            "title":       title[:100],  # YouTube limit
            "description": description[:5000],
            "tags":        _sanitize_tags(tags),
            "categoryId":  "24",  # Entertainment (documentary/history content)
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus":          privacy,
            "selfDeclaredMadeForKids": False,
            "selfDeclaredAlteredContent": True,
        }
    }

    media = MediaFileUpload(
        video_path,
        mimetype="video/mp4",
        resumable=True,
        chunksize=50 * 1024 * 1024  # 50MB chunks
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    # Resumable upload with progress and retry on tag errors
    response = None
    try:
        while response is None:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                print(f"  Upload progress: {pct}%")
    except Exception as upload_err:
        err_str = str(upload_err)
        if "invalidTags" in err_str or "invalid video keywords" in err_str.lower():
            print("[YouTube] Tag error detected — retrying with no tags...")
            body["snippet"]["tags"] = []
            media = MediaFileUpload(
                video_path,
                mimetype="video/mp4",
                resumable=True,
                chunksize=50 * 1024 * 1024
            )
            request = youtube.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media
            )
            try:
                response = None
                while response is None:
                    status, response = request.next_chunk()
                    if status:
                        pct = int(status.progress() * 100)
                        print(f"  Upload progress: {pct}%")
            except Exception as retry_err:
                print(f"[YouTube] Retry also failed: {retry_err}")
                raise retry_err from upload_err
        else:
            raise

    video_id  = response["id"]
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"[YouTube] ✓ Uploaded: {video_url}")

    # Set thumbnail
    if thumbnail_path and Path(thumbnail_path).exists():
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path, mimetype="image/jpeg")
            ).execute()
            print("[YouTube] ✓ Thumbnail set")
        except Exception as e:
            print(f"[YouTube] Thumbnail failed (need verified channel): {e}")

    return {"video_id": video_id, "url": video_url}

def batch_set_public(video_ids, stagger=True):
    """Flip existing videos from unlisted/private to public.

    If stagger=True, sets top 3 immediately; defers rest for manual daily flip.
    """
    from googleapiclient.discovery import build as gbuild

    creds = get_credentials()
    youtube = gbuild("youtube", "v3", credentials=creds)

    immediate = video_ids[:3] if stagger else video_ids
    deferred = video_ids[3:] if stagger else []

    results = {"updated": [], "failed": [], "deferred": deferred}
    for vid in immediate:
        try:
            resp = youtube.videos().list(part="status", id=vid).execute()
            items = resp.get("items", [])
            if not items:
                print(f"[YouTube] Video {vid} not found — skipping")
                continue
            current = items[0]["status"]["privacyStatus"]
            if current == "public":
                print(f"[YouTube] {vid} already public — skipping")
                continue
            youtube.videos().update(
                part="status",
                body={"id": vid, "status": {"privacyStatus": "public"}},
            ).execute()
            print(f"[YouTube] {vid}: {current} -> public")
            results["updated"].append(vid)
        except Exception as e:
            results["failed"].append({"video_id": vid, "error": str(e)})
            print(f"[YouTube] Failed to update {vid}: {e}")

    if deferred:
        print(f"[YouTube] {len(deferred)} videos deferred for staggered release")
    print(f"[YouTube] Updated {len(results['updated'])}/{len(video_ids)} videos to public")
    return results


def upload_with_provider(video_path, title, description, tags, thumbnail_path=None) -> dict:
    """Publish through a non-YouTube upload provider (providers.upload).

    Returns the provider's result dict plus {"provider": <name>}. Raises if the
    provider returns no video_id (stage validation treats that as a failure).
    """
    from providers.registry import get_provider, get_provider_name
    provider = get_provider("upload")
    name = get_provider_name("upload")
    print(f"[Upload] Publishing via upload provider '{name}' ({provider.name})")
    result = provider.upload(
        Path(video_path), title, description, list(tags or []),
        Path(thumbnail_path) if thumbnail_path else None,
    )
    result = dict(result or {})
    if not result.get("video_id"):
        raise RuntimeError(f"Upload provider '{name}' returned no video_id: {result}")
    result.setdefault("url", "")
    result["provider"] = name
    print(f"[Upload] ✓ {result.get('status', 'done')}: {result.get('url', '')}")
    return result


def _youtube_privacy(default: str) -> str:
    """providers.upload.options.privacy (youtube provider) overrides the call default."""
    try:
        from providers.registry import get_provider
        return getattr(get_provider("upload"), "privacy", None) or default
    except Exception:
        return default


def run(seo_data, manifest, verification_data=None, research_data=None, privacy="public", thumbnail_path=None):
    """Main entry point called from run_pipeline.py"""

    # Find latest rendered video
    videos = sorted(glob.glob(str(Path(__file__).resolve().parent.parent / "outputs" / "*_FINAL_VIDEO.mp4")))
    if not videos:
        raise FileNotFoundError("No rendered video found. Run Stage 12 first.")
    video_path = videos[-1]
    print(f"[YouTube] Video: {Path(video_path).name} ({Path(video_path).stat().st_size//1024//1024}MB)")

    title       = seo_data.get("recommended_title", "Untitled")
    tags        = seo_data.get("tags", [])
    description = build_description(seo_data, verification_data)
    thumbnail   = thumbnail_path if (thumbnail_path and Path(thumbnail_path).exists()) else generate_thumbnail(manifest)

    # Inject real chapters — prefer video-data.json (has real start_time) over footage manifest
    video_data_path = Path(__file__).resolve().parent.parent / "remotion" / "src" / "video-data.json"
    timed_data = {}
    if video_data_path.exists():
        try:
            timed_data = json.loads(video_data_path.read_text())
        except Exception:
            pass
    scenes_data = timed_data if timed_data.get("scenes") else (manifest if manifest.get("scenes") else {})
    audio_data  = {"total_duration_seconds": timed_data.get("total_duration_seconds") or manifest.get("total_duration_seconds", 0)}
    real_chapters = generate_real_chapters(scenes_data, audio_data)
    if real_chapters:
        seo_data = dict(seo_data)
        seo_data["chapter_markers"] = real_chapters
        description = build_description(seo_data, verification_data)

    from providers.registry import get_provider_name
    if get_provider_name("upload") != "youtube":
        # Non-YouTube provider: publish and skip YouTube-only follow-ups
        return upload_with_provider(video_path, title, description, tags, thumbnail)

    result  = upload_video(video_path, title, description, tags, thumbnail, _youtube_privacy(privacy))
    result["provider"] = "youtube"
    video_id = result.get("video_id", "")

    # Post sources comment
    if video_id:
        try:
            creds   = get_credentials()
            from googleapiclient.discovery import build as gbuild
            youtube = gbuild("youtube", "v3", credentials=creds)
            post_sources_comment(youtube, video_id, research_data or {}, verification_data)

            # Add to era playlist
            try:
                era = classify_era(seo_data.get("recommended_title", "") + " " + (manifest.get("topic", "")))
                playlist_id = get_or_create_playlist(youtube, era)
                add_to_playlist(youtube, video_id, playlist_id)
            except Exception as e:
                print(f"[YouTube] Playlist failed (non-critical): {e}")

            # Endscreen & cards strategy
            try:
                topic_text = manifest.get("topic", "") or seo_data.get("recommended_title", "")
                era = classify_era(seo_data.get("recommended_title", "") + " " + topic_text)
                related_id = get_related_video_id(topic_text, era)
                set_endscreen(video_id, youtube=youtube, recommended_video_id=related_id or None)
                if related_id:
                    # Add cards at 25%, 50%, 75% of video duration
                    total_dur = manifest.get("total_duration_seconds", 0)
                    if total_dur > 0:
                        card_timestamps = [total_dur * p for p in [0.25, 0.50, 0.75]]
                        add_cards(video_id, [related_id], card_timestamps[:1], youtube=youtube)
                # Log YouTube Studio endscreen editor URL for manual element addition
                studio_url = f"https://studio.youtube.com/video/{video_id}/editor"
                print(f"\n{'='*60}")
                print("  ADD ENDSCREEN ELEMENTS IN YOUTUBE STUDIO")
                print(f"{'='*60}")
                print(f"  URL: {studio_url}")
                print("  1. Click 'End screen' in the editor")
                print("  2. Add 'Best for viewer' video element (left box)")
                if related_id:
                    print(f"     Or use specific video: https://youtube.com/watch?v={related_id}")
                print("  3. Add 'Subscribe' element (right box)")
                print("  4. Position both in the lower-right to match video overlay")
                print(f"{'='*60}\n")
                # Telegram notification with Studio link
                try:
                    from server.notify import _tg
                    rec_line = f"[Recommended](https://youtube.com/watch?v={related_id})" if related_id else "Use 'Best for viewer'"
                    _tg(
                        f"🎬 *Add Endscreen Elements*\n\n"
                        f"[Open YouTube Studio Editor]({studio_url})\n\n"
                        f"1. Click 'End screen'\n"
                        f"2. Add video element → {rec_line}\n"
                        f"3. Add Subscribe element\n"
                        f"4. Position in lower-right to match overlay"
                    )
                except Exception:
                    pass
            except Exception as e:
                print(f"[YouTube] Endscreen/cards failed (non-critical): {e}")

        except Exception as e:
            print(f"[YouTube] Sources comment skipped: {e}")

    return result

if __name__ == "__main__":
    # Standalone test — loads latest state file
    state_files = sorted(glob.glob("outputs/*_state.json"))
    if not state_files:
        print("No state file found. Run run_pipeline.py first.")
        sys.exit(1)

    with open(state_files[-1]) as f:
        state = json.load(f)

    seo_data          = state.get("stage_6", {})
    manifest_path     = Path("outputs/media/media_manifest.json")
    manifest          = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    verification_data = state.get("stage_5", {})
    research_data     = state.get("stage_1", {})

    privacy = "public"
    if "--unlisted" in sys.argv:
        privacy = "unlisted"
    if "--private" in sys.argv:
        privacy = "private"

    print(f"[YouTube] Privacy mode: {privacy}")
    result = run(seo_data, manifest, verification_data, research_data=research_data, privacy=privacy)
    print(f"\n✓ Done: {result['url']}")
