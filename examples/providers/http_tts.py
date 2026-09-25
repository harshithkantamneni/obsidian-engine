"""
Example TTS provider: any HTTP text-to-speech service.

A starting point for wiring your own (or a self-hosted) TTS server into the
pipeline. It POSTs {"text", "voice", "speed"} to ``api_url`` and accepts
either raw audio bytes back, or JSON:

    {"audio_base64": "...", "format": "mp3",
     "words": [{"word": "Hello", "start": 0.0, "end": 0.42}, ...]}   # words optional

obsidian.yaml:
    providers:
      tts:
        name: examples.providers.http_tts.HttpTTS
        options:
          api_url: http://localhost:8000/tts
          voice: narrator
          quote_voice: elder          # optional
          api_key_env: MY_TTS_KEY     # optional: sent as "Authorization: Bearer <key>"

If your service returns no word timings, return [] — the pipeline aligns
words itself (Whisper if installed, otherwise an even spread).
"""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

from providers.base import TTSProvider


class HttpTTS(TTSProvider):
    def __init__(self, api_url: str, voice: str = "default", quote_voice: str | None = None,
                 api_key_env: str | None = None, timeout: int = 120):
        self.api_url = api_url
        self.api_key_env = api_key_env
        self.timeout = timeout
        # resolve_voice() (from the base class) looks roles up in self.voices
        self.voices = {"narrator": voice, "quote": quote_voice or voice}

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            key = os.getenv(self.api_key_env)
            if not key:
                raise RuntimeError(f"{self.api_key_env} not set")
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def synthesize(self, text, voice_id=None, voice_settings=None, speed=1.0):
        import requests

        r = requests.post(
            self.api_url,
            headers=self._headers(),
            json={"text": text, "voice": voice_id or self.voices["narrator"], "speed": speed},
            timeout=self.timeout,
        )
        r.raise_for_status()

        words: list[dict] = []
        if r.headers.get("content-type", "").startswith("application/json"):
            data = r.json()
            audio = base64.b64decode(data["audio_base64"])
            suffix = "." + data.get("format", "mp3")
            words = data.get("words") or []
        else:
            audio = r.content
            suffix = ".wav" if "wav" in r.headers.get("content-type", "") else ".mp3"

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio)
        # Any format ffmpeg reads is fine: the pipeline converts to 44.1kHz MP3.
        return Path(tmp.name), words

    def list_voices(self):
        return [{"id": v, "name": v, "description": role} for role, v in self.voices.items()]

    def check_credits(self):
        return {"remaining": -1, "limit": -1, "unit": "characters"}  # unknown

    @property
    def name(self):
        return "HTTP TTS"
