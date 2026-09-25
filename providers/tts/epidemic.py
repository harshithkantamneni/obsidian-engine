"""
Epidemic Sound TTS provider — AI voiceover as ElevenLabs alternative.

Uses Epidemic Sound's voiceover API with async generation (submit → poll → download).
Word timestamps are extracted via Whisper forced alignment since Epidemic may not
provide character-level timing natively.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from providers.base import TTSProvider


class EpidemicTTSProvider(TTSProvider):
    """Epidemic Sound AI voiceover provider."""

    def __init__(self):
        self._api_key = os.getenv("EPIDEMIC_SOUND_API_KEY")
        self._default_voice_id = None

    def synthesize(
        self,
        text: str,
        voice_id: str | None = None,
        voice_settings: dict | None = None,
        speed: float = 1.0,
    ) -> tuple[Path, list[dict]]:
        """Generate speech via Epidemic Sound voiceover API.

        Returns (audio_path, word_timestamps) matching TTSProvider contract.
        Word timestamps use Whisper forced alignment as fallback.
        """
        import tempfile

        from clients.epidemic_client import EpidemicSoundClient

        if not self._api_key:
            raise RuntimeError("EPIDEMIC_SOUND_API_KEY not set")

        client = EpidemicSoundClient()

        # Resolve voice ID
        effective_voice_id = voice_id or self._default_voice_id
        if not effective_voice_id:
            voices = client.browse_voices(limit=1)
            if voices:
                effective_voice_id = voices[0].get("id", "")
                self._default_voice_id = effective_voice_id
            if not effective_voice_id:
                raise RuntimeError("No Epidemic Sound voices available")

        # Map speed: ElevenLabs uses 0.5-2.0, Epidemic uses -1.0 to +1.0
        # ElevenLabs default 1.0 → Epidemic 0.0
        epidemic_speed = max(-1.0, min(1.0, (speed - 1.0) * 2.0))

        # Submit voiceover generation
        result = client.generate_voiceover(
            effective_voice_id, text,
            speed=epidemic_speed,
        )
        voiceover_id = result.get("voiceover_id", "")
        if not voiceover_id:
            raise RuntimeError(f"No voiceover_id returned: {result}")

        # Poll for completion
        deadline = time.time() + 60
        status = result.get("status", "GENERATING")
        status_data = result
        while time.time() < deadline and status == "GENERATING":
            time.sleep(3)
            status_data = client.get_voiceover_status(voiceover_id)
            status = status_data.get("status", "UNKNOWN")

        if status == "FAILED":
            reason = status_data.get("failure_reason", "unknown")
            raise RuntimeError(f"Voiceover generation failed: {reason}")
        if status != "DONE":
            raise RuntimeError(f"Voiceover generation timed out: {status}")

        # Download audio
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()
        audio_path = Path(tmp.name)
        client.download_voiceover(voiceover_id, audio_path)

        if not audio_path.exists() or audio_path.stat().st_size < 1000:
            raise RuntimeError("Voiceover download failed or empty")

        # Extract word timestamps
        timestamps = self._extract_timestamps(audio_path, text)

        return audio_path, timestamps

    def _extract_timestamps(self, audio_path: Path, text: str) -> list[dict]:
        """Extract word-level timestamps from generated audio.

        Tries Whisper forced alignment first, falls back to even distribution.
        """
        try:
            from media.forced_alignment import align_audio_to_text
            return align_audio_to_text(audio_path, text)
        except ImportError:
            pass
        except Exception:
            pass

        # Fallback: even distribution based on audio duration
        return self._even_distribution(audio_path, text)

    def _even_distribution(self, audio_path: Path, text: str) -> list[dict]:
        """Distribute words evenly across audio duration as last resort."""
        try:
            import mutagen
            audio = mutagen.File(str(audio_path))
            duration = audio.info.length if audio and audio.info else 10.0
        except Exception:
            duration = 10.0

        words = text.split()
        if not words:
            return []

        per_word = duration / len(words)
        return [
            {
                "word": w,
                "start": round(i * per_word, 3),
                "end": round((i + 1) * per_word, 3),
            }
            for i, w in enumerate(words)
        ]

    def list_voices(self) -> list[dict]:
        if not self._api_key:
            return []
        try:
            from clients.epidemic_client import EpidemicSoundClient
            client = EpidemicSoundClient()
            voices = client.browse_voices(limit=20)
            return [
                {
                    "id": v.get("id", ""),
                    "name": v.get("title", ""),
                    "description": v.get("language", ""),
                }
                for v in voices
            ]
        except Exception:
            return []

    def check_credits(self) -> dict:
        if not self._api_key:
            return {"remaining": 0, "limit": 0, "unit": "generations"}
        try:
            from clients.epidemic_client import EpidemicSoundClient
            client = EpidemicSoundClient()
            sub = client.check_subscription()
            return {
                "remaining": sub.get("voiceover_remaining", -1),
                "limit": sub.get("voiceover_limit", -1),
                "unit": "generations",
            }
        except Exception:
            return {"remaining": -1, "limit": -1, "unit": "generations"}

    @property
    def name(self) -> str:
        return "Epidemic Sound"
