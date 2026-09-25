"""Music / SFX / ambient selection for the Remotion convert stage.

Everything goes through the configured providers (providers.music and
providers.sfx in obsidian.yaml). The built-in providers reproduce the
original behaviour (Epidemic Sound API first when configured, then the local
library); a custom provider fully decides what to return.

All returned paths are relative to remotion/public (Invariant 15).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from core.log import get_logger

logger = get_logger(__name__)


# ── SFX / ambient ────────────────────────────────────────────────────────────

def _sfx_provider():
    from providers.registry import get_provider
    return get_provider("sfx")


def select_scene_ambient(scene: dict, public_dir: Path) -> str | None:
    """Ambient bed for a scene via providers.sfx (None on error / nothing found)."""
    try:
        return _sfx_provider().ambient_for_scene(scene, Path(public_dir) / "ambience") or None
    except Exception as e:
        logger.debug(f"[Convert] Ambient selection failed: {e}")
        return None


def select_scene_sfx(scene: dict, public_dir: Path) -> str | None:
    """One-shot SFX for a key scene via providers.sfx (None on error / nothing found)."""
    try:
        return _sfx_provider().sfx_for_scene(scene, Path(public_dir) / "sfx") or None
    except Exception as e:
        logger.debug(f"[Convert] SFX selection failed: {e}")
        return None


# ── Music ────────────────────────────────────────────────────────────────────

def normalize_public_path(file_ref: str, public_dir: Path, subdir: str = "music") -> str | None:
    """Return a path relative to remotion/public for a provider-returned file.

    Absolute paths (or files outside remotion/public) are copied into
    ``remotion/public/<subdir>/``. Paths already relative to remotion/public
    are kept.
    """
    if not file_ref:
        return None
    public_dir = Path(public_dir)
    p = Path(str(file_ref))
    if not p.is_absolute():
        if (public_dir / p).exists():
            return p.as_posix()
        if not p.exists():
            return p.as_posix()  # trust the provider (e.g. file appears later)
        p = p.resolve()
    try:
        return p.resolve().relative_to(public_dir.resolve()).as_posix()
    except ValueError:
        pass
    if not p.exists():
        logger.warning(f"[Convert] Music file from provider not found: {p}")
        return None
    dest_dir = public_dir / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / p.name
    if not dest.exists() or dest.stat().st_size != p.stat().st_size:
        shutil.copy2(p, dest)
    return f"{subdir}/{p.name}"


def select_background_music(remotion_scenes: list[dict], total_duration: float,
                            public_dir: Path) -> tuple[str | None, float]:
    """Pick the background track: music provider first, then local fallbacks.

    Returns (music_file relative to remotion/public or None, start_offset).
    """
    music_file = None
    music_start_offset = 0.0
    provider_name = "local"
    try:
        from providers.registry import get_provider, get_provider_name
        provider_name = get_provider_name("music")
        result = get_provider("music").select_for_video(remotion_scenes, total_duration)
        if result and result.get("music_file"):
            music_file = normalize_public_path(result["music_file"], public_dir)
            music_start_offset = float(result.get("music_start_offset", 0) or 0)
            if music_file:
                corr = result.get("correlation_score")
                corr_txt = f", corr={corr:.3f}" if isinstance(corr, (int, float)) else ""
                logger.info(f"[Convert] Music ({provider_name}): {music_file} "
                            f"(offset={music_start_offset:.1f}s{corr_txt})")
    except Exception as e:
        logger.warning(f"[Convert] Music provider '{provider_name}' failed: {e}")
        music_file = None

    if not music_file:
        music_start_offset = 0.0
        try:
            from media import music_manager
            if provider_name != "local":
                smart = music_manager.get_smart_music_for_video(remotion_scenes, total_duration)
                if smart and smart.get("music_file"):
                    music_file = smart["music_file"]
                    music_start_offset = float(smart.get("music_start_offset", 0) or 0)
                    logger.info(f"[Convert] Smart music (local library): {music_file}")
            if not music_file:
                music_file = music_manager.get_music_for_video(remotion_scenes, total_duration)
                if music_file:
                    logger.info(f"[Convert] Background music (random): {music_file}")
        except Exception as _music_err:
            logger.warning(f"[Convert] Music manager unavailable: {_music_err}")

    # Fallback: local mood-mapped files
    if not music_file:
        mood_counts: dict[str, int] = {}
        for s in remotion_scenes:
            m = s.get("mood", "dark")
            mood_counts[m] = mood_counts.get(m, 0) + 1
        dominant_mood = max(mood_counts, key=mood_counts.get) if mood_counts else "dark"
        MOOD_MUSIC = {
            "dark":      "music/dark_01_scp_x1x.mp3",
            "tense":     "music/tense_01_stay_the_course.mp3",
            "dramatic":  "music/dramatic_01_strength_of_titans.mp3",
            "cold":      "music/cold_01_scp_x5x.mp3",
            "reverent":  "music/reverent_01_ancient_rite.mp3",
            "wonder":    "music/wonder_01_the_descent.mp3",
            "warmth":    "music/warmth_01_hearth_and_home.mp3",
            "absurdity": "music/absurdity_01_scheming_weasel.mp3",
        }
        local_file = MOOD_MUSIC.get(dominant_mood, MOOD_MUSIC["dark"])
        if (Path(public_dir) / local_file).exists():
            music_file = local_file
            logger.info(f"[Convert] Background music (local): {music_file} (mood: {dominant_mood})")
        else:
            logger.warning("[Convert] No background music available")

    return music_file, music_start_offset
