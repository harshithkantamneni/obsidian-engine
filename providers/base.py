"""
Abstract base classes for all provider types.

Every external service the pipeline talks to (LLM, TTS, images, stock footage,
upload, music, SFX) goes through one of these interfaces, so you can bring
your own provider without touching pipeline code:

1. Subclass the relevant base class (e.g. ``TTSProvider``) in any module that
   is importable from the project root (e.g. ``my_providers/tts.py``).
2. Implement every ``@abstractmethod``. The non-abstract helpers
   (``resolve_model``, ``resolve_voice``, ``generate_with_reference``,
   ``sfx_for_scene`` ...) have sensible defaults and are optional.
3. Point obsidian.yaml at it::

       providers:
         tts:
           name: my_providers.tts.MyTTS    # dotted path to the class
           options:                        # passed to MyTTS(**options)
             voice: narrator-1

4. Run ``python -m providers`` to check that everything loads.

See docs/PROVIDERS.md for the full contract of each type.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class LLMProvider(ABC):
    """Abstract base for text generation providers (Claude, GPT, local models)."""

    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_tokens: int = 4000,
        expect_json: bool = True,
        output_schema: dict | None = None,
    ) -> Any:
        """Generate text or structured output.

        Args:
            system_prompt: System/instruction prompt
            user_prompt: User message
            model: Model identifier (provider-specific). None = use default.
            max_tokens: Maximum output tokens
            expect_json: If True, parse response as JSON
            output_schema: JSON schema for structured output (provider enforces if supported)

        Returns:
            Parsed JSON (dict/list) if expect_json=True, else raw string.
        """

    @abstractmethod
    def generate_with_search(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_tokens: int = 4000,
        output_schema: dict | None = None,
    ) -> str:
        """Generate text with web search capability.

        Returns raw text with search results embedded.
        """

    @abstractmethod
    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str | None = None) -> float:
        """Estimate cost in USD for a given token count."""

    def resolve_model(self, tier: str) -> str | None:
        """Map a pipeline quality tier to a model id for this provider.

        The pipeline asks for one of three tiers: ``"premium"`` (creative
        writing), ``"full"`` (reasoning/analysis) or ``"light"`` (formatting,
        classification). Return ``None`` to let ``generate()`` use its default.

        Default: look the tier up in ``self.models`` (a dict) if the provider
        defines one, e.g. from an ``options: {models: {...}}`` block.
        """
        return (getattr(self, "models", None) or {}).get(tier)

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider display name."""


class TTSProvider(ABC):
    """Abstract base for text-to-speech providers (ElevenLabs, OpenAI TTS)."""

    @abstractmethod
    def synthesize(
        self,
        text: str,
        voice_id: str | None = None,
        voice_settings: dict | None = None,
        speed: float = 1.0,
    ) -> tuple[Path, list[dict]]:
        """Generate speech audio from text.

        Args:
            text: Text to speak
            voice_id: Voice identifier (provider-specific). None = use default.
            voice_settings: Voice parameters (stability, style, etc.)
            speed: Playback speed multiplier

        Returns:
            Tuple of (audio_file_path, word_timestamps).
            word_timestamps: list of {"word": str, "start": float, "end": float}
            (seconds from the start of this clip). Return [] if the service
            has no timing data: the pipeline then runs forced alignment
            (Whisper, if installed) or spreads words evenly.
            Any audio format ffmpeg can read is fine; non-MP3 is transcoded.
        """

    @abstractmethod
    def list_voices(self) -> list[dict]:
        """List available voices.

        Returns list of {"id": str, "name": str, "description": str}
        """

    @abstractmethod
    def check_credits(self) -> dict:
        """Check remaining credits/quota.

        Returns {"remaining": int, "limit": int, "unit": str}.
        Return -1 for remaining/limit when the service has no quota endpoint.
        """

    def resolve_voice(self, role: str) -> str | None:
        """Map a narration role to a voice id for this provider.

        ``role`` is ``"narrator"`` (main voice) or ``"quote"`` (quoted
        historical speech). Return ``None`` to let ``synthesize()`` use its
        default voice.

        Default: look the role up in ``self.voices`` (a dict) if defined.
        """
        return (getattr(self, "voices", None) or {}).get(role)

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider display name."""


class ImageProvider(ABC):
    """Abstract base for image generation providers (fal.ai, Replicate, ComfyUI)."""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        style: str | None = None,
        width: int = 1920,
        height: int = 1080,
        seed: int | None = None,
    ) -> Path:
        """Generate an image from a text prompt.

        Args:
            prompt: Image description
            style: Style modifier (provider-specific)
            width: Image width in pixels
            height: Image height in pixels
            seed: Random seed for reproducibility

        Returns:
            Path to the generated image file (JPEG or PNG). The pipeline
            copies it into place, so a temp file is fine.
        """

    #: Set to True if ``generate_with_reference`` is implemented. The pipeline
    #: then uses character reference portraits for visual consistency.
    supports_reference_images: bool = False

    def generate_with_reference(
        self,
        prompt: str,
        reference_image: Path,
        width: int = 1920,
        height: int = 1080,
        seed: int | None = None,
    ) -> Path:
        """Generate an image that keeps the subject of ``reference_image``.

        Optional. Only called when ``supports_reference_images`` is True.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support reference images")

    @abstractmethod
    def estimate_cost(self) -> float:
        """Estimate cost in USD per image generation."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider display name."""


