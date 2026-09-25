"""
Profile loader — reads channel style profiles from profiles/*.yaml.

The active profile is set in obsidian.yaml:
    profile: documentary

Or via environment variable:
    OBSIDIAN_PROFILE=explainer

Usage:
    from core.profile import get_profile, get_style_directive

    profile = get_profile()              # Full profile dict
    directive = get_style_directive()    # Style block for prompt injection
"""

from __future__ import annotations

import os
import re
import yaml
from pathlib import Path

from core.log import get_logger

logger = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
PROFILES_DIR = BASE_DIR / "profiles"

# Cache
_active_profile: dict | None = None
_active_name: str | None = None


def _resolve_profile_name() -> str:
    """Determine which profile to load. Priority: env var > obsidian.yaml > default."""
    # 1. Environment variable
    env_profile = os.getenv("OBSIDIAN_PROFILE")
    if env_profile:
        return env_profile

    # 2. obsidian.yaml
    try:
        from core.config import cfg
        profile_name = cfg.get("profile", None)
        if profile_name:
            return profile_name
    except Exception:
        pass

    # 3. Default
    return "documentary"


_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _load_profile(name: str) -> dict:
    """Load a profile YAML file by name."""
    # Reject path separators / traversal ("../x", "/etc/x") — names only.
    if not isinstance(name, str) or not _PROFILE_NAME_RE.match(name):
        logger.warning(f"[Profile] Invalid profile name {name!r}, falling back to documentary")
        name = "documentary"
    profile_path = PROFILES_DIR / f"{name}.yaml"
    if not profile_path.exists():
        logger.warning(f"[Profile] '{name}' not found at {profile_path}, falling back to documentary")
        profile_path = PROFILES_DIR / "documentary.yaml"
        if not profile_path.exists():
            logger.warning("[Profile] No profiles found — using empty profile")
            return {"name": "default", "style_directive": ""}

    with open(profile_path) as f:
        data = yaml.safe_load(f) or {}

    return data


def get_profile() -> dict:
    """Get the active profile as a dict. Cached after first load."""
    global _active_profile, _active_name
    if _active_profile is not None:
        return _active_profile

    name = _resolve_profile_name()
    _active_profile = _load_profile(name)
    _active_name = name
    logger.info(f"[Profile] Loaded: {_active_profile.get('name', name)}")
    return _active_profile


def get_style_directive() -> str:
    """Get the style directive block for prompt injection.

    This is the single most important output — it gets prepended to every
    agent's system prompt to set the tone, structure, and approach.
    """
    profile = get_profile()
    directive = profile.get("style_directive", "")
    if isinstance(directive, str):
        return directive.strip()
    return ""


def get_profile_field(section: str, key: str, default=None):
    """Get a specific field from the active profile.

    Example:
        get_profile_field("narrative", "hook_style", "cold_open")
        get_profile_field("visuals", "mood_palette", ["dark", "dramatic"])
    """
    profile = get_profile()
    section_data = profile.get(section, {})
    if isinstance(section_data, dict):
        return section_data.get(key, default)
    return default


def get_mood_palette() -> list[str]:
    """Get the visual mood palette for the active profile."""
    return get_profile_field("visuals", "mood_palette", [
        "dark", "tense", "reverent", "cold", "dramatic", "wonder", "warmth", "absurdity"
    ])


def get_hook_registers() -> list[str]:
    """Get valid hook registers for the active profile."""
    return get_profile_field("narrative", "hook_registers", [
        "TENSION", "DREAD_THROUGH_BEAUTY", "MYSTERY", "INTIMACY"
    ])


def get_structure_types() -> list[str]:
    """Get valid narrative structure types for the active profile."""
    return get_profile_field("narrative", "structure_types", [
        "CLASSIC", "MYSTERY", "DUAL_TIMELINE", "COUNTDOWN", "TRIAL", "REFRAME"
    ])


def reset_profile_cache():
    """Reset cached profile (call when config changes or at pipeline start)."""
    global _active_profile, _active_name
    _active_profile = None
    _active_name = None
