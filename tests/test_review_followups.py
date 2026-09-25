"""Follow-up fixes from the independent review of the provider/security branch."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_atomic_write_json_follows_symlink(tmp_path):
    from core.utils import atomic_write_json
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "lessons_learned.json"
    target.write_text("{}")
    link = tmp_path / "lessons_learned.json"
    link.symlink_to(target)
    atomic_write_json(link, {"a": 1})
    assert link.is_symlink(), "symlink into ./data must survive the write"
    assert json.loads(target.read_text()) == {"a": 1}


def test_scheduler_atomic_write_follows_symlink(tmp_path):
    import scheduler
    target = tmp_path / "real.json"
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    scheduler._atomic_write_json(link, {"b": 2})
    assert link.is_symlink()
    assert json.loads(target.read_text()) == {"b": 2}


def _capture_elevenlabs_payload(voice_settings, speed):
    from providers.tts.elevenlabs import ElevenLabsProvider
    p = ElevenLabsProvider()  # key comes from ELEVENLABS_API_KEY (set in CI env)
    captured = {}

    def fake_post(url, payload):
        captured.update(payload)
        import base64
        return {"audio_base64": base64.b64encode(b"ID3").decode(), "alignment": {}}

    with patch.object(p, "_post_with_retry", side_effect=fake_post):
        p.synthesize("hello world", voice_id="v", voice_settings=voice_settings, speed=speed)
    return captured


def test_elevenlabs_does_not_inject_speed_into_voice_settings():
    payload = _capture_elevenlabs_payload({"stability": 0.4}, 0.85)
    assert payload["speed"] == 0.85
    assert "speed" not in payload["voice_settings"]


def test_elevenlabs_passes_through_pipeline_speed():
    payload = _capture_elevenlabs_payload({"stability": 0.4, "speed": 0.78}, 0.78)
    assert payload["voice_settings"]["speed"] == 0.78


def test_scheduler_warns_when_youtube_token_but_local_upload(tmp_path, capsys, monkeypatch):
    import scheduler
    monkeypatch.setenv("YOUTUBE_TOKEN_JSON", "{}")
    with patch("providers.registry.get_provider_name", return_value="local"):
        scheduler._warn_if_youtube_upload_disabled()
    assert "will NOT be published" in capsys.readouterr().out


def test_scheduler_no_warning_when_youtube(monkeypatch, capsys):
    import scheduler
    monkeypatch.setenv("YOUTUBE_TOKEN_JSON", "{}")
    with patch("providers.registry.get_provider_name", return_value="youtube"):
        scheduler._warn_if_youtube_upload_disabled()
    assert "WARNING" not in capsys.readouterr().out
