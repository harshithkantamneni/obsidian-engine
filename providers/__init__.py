"""
Providers — swappable backends for every external service the pipeline uses.

Each provider type has an abstract base class in providers/base.py and one or
more built-in implementations. The active provider is selected in
obsidian.yaml (``providers.<type>.name``) and loaded via get_provider(). Any
class that extends the right base class can be used by giving its dotted path
(``my_pkg.my_module.MyClass``); ``providers.<type>.options`` is passed to its
constructor as keyword arguments.

Provider types:
  - llm:     Text generation (built-in: anthropic, openai)
  - tts:     Text-to-speech (built-in: elevenlabs, openai, epidemic_sound)
  - images:  Image generation (built-in: fal)
  - footage: Stock footage (built-in: pexels)
  - upload:  Publishing the final video (built-in: local, youtube)
  - music:   Background music (built-in: local, epidemic_sound, or auto)
  - sfx:     Sound effects / ambience (built-in: local, epidemic_sound, or auto)

Self-check: ``python -m providers``. Guide: docs/PROVIDERS.md.
"""

from providers.registry import (
    clear_cache,
    get_provider,
    get_provider_name,
    is_builtin,
    list_providers,
)

__all__ = ["get_provider", "get_provider_name", "is_builtin", "list_providers", "clear_cache"]