class FootageProvider(ABC):
    """Abstract base for stock footage providers (Pexels, Pixabay)."""

    @abstractmethod
    def search(
        self,
        query: str,
        orientation: str = "landscape",
        min_duration: int = 5,
        max_results: int = 5,
    ) -> list[dict]:
        """Search for stock footage.

        Args:
            query: Search terms
            orientation: "landscape" or "portrait"
            min_duration: Minimum clip duration in seconds
            max_results: Maximum results to return

        Returns:
            List of {"url": str, "duration": int, "width": int, "height": int,
                     "preview_url": str, "credit": str (optional attribution)}
            Best match first; the pipeline uses the first result's ``url``.
        """

    @abstractmethod
    def download(self, url: str, output_path: Path) -> Path:
        """Download a footage file.

        Returns path to the downloaded file.
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider display name."""


class MusicProvider(ABC):
    """Abstract base for music providers (Epidemic Sound, local library)."""

    @abstractmethod
    def search(self, mood: str, duration: float = 600, **kwargs) -> list[dict]:
        """Search for music tracks matching criteria.

        Returns list of {"id": str, "title": str, "artist": str,
                         "duration": float, "bpm": int, "mood": str}
        """

    @abstractmethod
    def download(self, track_id: str, output_path: Path,
                 stem: str | None = None) -> Path:
        """Download a track. stem: None (full), 'BASS', 'DRUMS', 'INSTRUMENTS'."""

    @abstractmethod
    def select_for_video(self, scenes: list[dict],
                         total_duration: float) -> dict | None:
        """Select best track for a video.

        ``scenes`` are the Remotion scenes (``mood``, ``start_time``,
        ``end_time``, ``narrative_position`` ...).

        Returns {"music_file": str, "music_start_offset": float,
                 "track_id": str, "title": str, "bpm": int, "mood": str}
        or None (the pipeline then falls back to the local library).
        Only ``music_file`` is required. It may be a path relative to
        ``remotion/public`` (e.g. ``"music/track.mp3"``) or an absolute path;
        absolute paths are copied into ``remotion/public/music/``.
        """

    def check_status(self) -> dict:
        """Check provider status (API connectivity, subscription)."""
        return {"status": "available"}

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider display name."""


class SFXProvider(ABC):
    """Abstract base for sound effects providers (Epidemic Sound, local files)."""

    @abstractmethod
    def search(self, keyword: str, duration_max: float = 5.0,
               **kwargs) -> list[dict]:
        """Search for sound effects.

        Returns list of {"id": str, "title": str, "duration": float, "tags": list}
        """

    @abstractmethod
    def download(self, sfx_id: str, output_path: Path) -> Path:
        """Download a sound effect file."""

    def check_status(self) -> dict:
        """Check provider status."""
        return {"status": "available"}

    # ── Scene-level helpers used by the pipeline (optional overrides) ────────

    def sfx_for_scene(self, scene: dict, dest_dir: Path) -> str | None:
        """Pick a one-shot sound effect for a key scene.

        ``dest_dir`` is ``remotion/public/sfx``. Return a path relative to
        ``remotion/public`` (e.g. ``"sfx/boom.mp3"``) or None.

        Default: ``search()`` for a mood-based keyword and ``download()`` the
        first hit into ``dest_dir``.
        """
        mood = (scene.get("mood") or "dramatic").lower()
        keyword = scene.get("sfx_keyword") or f"{mood} cinematic impact"
        return self._search_and_fetch(keyword, 5.0, dest_dir, "sfx")

    def ambient_for_scene(self, scene: dict, dest_dir: Path) -> str | None:
        """Pick a looping ambient bed for a scene.

        ``dest_dir`` is ``remotion/public/ambience``. Return a path relative to
        ``remotion/public`` (e.g. ``"ambience/wind.mp3"``) or None.

        Default: ``search()`` for ``"<mood> ambience"`` and ``download()`` the
        first hit into ``dest_dir``.
        """
        mood = (scene.get("mood") or "dark").lower()
        keyword = scene.get("ambient_keyword") or f"{mood} ambience"
        return self._search_and_fetch(keyword, 30.0, dest_dir, "amb")

    def _search_and_fetch(self, keyword: str, duration_max: float,
                          dest_dir: Path, prefix: str) -> str | None:
        cache = self.__dict__.setdefault("_scene_audio_cache", {})
        key = (prefix, keyword)
        if key in cache:
            return cache[key]
        results = self.search(keyword=keyword, duration_max=duration_max)
        if not results:
            cache[key] = None
            return None
        item_id = str(results[0].get("id", ""))
        safe = re.sub(r"[^a-z0-9]+", "_", f"{keyword}_{item_id}".lower()).strip("_")[:60]
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / f"{prefix}_{safe}.mp3"
        if not out.exists():
            got = Path(self.download(item_id, out) or out)
            if got.exists() and got.resolve().parent != dest_dir.resolve():
                import shutil
                out = dest_dir / got.name
                shutil.copy2(got, out)
            else:
                out = got
        rel = f"{dest_dir.name}/{out.name}" if out.exists() else None
        cache[key] = rel
        return rel

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider display name."""


class UploadProvider(ABC):
    """Abstract base for video upload providers (YouTube, local save)."""

    @abstractmethod
    def upload(
        self,
        video_path: Path,
        title: str,
        description: str,
        tags: list[str],
        thumbnail_path: Path | None = None,
    ) -> dict:
        """Upload a video.

        Args:
            video_path: Path to the rendered video
            title: Video title
            description: Video description
            tags: List of tags
            thumbnail_path: Optional custom thumbnail

        Returns:
            {"video_id": str, "url": str, "status": str}
            ``video_id`` must be non-empty: the pipeline treats an empty id
            as a failed upload.
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider display name."""
