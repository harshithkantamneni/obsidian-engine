"""
Supabase client + schema setup for The Obsidian Archive.
Run once to create tables: python3 supabase_client.py --setup
"""
import os
import sys
import threading
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", interpolate=False)


def _with_retry(fn, max_retries=3, base_delay=1.0):
    """Retry a Supabase operation with exponential backoff."""
    import time
    last_err = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"[Supabase] Retry {attempt + 1}/{max_retries} after {delay}s: {e}")
                time.sleep(delay)
    raise last_err


SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

_cached_client = None
_client_lock = threading.Lock()

def get_client():
    global _cached_client
    if _cached_client is not None:
        return _cached_client
    with _client_lock:
        if _cached_client is not None:
            return _cached_client
        from supabase import create_client
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise Exception("SUPABASE_URL and SUPABASE_KEY must be set in .env")
        _cached_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        return _cached_client

def setup_schema():
    client = get_client()
    print("""
Run this SQL in Supabase dashboard → SQL Editor:

CREATE TABLE IF NOT EXISTS topics (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    topic TEXT NOT NULL UNIQUE,
    status TEXT DEFAULT 'queued',
    score FLOAT DEFAULT 0,
    source TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    processed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT now(),
    retry_count INT DEFAULT 0
);

-- If table already exists, add missing columns:
-- ALTER TABLE topics ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();
-- ALTER TABLE topics ADD COLUMN IF NOT EXISTS retry_count INT DEFAULT 0;

CREATE TABLE IF NOT EXISTS videos (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    topic TEXT NOT NULL,
    title TEXT,
    youtube_url TEXT,
    youtube_id TEXT,
    script_path TEXT,
    video_path TEXT,
    duration_seconds FLOAT,
    word_count INT,
    status TEXT DEFAULT 'rendered',
    pipeline_state JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    uploaded_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS analytics (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    video_id UUID REFERENCES videos(id) UNIQUE,
    views INT DEFAULT 0,
    watch_time_hours FLOAT DEFAULT 0,
    avg_view_percentage FLOAT DEFAULT 0,
    subscribers_gained INT DEFAULT 0,
    ctr_pct FLOAT DEFAULT 0,
    impressions INT DEFAULT 0,
    revenue_usd FLOAT DEFAULT 0,
    recorded_at TIMESTAMPTZ DEFAULT now()
);
-- Migration for existing tables (run once in Supabase SQL editor):
-- ALTER TABLE analytics ADD COLUMN IF NOT EXISTS avg_view_percentage FLOAT DEFAULT 0;
-- ALTER TABLE analytics ADD COLUMN IF NOT EXISTS ctr_pct FLOAT DEFAULT 0;
-- ALTER TABLE analytics ADD COLUMN IF NOT EXISTS impressions INT DEFAULT 0;
-- ALTER TABLE analytics ADD CONSTRAINT analytics_video_id_unique UNIQUE (video_id);

CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS param_observations (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    video_id TEXT,
    youtube_id TEXT,
    era TEXT,
    params JSONB,
    render_verification JSONB,
    render_compliance TEXT,
    metrics JSONB,
    metrics_attached_at TIMESTAMPTZ,
    loss_value FLOAT,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS optimizer_cycles (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    epoch INT DEFAULT 0,
    observations_used INT DEFAULT 0,
    confidence_level TEXT DEFAULT 'none',
    proposals JSONB,
    auto_applied JSONB,
    pending_approval JSONB,
    rollback_triggered BOOLEAN DEFAULT FALSE,
    rollback_params JSONB,
    diagnostics JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status);
CREATE INDEX IF NOT EXISTS idx_videos_created ON videos(created_at DESC);
""")
    try:
        client.table("topics").select("count").limit(1).execute()
        print("✓ Connected to Supabase successfully")
    except Exception as e:
        print(f"✗ Connection failed: {e}")

