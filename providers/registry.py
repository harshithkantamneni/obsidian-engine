"""
Provider registry — instantiate the right provider based on obsidian.yaml config.

Usage:
    from providers.registry import get_provider
    llm = get_provider("llm")       # Returns configured LLMProvider
    tts = get_provider("tts")       # Returns configured TTSProvider
    img = get_provider("images")    # Returns configured ImageProvider
    ftg = get_provider("footage")   # Returns configured FootageProvider
    upl = get_provider("upload")    # Returns configured UploadProvider
    mus = get_provider("music")     # Returns configured MusicProvider
    sfx = get_provider("sfx")       # Returns configured SFXProvider

Configuration (obsidian.yaml):
    providers:
      tts:
        name: elevenlabs                    # a built-in name ...
      images:
        name: my_pkg.my_module.MyImages     # ... or your own class
        options: {api_url: "http://..."}    # passed as MyImages(**options)

Run ``python -m providers`` to check that every configured provider loads.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

from providers.base import (
    FootageProvider,
    ImageProvider,
    LLMProvider,
    MusicProvider,
    SFXProvider,
    TTSProvider,
    UploadProvider,
)

# Maps provider type → (module_path, class_name) for built-in providers
_BUILTIN_PROVIDERS: dict[str, dict[str, tuple[str, str]]] = {
    "llm": {
        "anthropic": ("providers.llm.anthropic", "AnthropicProvider"),
        "openai": ("providers.llm.openai", "OpenAIProvider"),
    },
    "tts": {
        "elevenlabs": ("providers.tts.elevenlabs", "ElevenLabsProvider"),
        "epidemic_sound": ("providers.tts.epidemic", "EpidemicTTSProvider"),
        "openai": ("providers.tts.openai_tts", "OpenAIProvider"),
    },
    "images": {
        "fal": ("providers.images.fal", "FalProvider"),
    },
    "footage": {
        "pexels": ("providers.footage.pexels", "PexelsProvider"),
    },
    "upload": {
        "local": ("providers.upload.local", "LocalSaveProvider"),
        "youtube": ("providers.upload.youtube", "YouTubeUploadProvider"),
    },
    "music": {
        "epidemic_sound": ("providers.music.epidemic", "EpidemicMusicProvider"),
        "local": ("providers.music.local", "LocalMusicProvider"),
    },
    "sfx": {
        "epidemic_sound": ("providers.sfx.epidemic", "EpidemicSFXProvider"),
        "local": ("providers.sfx.local", "LocalSFXProvider"),
    },
}

# Expected base class for each provider type
_BASE_CLASSES: dict[str, type] = {
    "llm": LLMProvider,
    "tts": TTSProvider,
    "images": ImageProvider,
    "footage": FootageProvider,
    "upload": UploadProvider,
    "music": MusicProvider,
    "sfx": SFXProvider,
}

# Default provider for each type (used when config doesn't specify)
_DEFAULTS: dict[str, str] = {
    "llm": "anthropic",
    "tts": "elevenlabs",
    "images": "fal",
    "footage": "pexels",
    "upload": "local",
    "music": "local",
    "sfx": "local",
}

# Singleton cache
_instances: dict[str, Any] = {}


def _load_class(module_path: str, class_name: str) -> type:
    """Import a class from a dotted module path."""
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    return cls


def _load_custom_class(provider_type: str, dotted: str) -> type:
    """Import a user-supplied ``module.path.ClassName`` with helpful errors."""
    module_path, _, class_name = dotted.rpartition(".")
    if not module_path or not class_name:
        raise RuntimeError(
            f"Custom {provider_type} provider '{dotted}' must be 'module.path.ClassName'"
        )
    try:
        mod = importlib.import_module(module_path)
    except ImportError as e:
        raise RuntimeError(
            f"Could not import module '{module_path}' for custom {provider_type} "
            f"provider '{dotted}': {e}. The module must be importable from the "
            f"project root (e.g. a file at {module_path.replace('.', '/')}.py next "
            f"to run_pipeline.py, or an installed package)."
        ) from e
    try:
        return getattr(mod, class_name)
    except AttributeError as e:
        raise RuntimeError(
            f"Module '{module_path}' has no class '{class_name}' "
            f"(configured as {provider_type} provider '{dotted}')"
        ) from e


def _resolve_provider_config(provider_type: str) -> tuple[str, dict]:
    """Read obsidian.yaml to find which provider and options to use.

    Returns (provider_name, options_dict).
    """
    try:
        from core.config import cfg
        providers_section = cfg.get("providers")
        if providers_section:
            section = providers_section.get(provider_type)
            if section:
                name = section.get("name") or section.get("provider")
                options = section.get("options") or {}
                if not isinstance(options, dict):
                    try:
                        options = options.to_dict()
                    except AttributeError:
                        options = {}
                if name:
                    return str(name), dict(options)
    except Exception:
        pass

    return _DEFAULTS.get(provider_type, ""), {}


def _resolve_auto(provider_type: str, name: str) -> str:
    """Resolve "auto" for music/sfx: epidemic_sound if its key is set, else local."""
    if name != "auto" or provider_type not in ("music", "sfx"):
        return name
    builtins = _BUILTIN_PROVIDERS.get(provider_type, {})
    if os.getenv("EPIDEMIC_SOUND_API_KEY") and "epidemic_sound" in builtins:
        try:
            _load_class(*builtins["epidemic_sound"])
            return "epidemic_sound"
        except Exception:
            return "local"
    return "local"


def _check_type(provider_type: str) -> None:
    if provider_type not in _BASE_CLASSES:
        raise ValueError(
            f"Unknown provider type '{provider_type}'. "
            f"Must be one of: {', '.join(_BASE_CLASSES)}"
        )


def get_provider_name(provider_type: str) -> str:
    """Return the configured provider name for a type, with "auto" resolved.

    Built-ins return their short name (e.g. "elevenlabs"); custom providers
    return their dotted path (e.g. "my_pkg.tts.MyTTS").
    """
    _check_type(provider_type)
    name, _ = _resolve_provider_config(provider_type)
    return _resolve_auto(provider_type, name)


def is_builtin(provider_type: str) -> bool:
    """True if the configured provider for this type is a built-in one."""
    return get_provider_name(provider_type) in _BUILTIN_PROVIDERS.get(provider_type, {})


def get_provider(provider_type: str, *, fresh: bool = False) -> Any:
    """Get a configured provider instance.

    Args:
        provider_type: One of "llm", "tts", "images", "footage", "upload",
            "music", "sfx"
        fresh: If True, create a new instance instead of returning cached one

    Returns:
        An instance of the appropriate provider.

    Raises:
        ValueError: If provider_type is unknown
        RuntimeError: If the provider can't be loaded
    """
    _check_type(provider_type)

    if not fresh and provider_type in _instances:
        return _instances[provider_type]

    name, options = _resolve_provider_config(provider_type)
    name = _resolve_auto(provider_type, name)

    builtins = _BUILTIN_PROVIDERS.get(provider_type, {})
    if name in builtins:
        cls = _load_class(*builtins[name])
    elif "." in name:
        # Custom provider: "my_package.my_module.MyProvider"
        cls = _load_custom_class(provider_type, name)
    else:
        raise RuntimeError(
            f"Unknown {provider_type} provider '{name}'. "
            f"Built-in options: {', '.join(builtins)} "
            f"(or a custom 'module.path.ClassName')"
        )

    # Validate it's the right type
    base = _BASE_CLASSES[provider_type]
    if not isinstance(cls, type) or not issubclass(cls, base):
        raise RuntimeError(
            f"Provider {getattr(cls, '__name__', cls)!r} does not extend "
            f"providers.base.{base.__name__}"
        )

    # Instantiate with options
    try:
        instance = cls(**options) if options else cls()
    except TypeError as e:
        keys = ", ".join(sorted(options)) or "(none)"
        raise RuntimeError(
            f"Could not create {provider_type} provider {cls.__name__} with "
            f"options [{keys}]: {e}. Check providers.{provider_type}.options in "
            f"obsidian.yaml — each key is passed as a constructor keyword argument."
        ) from e

    _instances[provider_type] = instance
    return instance


def list_providers(provider_type: str | None = None) -> dict[str, list[str]]:
    """List available built-in providers.

    Args:
        provider_type: If given, list only providers for that type.

    Returns:
        Dict of {provider_type: [provider_names]}
    """
    if provider_type:
        return {provider_type: list(_BUILTIN_PROVIDERS.get(provider_type, {}))}
    return {k: list(v) for k, v in _BUILTIN_PROVIDERS.items()}


def clear_cache() -> None:
    """Clear the provider singleton cache. Useful for testing."""
    _instances.clear()
