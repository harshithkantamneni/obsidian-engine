"""
Local SFX provider — uses existing setup_sfx.py and setup_ambience.py files.
"""

from __future__ import annotations

from pathlib import Path

from providers.base import SFXProvider


def local_sfx_for_scene(scene: dict) -> str | None:
    """Mood-mapped one-shot from the local SFX set ("sfx/<file>") or None."""
    try:
        from scripts.setup_sfx import get_sfx_file
        return get_sfx_file(scene.get("mood", "dramatic")) or None
    except Exception:
        return None


def local_ambient_for_scene(scene: dict) -> str | None:
    """Location/mood-aware ambient bed from the local set ("ambience/<file>") or None."""
    try:
        from scripts.setup_ambience import get_ambient_file
        return get_ambient_file(
            scene.get("mood", "dark"),
            location=scene.get("location", ""),
            visual_desc=scene.get("visual_description", ""),
        ) or None
    except Exception:
        return None


class LocalSFXProvider(SFXProvider):
    """Local sound effects provider using Pixabay downloads."""

    def search(self, keyword: str, duration_max: float = 5.0,
               **kwargs) -> list[dict]:
        from scripts.setup_sfx import SFX_TRACKS

        results = []
        for mood, info in SFX_TRACKS.items():
            if keyword.lower() in mood or mood in keyword.lower():
                results.append({
                    "id": info["file"],
                    "title": info["desc"],
                    "duration": 0,
                    "tags": [mood],
                })
        return results

    def download(self, sfx_id: str, output_path: Path) -> Path:
        raise NotImplementedError("Local SFX tracks are already on disk")

    # Scene-level selection: the curated local files (scripts/setup_sfx.py,
    # scripts/setup_ambience.py), already under remotion/public.
    def sfx_for_scene(self, scene: dict, dest_dir: Path) -> str | None:
        return local_sfx_for_scene(scene)

    def ambient_for_scene(self, scene: dict, dest_dir: Path) -> str | None:
        return local_ambient_for_scene(scene)

    @property
    def name(self) -> str:
        return "Local SFX"