def add_topic(topic, source="manual", score=0.5, metadata=None):
    client = get_client()
    row = {"topic": topic, "source": source, "score": score, "status": "queued"}
    if metadata:
        row["metadata"] = metadata
    try:
        result = _with_retry(lambda: client.table("topics").insert(row).execute())
        print(f"✓ Added: {topic}")
        return result.data[0] if result.data else None
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            print(f"⚠ Already exists: {topic}")
        else:
            raise

def get_next_topic():
    """
    Pick the next topic to produce. Priority order:
    1. Series continuations (Part 2/3) whose parent Part 1 has been published
    2. Highest-scored queued topic with era rotation

    Uses claim_topic() for atomic claiming to prevent race conditions.
    """
    client = get_client()

    # Priority 1: Series continuations — Part 2/3 whose parent is already published
    try:
        series_parts = client.table("topics")\
            .select("*").eq("status", "queued").eq("source", "series_auto")\
            .order("created_at").limit(10).execute()
        for part in (series_parts.data or []):
            meta = part.get("metadata") or {}
            parent_topic = meta.get("parent_topic", "")
            if parent_topic:
                parent = client.table("topics")\
                    .select("status").eq("topic", parent_topic).limit(1).execute()
                if parent.data and parent.data[0].get("status") == "done":
                    if claim_topic(part["id"]):
                        print(f"[Scheduler] Series continuation: Part {meta.get('series_part', '?')} of '{parent_topic[:60]}'")
                        return part
    except Exception as e:
        print(f"[Scheduler] Series priority check failed (non-fatal): {e}")

    # Priority 2: Highest-scored queued topic with era rotation
    result = client.table("topics")\
        .select("*").eq("status","queued")\
        .order("score", desc=True).order("created_at").limit(20).execute()
    candidates = result.data or []
    if not candidates:
        return None

    # Era rotation: fetch last 2 published video topics and block both eras
    preferred_order = list(candidates)  # default: score order
    try:
        from intel.era_classifier import classify_era
        recent = client.table("videos").select("topic")\
            .order("created_at", desc=True).limit(2).execute()
        recent_topics = [r["topic"] for r in (recent.data or [])]
        blocked_eras = set()
        for rt in recent_topics:
            era = classify_era(rt)
            if era != "other":
                blocked_eras.add(era)

        if blocked_eras:
            # Re-sort: non-blocked eras first, then blocked
            non_blocked = [c for c in candidates if classify_era(c["topic"]) not in blocked_eras]
            blocked = [c for c in candidates if classify_era(c["topic"]) in blocked_eras]
            if non_blocked:
                preferred_order = non_blocked + blocked
            else:
                print(f"[Scheduler] All queued topics match blocked eras ({blocked_eras}), picking highest scored anyway")
    except Exception as e:
        print(f"[Scheduler] Era rotation check failed (non-fatal): {e}")

    # Try to atomically claim each candidate in preferred order
    for candidate in preferred_order:
        if claim_topic(candidate["id"]):
            return candidate
        print(f"[Scheduler] Topic '{candidate['topic']}' already claimed, trying next...")

    print("[Scheduler] All candidates were claimed by another instance")
    return None

def claim_topic(topic_id: str) -> bool:
    """Atomically claim a topic for processing. Returns False if already claimed.

    Uses Supabase's conditional update: only transitions status from 'queued'
    to 'in_progress'. If another instance already claimed it (status != 'queued'),
    the update affects 0 rows and we return False.
    """
    client = get_client()
    result = client.table("topics").update({
        "status": "in_progress",
    }).eq("id", topic_id).eq("status", "queued").execute()
    # If the update matched a row, we claimed it
    return bool(result.data)


def mark_topic_status(topic_id, status):
    from datetime import datetime, timezone
    client = get_client()
    update = {"status": status}
    if status in ("done","failed"):
        update["processed_at"] = datetime.now(timezone.utc).isoformat()
    _with_retry(lambda: client.table("topics").update(update).eq("id", topic_id).execute())

