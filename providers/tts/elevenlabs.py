"""
ElevenLabs TTS provider — default implementation.

Uses the ElevenLabs ``/text-to-speech/{voice}/with-timestamps`` endpoint, which
returns MP3 audio plus character-level alignment that we fold into word
timestamps for captions.

obsidian.yaml:
    providers:
      tts:
        name: elevenlabs
        # options:
        #   model: eleven_v3          # default: voice.model
        #   voices: {narrator: <id>, quote: <id>}   # default: voice.narrator_id / voice.quote_id
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from pathlib import Path

from core.log import get_logger
from providers.base import TTSProvider

logger = get_logger(__name__)

API_BASE = "https://api.elevenlabs.io/v1"


def alignment_to_words(data: dict) -> list[dict]:
    """Fold ElevenLabs character alignment into word timestamps.

    Robust to missing/None/non-dict alignment, newline separators and
    truncated timing arrays.
    """
    alignment = data.get("alignment") or {}
    if not isinstance(alignment, dict):
        alignment = {}
    chars = alignment.get("characters", []) or []
    starts = alignment.get("character_start_times_seconds", []) or []
    ends = alignment.get("character_end_times_seconds", []) or []

    words: list[dict] = []
    word, word_start, last_end = "", None, 0.0
    for j, ch in enumerate(chars):
        if ch in (" ", "\n"):
            if word and word_start is not None:
                end_idx = j - 1
                word_end = ends[end_idx] if 0 <= end_idx < len(ends) else word_start + max(0.15, len(word) * 0.08)
                words.append({"word": word, "start": round(word_start, 3), "end": round(word_end, 3)})
                word, word_start = "", None
        else:
            if word_start is None and j < len(starts):
                word_start = starts[j]
            word += ch
            if j < len(ends):
                last_end = ends[j]
    if word and word_start is not None:
        words.append({"word": word, "start": round(word_start, 3), "end": round(last_end, 3)})
    return words


class ElevenLabsProvider(TTSProvider):
    """ElevenLabs text-to-speech provider."""

    MAX_ATTEMPTS = 5

    def __init__(self, model: str | None = None, voices: dict | None = None,
                 api_key_env: str = "ELEVENLABS_API_KEY"):
        self._model = model
        self._voices = dict(voices or {})
        self._api_key_env = api_key_env

    # Read lazily so a key added after startup (or in tests) is picked up
    @property
    def _api_key(self) -> str | None:
        return os.getenv(self._api_key_env)

    def _model_id(self) -> str:
        if self._model:
            return self._model
        try:
            from core.config import cfg
            return cfg.voice.model or "eleven_v3"
        except Exception:
            return "eleven_v3"

    def resolve_voice(self, role: str) -> str | None:
        if role in self._voices:
            return self._voices[role]
        try:
            from core.pipeline_config import NARRATOR_VOICE_ID, QUOTE_VOICE_ID
        except Exception:
            return None
        return QUOTE_VOICE_ID if role == "quote" else NARRATOR_VOICE_ID

    def _post_with_retry(self, url: str, payload: dict) -> dict:
        """POST with retry/backoff: 429 → 60s·n, 5xx/timeouts → 30s·n, 5 attempts."""
        import requests

        headers = {"xi-api-key": self._api_key, "Content-Type": "application/json"}
        last_err: Exception | None = None
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=120)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as ce:
                wait = 30 * (attempt + 1)
                logger.warning(f"  Connection/timeout error, waiting {wait}s...")
                time.sleep(wait)
                last_err = ce
                continue
            if r.status_code == 200:
                return json.loads(r.text, strict=False)
            elif r.status_code == 429:
                wait = 60 * (attempt + 1)
                logger.warning(f"  Rate limited, waiting {wait}s (attempt {attempt+1}/{self.MAX_ATTEMPTS})...")
                time.sleep(wait)
                last_err = Exception("ElevenLabs rate limit (429)")
            elif r.status_code in (500, 502, 503):
                wait = 30 * (attempt + 1)
                logger.warning(f"  Server error ({r.status_code}), waiting {wait}s (attempt {attempt+1}/{self.MAX_ATTEMPTS})...")
                time.sleep(wait)
                last_err = Exception(f"ElevenLabs server error ({r.status_code})")
            else:
                raise Exception(f"ElevenLabs {r.status_code}: {r.text[:200]}")
        raise last_err or Exception(f"Failed after {self.MAX_ATTEMPTS} attempts")

    def synthesize(
        self,
        text: str,
        voice_id: str | None = None,
        voice_settings: dict | None = None,
        speed: float = 1.0,
    ) -> tuple[Path, list[dict]]:
        if not self._api_key:
            raise RuntimeError(f"{self._api_key_env} not set — cannot generate audio with ElevenLabs")

        voice_id = voice_id or self.resolve_voice("narrator")
        if voice_settings is None:
            from core.config import cfg
            voice_settings = cfg.voice.body.to_dict()
        vs = dict(voice_settings)
        vs.setdefault("speed", speed)

        payload = {
            "text": text,
            "model_id": self._model_id(),
            "voice_settings": vs,
            "speed": speed,
        }
        data = self._post_with_retry(f"{API_BASE}/text-to-speech/{voice_id}/with-timestamps", payload)

        audio_bytes = base64.b64decode(data.get("audio_base64", "") or "")
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(audio_bytes)
        return Path(tmp.name), alignment_to_words(data)

    def list_voices(self) -> list[dict]:
        import requests
        if not self._api_key:
            return []
        r = requests.get(
            f"{API_BASE}/voices",
            headers={"xi-api-key": self._api_key},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        voices = json.loads(r.text, strict=False).get("voices", [])
        return [
            {"id": v["voice_id"], "name": v["name"], "description": v.get("description", "")}
            for v in voices
        ]

    def check_credits(self) -> dict:
        """Character quota. Raises on 401 (invalid key / quota exceeded)."""
        import requests
        if not self._api_key:
            return {"remaining": 0, "limit": 0, "unit": "characters"}
        r = requests.get(
            f"{API_BASE}/user",
            headers={"xi-api-key": self._api_key},
            timeout=10,
        )
        if r.status_code == 401:
            raise Exception("ElevenLabs API key invalid or quota exceeded (401)")
        if r.status_code != 200:
            return {"remaining": 0, "limit": 0, "unit": "characters"}
        sub = json.loads(r.text, strict=False).get("subscription", {})
        limit = sub.get("character_limit", 0)
        used = sub.get("character_count", 0)
        return {"remaining": limit - used, "limit": limit, "unit": "characters"}

    @property
    def name(self) -> str:
        return "ElevenLabs"
