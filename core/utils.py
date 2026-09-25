"""Shared utilities for the Obsidian pipeline."""

import json
import os
import tempfile
from pathlib import Path
from typing import Union


def atomic_write_json(path: Path, data: Union[dict, list]):
    """Write JSON atomically via temp file + rename to prevent corruption."""
    # resolve() follows symlinks, so a file symlinked into a mounted data dir
    # (Docker ./data) is replaced in place instead of the link being clobbered.
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Supabase JSON blob persistence ──────────────────────────────────────────
# Persists large JSON files (channel_insights, lessons_learned, etc.) to
# Supabase so they survive Railway's ephemeral disk across deployments.
#
# Uses a simple kv_store table:
#   CREATE TABLE IF NOT EXISTS kv_store (
#       key TEXT PRIMARY KEY,
#       value JSONB NOT NULL,
#       updated_at TIMESTAMPTZ DEFAULT now()
#   );

# Keys used for each file
_KV_KEYS = {
    "channel_insights.json": "channel_insights",
    "lessons_learned.json": "lessons_learned",
    "correlation_results.json": "correlation_results",
    "music_analysis.json": "music_analysis",
    "competitive_intel.json": "competitive_intel",
    "cost_log.json": "cost_log",
    "trend_results.json": "trend_results",
    "image_audit_log.json": "image_audit_log",
}


def persist_json_to_supabase(file_path: Path, data: Union[dict, list]):
    """Persist a JSON blob to Supabase kv_store. Non-blocking, never crashes."""
    try:
        from clients.supabase_client import get_client
        key = _KV_KEYS.get(Path(file_path).name)
        if not key:
            return
        sb = get_client()
        sb.table("kv_store").upsert({
            "key": key,
            "value": data,
        }, on_conflict="key").execute()
    except Exception as e:
        print(f"[Utils] Could not persist {Path(file_path).name} to Supabase: {e}")


def restore_json_from_supabase(file_path: Path) -> bool:
    """Restore a JSON file from Supabase kv_store if local file is empty/missing.

    Returns True if file was restored, False otherwise.
    """
    path = Path(file_path)
    # Skip if file already has real data (not just '{}')
    if path.exists():
        try:
            content = path.read_text().strip()
            if content and content not in ("{}", "[]"):
                return False
        except Exception:
            pass

    try:
        from clients.supabase_client import get_client
        key = _KV_KEYS.get(path.name)
        if not key:
            return False
        sb = get_client()
        resp = sb.table("kv_store").select("value").eq("key", key).limit(1).execute()
        if resp.data and resp.data[0].get("value"):
            data = resp.data[0]["value"]
            # Supabase returns JSONB as a Python dict or list
            atomic_write_json(path, data)
            print(f"[Utils] Restored {path.name} from Supabase ({len(json.dumps(data))} bytes)")
            return True
    except Exception as e:
        print(f"[Utils] Could not restore {path.name} from Supabase: {e}")
    return False


def restore_all_intel_files():
    """Restore all intel JSON files from Supabase. Called on server startup."""
    base = Path(__file__).resolve().parent.parent
    files = [
        base / "channel_insights.json",
        base / "lessons_learned.json",
        base / "outputs" / "correlation_results.json",
        base / "outputs" / "music_analysis.json",
        base / "outputs" / "competitive_intel.json",
        base / "outputs" / "cost_log.json",
        base / "outputs" / "trend_results.json",
        base / "outputs" / "image_audit_log.json",
    ]
    restored = 0
    for f in files:
        if restore_json_from_supabase(f):
            restored += 1
    if restored:
        print(f"[Utils] Restored {restored} intel file(s) from Supabase")