def save_video(topic, title, youtube_url, youtube_id, script_path,
               video_path, duration_seconds, word_count, pipeline_state,
               scene_manifest=None):
    from datetime import datetime, timezone
    client = get_client()
    payload = {
        "topic": topic, "title": title,
        "youtube_url": youtube_url, "youtube_id": youtube_id,
        "script_path": script_path, "video_path": video_path,
        "duration_seconds": duration_seconds, "word_count": word_count,
        "status": "uploaded", "pipeline_state": pipeline_state,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
    if scene_manifest is not None:
        payload["scene_manifest"] = scene_manifest
    # Upsert: update existing record if youtube_id already present, else insert
    if youtube_id:
        existing = client.table("videos").select("id").eq("youtube_id", youtube_id).limit(1).execute()
        if existing.data:
            result = _with_retry(lambda: client.table("videos").update(payload).eq("youtube_id", youtube_id).execute())
            print(f"✓ Updated video: {title}")
            return result.data[0] if result.data else None
    else:
        # No youtube_id — check for existing record by topic to avoid duplicates
        existing = client.table("videos").select("id").eq("topic", topic).is_("youtube_id", "null").limit(1).execute()
        if existing.data:
            existing_id = existing.data[0]["id"]
            result = _with_retry(lambda: client.table("videos").update(payload).eq("id", existing_id).execute())
            print(f"✓ Updated video (no youtube_id): {title}")
            return result.data[0] if result.data else None
    try:
        result = _with_retry(lambda: client.table("videos").insert(payload).execute())
        print(f"✓ Saved video: {title}")
        return result.data[0] if result.data else None
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            # Race condition: another process inserted first — fall back to update
            result = _with_retry(lambda: client.table("videos").update(payload).eq("topic", topic).execute())
            print(f"✓ Updated video (race-condition fallback): {title}")
            return result.data[0] if result.data else None
        raise

def save_music_metadata(topic: str, music_data: dict) -> None:
    """Update the videos table with music metadata after render.

    music_data keys: track_id, title, mood, source, adapted, stems_used, bpm, match_score.
    Silently ignores missing columns (graceful for pre-migration databases).
    """
    try:
        client = get_client()
        payload = {}
        field_map = {
            "track_id": "music_track_id",
            "title": "music_track_title",
            "mood": "music_mood",
            "source": "music_source",
            "adapted": "music_adapted",
            "stems_used": "music_stems_used",
            "bpm": "music_bpm",
            "match_score": "music_match_score",
        }
        for src_key, db_col in field_map.items():
            val = music_data.get(src_key)
            if val is not None:
                payload[db_col] = val

        if payload:
            _with_retry(lambda: client.table("videos").update(payload).eq("topic", topic).execute())
            print(f"✓ Saved music metadata for: {topic[:40]}")
    except Exception as e:
        # Graceful: missing columns or table issues should not break pipeline
        print(f"⚠ Music metadata save skipped: {e}")


def get_all_topics_done():
    client = get_client()
    result = client.table("topics").select("topic")\
        .in_("status",["done","skipped"]).execute()
    return [r["topic"].lower() for r in result.data] if result.data else []

def list_queue():
    client = get_client()
    result = client.table("topics").select("*").order("score", desc=True).execute()
    rows = result.data or []
    print(f"\n{'─'*70}\n  TOPIC QUEUE ({len(rows)} topics)\n{'─'*70}")
    for t in rows:
        print(f"  [{t['status']:12}] score={t.get('score') or 0:.2f}  {t['topic']}")
    print(f"{'─'*70}\n")

if __name__ == "__main__":
    if "--setup" in sys.argv:
        setup_schema()
    elif "--list" in sys.argv:
        list_queue()
    elif "--add" in sys.argv:
        idx = sys.argv.index("--add")
        if idx + 1 >= len(sys.argv):
            print("Error: --add requires a topic name argument")
            sys.exit(1)
        add_topic(sys.argv[idx+1], source="manual", score=0.8)
    else:
        print("Usage:")
        print("  python3 supabase_client.py --setup")
        print("  python3 supabase_client.py --list")
        print('  python3 supabase_client.py --add "topic name"')
