"""Fake "bring your own" providers used by tests/test_provider_wiring.py.

Configured by dotted path (e.g. ``tests._fake_providers.FakeTTS``) exactly
like a user's custom provider would be.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from providers.base import (
    FootageProvider,
    ImageProvider,
    LLMProvider,
    MusicProvider,
    SFXProvider,
    TTSProvider,
    UploadProvider,
)


class FakeLLM(LLMProvider):
    def __init__(self, models: dict | None = None, reply=None):
        self.models = models or {}
        self.reply = reply if reply is not None else {"ok": True}
        self.calls: list[dict] = []

    def generate(self, system_prompt, user_prompt, model=None, max_tokens=4000,
                 expect_json=True, output_schema=None):
        self.calls.append({"kind": "generate", "model": model, "system": system_prompt,
                           "user": user_prompt, "expect_json": expect_json,
                           "output_schema": output_schema})
        return self.reply

    def generate_with_search(self, system_prompt, user_prompt, model=None,
                             max_tokens=4000, output_schema=None):
        self.calls.append({"kind": "search", "model": model})
        return "search text"

    def estimate_cost(self, input_tokens, output_tokens, model=None):
        return 0.0

    @property
    def name(self):
        return "Fake LLM"


class FakeTTS(TTSProvider):
    """Returns a 22.05kHz WAV sine tone (not the pipeline's MP3 format) and
    no timestamps, to exercise transcoding + alignment."""

    def __init__(self, seconds_per_word: float = 0.4, voices: dict | None = None):
        self.seconds_per_word = seconds_per_word
        self.voices = voices or {"narrator": "fake-narrator", "quote": "fake-quote"}
        self.calls: list[dict] = []

    def synthesize(self, text, voice_id=None, voice_settings=None, speed=1.0):
        self.calls.append({"text": text, "voice_id": voice_id, "speed": speed})
        dur = max(1.0, len(text.split()) * self.seconds_per_word)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out = Path(tmp.name)
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
             "-ar", "22050", str(out)],
            check=True, capture_output=True,
        )
        return out, []

    def list_voices(self):
        return [{"id": "fake-narrator", "name": "Fake", "description": ""}]

    def check_credits(self):
        return {"remaining": -1, "limit": -1, "unit": "characters"}

    @property
    def name(self):
        return "Fake TTS"


class FakeImages(ImageProvider):
    def __init__(self):
        self.calls: list[dict] = []

    def generate(self, prompt, style=None, width=1920, height=1080, seed=None):
        from PIL import Image
        self.calls.append({"prompt": prompt, "style": style, "width": width, "height": height})
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            out = Path(tmp.name)
        Image.new("RGB", (width, height), (40, 30, 20)).save(out, "JPEG")
        return out

    def estimate_cost(self):
        return 0.0

    @property
    def name(self):
        return "Fake Images"


class FakeFootage(FootageProvider):
    def __init__(self):
        self.queries: list[str] = []

    def search(self, query, orientation="landscape", min_duration=5, max_results=5):
        self.queries.append(query)
        return [{"url": f"https://stock.example/{query.replace(' ', '_')}.mp4",
                 "duration": 12, "width": 1920, "height": 1080, "preview_url": ""}]

    def download(self, url, output_path):
        Path(output_path).write_bytes(b"video")
        return Path(output_path)

    @property
    def name(self):
        return "Fake Stock"


class FakeUpload(UploadProvider):
    def __init__(self, dest: str | None = None):
        self.dest = dest
        self.calls: list[dict] = []

    def upload(self, video_path, title, description, tags, thumbnail_path=None):
        self.calls.append({"video_path": Path(video_path), "title": title,
                           "description": description, "tags": list(tags),
                           "thumbnail_path": thumbnail_path})
        return {"video_id": "fake-123", "url": "https://videos.example/fake-123",
                "status": "published"}

    @property
    def name(self):
        return "Fake Upload"


class FakeMusic(MusicProvider):
    """select_for_video returns an absolute path outside remotion/public."""

    track_path: str = ""

    def __init__(self, track_path: str = ""):
        self.track_path = track_path

    def search(self, mood, duration=600, **kwargs):
        return []

    def download(self, track_id, output_path, stem=None):
        raise NotImplementedError

    def select_for_video(self, scenes, total_duration):
        return {"music_file": self.track_path, "music_start_offset": 2.5}

    @property
    def name(self):
        return "Fake Music"


class FakeSFX(SFXProvider):
    """Uses the base-class default sfx_for_scene/ambient_for_scene."""

    def __init__(self):
        self.downloads: list[str] = []

    def search(self, keyword, duration_max=5.0, **kwargs):
        return [{"id": "42", "title": keyword, "duration": 1.0, "tags": []}]

    def download(self, sfx_id, output_path):
        self.downloads.append(sfx_id)
        Path(output_path).write_bytes(b"ID3fake")
        return Path(output_path)

    @property
    def name(self):
        return "Fake SFX"


class NeedsKwarg(UploadProvider):
    def __init__(self, bucket: str):
        self.bucket = bucket

    def upload(self, video_path, title, description, tags, thumbnail_path=None):
        return {"video_id": "x", "url": "", "status": "ok"}

    @property
    def name(self):
        return "Needs kwarg"


class NotAProvider:
    pass
