"""
OpenAI TTS provider.

OpenAI's speech endpoint returns audio only (no timing data), so synthesize()
returns [] word timestamps and the pipeline derives them with forced
alignment (Whisper, if installed) or an even spread.

ElevenLabs-specific settings (voice.narrator_id, voice.model, stability/style
voice_settings) are ignored — configure this provider with options instead:

    providers:
      tts:
        name: openai
        options:
          model: tts-1          # or tts-1-hd, gpt-4o-mini-tts
          voice: onyx           # narrator voice
          quote_voice: echo     # optional voice for quoted speech
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from providers.base import TTSProvider

_VOICES = [
    "alloy", "ash", "ballad", "coral", "echo", "fable",
    "onyx", "nova", "sage", "shimmer", "verse", "marin", "cedar",
]


class OpenAIProvider(TTSProvider):
    """OpenAI text-to-speech provider."""

    API_URL = "https://api.openai.com/v1/audio/speech"

    def __init__(self, model: str = "tts-1", voice: str = "alloy",
                 quote_voice: str | None = None, api_key_env: str = "OPENAI_API_KEY"):
        self._model = model
        self._api_key_env = api_key_env
        self.voices = {"narrator": voice, "quote": quote_voice or voice}

    @property
    def _api_key(self) -> str | None:
        return os.getenv(self._api_key_env)

    def synthesize(
        self,
        text: str,
        voice_id: str | None = None,
        voice_settings: dict | None = None,  # ElevenLabs-specific — ignored
        speed: float = 1.0,
    ) -> tuple[Path, list[dict]]:
        import requests

        if not self._api_key:
            raise RuntimeError(f"{self._api_key_env} not set — required for OpenAI TTS")

        payload = {
            "input": text,
            "model": self._model,
            "voice": voice_id or self.voices["narrator"],
            "response_format": "mp3",
        }
        if speed and speed != 1.0:
            payload["speed"] = max(0.25, min(4.0, float(speed)))

        r = requests.post(
            self.API_URL,
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        r.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(r.content)
        # No timestamps from OpenAI — the pipeline aligns words itself.
        return Path(tmp.name), []

    def list_voices(self) -> list[dict]:
        """Returns the built-in OpenAI voices."""
        return [
            {"id": v, "name": v.capitalize(), "description": "OpenAI built-in voice"}
            for v in _VOICES
        ]

    def check_credits(self) -> dict:
        """OpenAI has no simple TTS quota endpoint (billing is org-wide USD)."""
        return {"remaining": -1, "limit": -1, "unit": "USD (check OpenAI dashboard)"}

    @property
    def name(self) -> str:
        return "OpenAI"
