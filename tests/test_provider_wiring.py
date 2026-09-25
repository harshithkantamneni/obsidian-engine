"""End-to-end wiring tests: every external-service call goes through the
provider registry, so a user-supplied ``module.ClassName`` in obsidian.yaml is
what the pipeline actually uses.

Fake providers live in tests/_fake_providers.py and are configured by dotted
path, exactly like a custom provider.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from providers import registry
from tests import _fake_providers as fp

FAKE = "tests._fake_providers"
HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture
def set_provider(monkeypatch):
    """Point providers.<type> in the live cfg at a provider name + options."""
    from core.config import cfg

    providers_cfg = cfg._data.setdefault("providers", {})

    def _set(ptype: str, name: str, options: dict | None = None):
        section = {"name": name}
        if options is not None:
            section["options"] = options
        monkeypatch.setitem(providers_cfg, ptype, section)
        registry.clear_cache()

    registry.clear_cache()
    yield _set
    registry.clear_cache()


# ── Registry ─────────────────────────────────────────────────────────────────

class TestRegistryCustomProviders:
    def test_custom_dotted_path_with_options(self, set_provider, tmp_path):
        set_provider("upload", f"{FAKE}.FakeUpload", {"dest": str(tmp_path)})
        p = registry.get_provider("upload")
        assert isinstance(p, fp.FakeUpload)
        assert p.dest == str(tmp_path)
        assert registry.get_provider_name("upload") == f"{FAKE}.FakeUpload"
        assert registry.is_builtin("upload") is False

    def test_builtin_name_and_auto(self, set_provider, monkeypatch):
        set_provider("sfx", "auto")
        monkeypatch.delenv("EPIDEMIC_SOUND_API_KEY", raising=False)
        assert registry.get_provider_name("sfx") == "local"
        assert registry.is_builtin("sfx") is True

    def test_bad_module_path_error(self, set_provider):
        set_provider("tts", "no_such_pkg.voices.MyTTS")
        with pytest.raises(RuntimeError) as ei:
            registry.get_provider("tts")
        msg = str(ei.value)
        assert "no_such_pkg.voices" in msg
        assert "importable from the project root" in msg

    def test_missing_class_error(self, set_provider):
        set_provider("tts", f"{FAKE}.DoesNotExist")
        with pytest.raises(RuntimeError, match="has no class 'DoesNotExist'"):
            registry.get_provider("tts")

    def test_bad_options_error_lists_keys(self, set_provider):
        set_provider("upload", f"{FAKE}.NeedsKwarg", {"bukket": "x", "region": "eu"})
        with pytest.raises(RuntimeError) as ei:
            registry.get_provider("upload")
        msg = str(ei.value)
        assert "bukket" in msg and "region" in msg
        assert "providers.upload.options" in msg

    def test_wrong_base_class(self, set_provider):
        set_provider("upload", f"{FAKE}.NotAProvider")
        with pytest.raises(RuntimeError, match="UploadProvider"):
            registry.get_provider("upload")

    def test_youtube_is_builtin_upload(self):
        assert "youtube" in registry.list_providers("upload")["upload"]
        from providers.upload.youtube import YouTubeUploadProvider
        assert YouTubeUploadProvider().name == "YouTube"

    def test_self_check_cli(self, set_provider, capsys):
        from providers.__main__ import main
        set_provider("upload", f"{FAKE}.FakeUpload")
        assert main() == 0
        out = capsys.readouterr().out
        assert "FakeUpload" in out and "custom" in out

    def test_self_check_reports_failure(self, set_provider, capsys):
        from providers.__main__ import main
        set_provider("tts", "no_such_pkg.MyTTS")
        assert main() == 1
        assert "ERROR" in capsys.readouterr().out


# ── LLM ──────────────────────────────────────────────────────────────────────

class TestLLMDispatch:
    def test_custom_llm_receives_call_with_tier_model(self, set_provider):
        from clients import claude_client as cc
        set_provider("llm", f"{FAKE}.FakeLLM",
                     {"models": {"premium": "big-model", "light": "small-model"}})
        out = cc.call_claude("sys", "user", model=cc.OPUS, expect_json=True)
        assert out == {"ok": True}
        prov = registry.get_provider("llm")
        assert prov.calls[-1]["model"] == "big-model"
        cc.call_claude("sys", "user", model=cc.HAIKU)
        assert prov.calls[-1]["model"] == "small-model"
        cc.call_claude("sys", "user", model=cc.SONNET)
        assert prov.calls[-1]["model"] is None  # no "full" mapping → provider default
        assert cc.call_claude_with_search("s", "u") == "search text"
        assert prov.calls[-1]["kind"] == "search"

    def test_custom_llm_string_reply_parsed_as_json(self, set_provider):
        from clients import claude_client as cc
        set_provider("llm", f"{FAKE}.FakeLLM", {"reply": '```json\n{"a": 1}\n```'})
        assert cc.call_claude("s", "u", expect_json=True) == {"a": 1}

    def test_agent_wrapper_uses_custom_llm(self, set_provider):
        from core.agent_wrapper import call_agent
        set_provider("llm", f"{FAKE}.FakeLLM", {"reply": {"title": "x"}})
        result = call_agent("seo_agent", system_prompt="s", user_prompt="u", max_tokens=100)
        assert result == {"title": "x"}
        assert registry.get_provider("llm").calls

    def test_default_uses_anthropic_path(self, set_provider, monkeypatch):
        from clients import claude_client as cc
        set_provider("llm", "anthropic")
        fake_client = MagicMock()
        resp = MagicMock()
        resp.content = [MagicMock(text='{"hello": "world"}')]
        resp.stop_reason = "end_turn"
        resp.usage = MagicMock(input_tokens=1, output_tokens=1,
                               cache_read_input_tokens=0, cache_creation_input_tokens=0)
        fake_client.messages.create.return_value = resp
        monkeypatch.setattr(cc, "client", fake_client)
        assert cc.call_claude("sys", "user", model=cc.HAIKU) == {"hello": "world"}
        assert fake_client.messages.create.call_args.kwargs["model"] == cc.HAIKU

    def test_model_tier_mapping(self):
        from clients import claude_client as cc
        assert cc.model_tier(cc.OPUS) == "premium"
        assert cc.model_tier(cc.SONNET) == "full"
        assert cc.model_tier(cc.HAIKU) == "light"
        assert cc.model_tier("some-other-model") == "full"

    def test_anthropic_provider_resolves_tiers(self):
        from clients import claude_client as cc
        from providers.llm.anthropic import AnthropicProvider
        p = AnthropicProvider()
        assert p.resolve_model("premium") == cc.OPUS
        assert p.resolve_model("light") == cc.HAIKU

    def test_openai_llm_options(self):
        from providers.llm.openai import OpenAIProvider
        p = OpenAIProvider(default_model="m", models={"light": "mini"})
        assert p.resolve_model("light") == "mini"
        assert p.resolve_model("premium") is None


# ── TTS ──────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg required")
class TestTTSWiring:
    @pytest.fixture
    def audio_env(self, set_provider, monkeypatch, tmp_path):
        import pipeline.audio as audio_mod
        from core import pipeline_config
        media = tmp_path / "media"
        chunks = media / "chunks"
        chunks.mkdir(parents=True)
        monkeypatch.setattr(audio_mod, "MEDIA_DIR", media)
        monkeypatch.setattr(audio_mod, "CHUNKS_DIR", chunks)
        monkeypatch.setattr(pipeline_config, "ENFORCE_WPM_GATE", False)
        set_provider("tts", f"{FAKE}.FakeTTS")
        return audio_mod, media, chunks

    def test_run_audio_with_custom_tts_aligns_words(self, audio_env, monkeypatch):
        audio_mod, media, chunks = audio_env
        import media.forced_alignment as fa

        def _no_whisper(path, text):
            raise ImportError("whisper not installed")
        monkeypatch.setattr(fa, "align_audio_to_text", _no_whisper)

        script = "The empire fell in silence. Nobody noticed the last emperor leave."
        result = audio_mod.run_audio({"full_script": script})

        tts = registry.get_provider("tts")
        assert tts.calls and tts.calls[0]["voice_id"] == "fake-narrator"
        ts = json.loads(Path(result["timestamps_path"]).read_text())
        words = [w["word"].rstrip(".") for w in ts["words"]]
        assert words == [w.rstrip(".") for w in script.split()]
        assert all(w["end"] > w["start"] for w in ts["words"])
        # Chunk cache semantics preserved; WAV was transcoded to 44.1kHz MP3
        from mutagen.mp3 import MP3
        chunk = chunks / "chunk_00.mp3"
        assert chunk.exists() and (chunks / "chunk_00_ts.json").exists()
        assert MP3(chunk).info.sample_rate == 44100
        assert Path(result["audio_path"]).exists()

    def test_forced_alignment_used_when_available(self, audio_env, monkeypatch):
        audio_mod, media, chunks = audio_env
        import media.forced_alignment as fa
        seen = []

        def _align(path, text):
            seen.append(Path(path).name)
            return [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4}
                    for i, w in enumerate(text.split())]
        monkeypatch.setattr(fa, "align_audio_to_text", _align)
        audio_mod.run_audio({"full_script": "Short test script here."})
        assert seen == ["chunk_00.mp3"]

    def test_quote_role_resolves_quote_voice(self, audio_env, tmp_path):
        audio_mod, _, _ = audio_env
        tts = registry.get_provider("tts")
        words = audio_mod.synthesize_chunk(tts, "He said this.", tmp_path / "q.mp3", role="quote")
        assert tts.calls[-1]["voice_id"] == "fake-quote"
        assert len(words) == 3


class TestTTSProviders:
    def test_openai_tts_uses_options_not_elevenlabs_ids(self, monkeypatch):
        from providers.tts.openai_tts import OpenAIProvider
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        p = OpenAIProvider(model="tts-1-hd", voice="onyx", quote_voice="echo")
        assert p.resolve_voice("narrator") == "onyx"
        assert p.resolve_voice("quote") == "echo"
        post = MagicMock()
        post.return_value.content = b"mp3"
        monkeypatch.setattr("requests.post", post)
        path, words = p.synthesize("Hi there", voice_settings={"stability": 0.3})
        payload = post.call_args.kwargs["json"]
        assert payload["model"] == "tts-1-hd" and payload["voice"] == "onyx"
        assert "stability" not in json.dumps(payload)
        assert words == []
        path.unlink()

    def test_elevenlabs_resolve_voice_and_speed(self, monkeypatch):
        import base64
        from core.pipeline_config import NARRATOR_VOICE_ID, QUOTE_VOICE_ID
        from providers.tts.elevenlabs import ElevenLabsProvider
        p = ElevenLabsProvider()
        assert p.resolve_voice("narrator") == NARRATOR_VOICE_ID
        assert p.resolve_voice("quote") == QUOTE_VOICE_ID
        monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
        resp = MagicMock(status_code=200)
        resp.text = json.dumps({
            "audio_base64": base64.b64encode(b"abc").decode(),
            "alignment": {"characters": list("Hi you"),
                          "character_start_times_seconds": [0, .1, .2, .3, .4, .5],
                          "character_end_times_seconds": [.1, .2, .3, .4, .5, .6]},
        })
        post = MagicMock(return_value=resp)
        monkeypatch.setattr("requests.post", post)
        path, words = p.synthesize("Hi you", voice_settings={"stability": 0.4}, speed=0.8)
        payload = post.call_args.kwargs["json"]
        assert payload["speed"] == 0.8 and payload["voice_settings"]["speed"] == 0.8
        assert payload["model_id"]  # from cfg.voice.model
        assert [w["word"] for w in words] == ["Hi", "you"]
        path.unlink()

    def test_epidemic_failure_reason_from_status(self, monkeypatch):
        import sys
        import types
        from providers.tts.epidemic import EpidemicTTSProvider
        monkeypatch.setenv("EPIDEMIC_SOUND_API_KEY", "k")
        client = MagicMock()
        client.generate_voiceover.return_value = {"voiceover_id": "v1", "status": "GENERATING"}
        client.get_voiceover_status.return_value = {"status": "FAILED", "failure_reason": "bad text"}
        mod = types.ModuleType("clients.epidemic_client")
        mod.EpidemicSoundClient = MagicMock(return_value=client)
        monkeypatch.setitem(sys.modules, "clients.epidemic_client", mod)
        monkeypatch.setattr("time.sleep", lambda s: None)
        with pytest.raises(RuntimeError, match="bad text"):
            EpidemicTTSProvider().synthesize("hello", voice_id="voice")


# ── Images ───────────────────────────────────────────────────────────────────

class TestImagesWiring:
    def test_run_images_with_custom_provider_and_no_scoring(self, set_provider, monkeypatch, tmp_path):
        import pipeline.images as images_mod
        assets = tmp_path / "assets"
        assets.mkdir()
        monkeypatch.setattr(images_mod, "ASSETS_DIR", assets)
        monkeypatch.setattr(images_mod, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr("core.utils.persist_json_to_supabase", lambda *a, **k: None)
        monkeypatch.delenv("FAL_API_KEY", raising=False)
        monkeypatch.delenv("FAL_KEY", raising=False)
        set_provider("images", f"{FAKE}.FakeImages")
        set_provider("llm", f"{FAKE}.FakeLLM")  # not anthropic → no vision scoring

        manifest = {"topic": "Test", "scenes": [
            {"narration": "A hook.", "mood": "dark", "narrative_position": "hook"},
            {"narration": "A reveal.", "mood": "tense", "is_reveal_moment": True},
        ], "visual_bible": {"character_descriptions": {"Caesar": "a general"}}}
        out = images_mod.run_images(manifest)

        imgs = registry.get_provider("images")
        # One image per scene: no retries without scoring, no portraits
        # (FakeImages doesn't support reference images)
        assert len(imgs.calls) == 2
        assert all(Path(s["ai_image"]).exists() for s in out["scenes"])
        audit = json.loads((tmp_path / "image_audit_log.json").read_text())
        assert audit["sources"][0]["source"] == "Fake Images (AI generated)"

    def test_score_skipped_when_llm_not_anthropic(self, set_provider, tmp_path, monkeypatch):
        from pipeline.images import _score_image
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        set_provider("llm", f"{FAKE}.FakeLLM")
        img = tmp_path / "x.jpg"
        img.write_bytes(b"\xff\xd8")
        assert _score_image(img) is None

    def test_fal_builds_expected_requests(self, monkeypatch):
        from providers.images import fal
        monkeypatch.setenv("FAL_API_KEY", "k")
        sub = MagicMock(return_value={"images": [{"url": "http://x/img.jpg"}]})
        monkeypatch.setattr(fal, "_fal_subscribe_with_retry", sub)
        monkeypatch.setattr("pipeline.helpers.download_file",
                            lambda url, path: Path(path).write_bytes(b"\xff\xd8"))
        p = fal.FalProvider()
        p.generate("x", style="recraft").unlink()
        assert sub.call_args[0][0] == "fal-ai/recraft/v3/text-to-image"
        assert sub.call_args[0][1]["image_size"] == {"width": 2560, "height": 1440}
        p.generate("x", style="flux").unlink()
        assert sub.call_args[0][0] == "fal-ai/flux-pro/v1.1-ultra"
        assert sub.call_args[0][1]["aspect_ratio"] == "16:9"
        assert sub.call_args[0][1]["safety_tolerance"] == "2"
        p.generate("x", style="flux", width=1440, height=2560).unlink()
        assert sub.call_args[0][1]["aspect_ratio"] == "9:16"
        import os
        assert os.environ["FAL_KEY"] == "k"


# ── Footage ──────────────────────────────────────────────────────────────────

class TestFootageWiring:
    def test_footage_hunter_uses_custom_provider(self, set_provider, monkeypatch):
        from pipeline.loader import load_agent
        agent = load_agent(Path("09_footage_hunter.py"))
        monkeypatch.setattr(agent, "_footage_cache", {})
        monkeypatch.setattr(agent, "_session_cache", {})
        monkeypatch.setattr(agent, "_save_cache", lambda cache: None)
        set_provider("footage", f"{FAKE}.FakeFootage")

        result = agent.search_stock_video("fire candle flame dark")
        assert result["source"] == f"{FAKE}.FakeFootage"
        assert result["url"].startswith("https://stock.example/")
        assert result["credit"] == "Footage via Fake Stock"
        assert set(result) == {"source", "url", "width", "height", "duration", "credit"}
        # cached by provider name + query
        assert f"{FAKE}.FakeFootage:fire candle flame dark" in agent._session_cache
        assert agent.search_pexels_video is agent.search_stock_video

    def test_pexels_prefers_hd_and_credits(self, monkeypatch):
        from providers.footage.pexels import PexelsProvider
        monkeypatch.setenv("PEXELS_API_KEY", "k")
        resp = MagicMock(status_code=200)
        resp.text = json.dumps({"videos": [{
            "duration": 8, "image": "p.jpg", "user": {"name": "Ana"},
            "video_files": [
                {"quality": "uhd", "width": 3840, "height": 2160, "link": "uhd.mp4"},
                {"quality": "hd", "width": 1920, "height": 1080, "link": "hd.mp4"},
                {"quality": "sd", "width": 640, "height": 360, "link": "sd.mp4"},
            ]}]})
        monkeypatch.setattr("requests.get", MagicMock(return_value=resp))
        res = PexelsProvider().search("x", min_duration=0)
        assert res[0]["url"] == "hd.mp4"
        assert res[0]["credit"] == "Video by Ana on Pexels"


# ── Upload ───────────────────────────────────────────────────────────────────

class TestUploadWiring:
    def test_stage13_uses_custom_upload_and_skips_youtube(self, set_provider, monkeypatch, tmp_path):
        from pipeline.loader import load_agent
        agent = load_agent(Path("11_youtube_uploader.py"))
        video = tmp_path / "x_FINAL_VIDEO.mp4"
        video.write_bytes(b"\x00" * 2048)
        thumb = tmp_path / "thumb.jpg"
        thumb.write_bytes(b"\xff\xd8")
        monkeypatch.setattr(agent, "glob", MagicMock(glob=MagicMock(return_value=[str(video)])))

        def _boom(*a, **k):
            raise AssertionError("YouTube API must not be called")
        for fn in ("upload_video", "get_credentials", "post_sources_comment", "set_endscreen"):
            monkeypatch.setattr(agent, fn, _boom)
        set_provider("upload", f"{FAKE}.FakeUpload")

        seo = {"recommended_title": "The Fall", "tags": ["history", "rome"],
               "description": "About the fall."}
        result = agent.run(seo, {"topic": "Rome"}, {}, research_data={}, thumbnail_path=str(thumb))

        up = registry.get_provider("upload")
        assert up.calls[0]["title"] == "The Fall"
        assert up.calls[0]["tags"] == ["history", "rome"]
        assert up.calls[0]["thumbnail_path"] == thumb
        assert up.calls[0]["video_path"] == video
        assert result["video_id"] == "fake-123" and result["url"]
        assert result["provider"] == f"{FAKE}.FakeUpload"
        from pipeline.validators import validate_stage_output
        ok = validate_stage_output(13, result)
        assert ok is True or (isinstance(ok, tuple) and ok[0])

    def test_post_phase_detects_non_youtube(self):
        from pipeline.phase_post import _uploaded_to_youtube, _youtube_id
        ctx = MagicMock()
        ctx.state = {"stage_13": {"video_id": "local_x", "provider": "local"}}
        assert _uploaded_to_youtube(ctx) is False
        assert _youtube_id(ctx) == ""
        ctx.state = {"stage_13": {"video_id": "abc"}}  # pre-provider state
        assert _uploaded_to_youtube(ctx) is True
        assert _youtube_id(ctx) == "abc"


# ── Music / SFX ──────────────────────────────────────────────────────────────

@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg required")
class TestConvertMusicSfxWiring:
    def test_convert_uses_music_and_sfx_providers(self, set_provider, monkeypatch, tmp_path):
        import pipeline.convert as convert_mod
        media = tmp_path / "media"
        src = tmp_path / "src"
        public = tmp_path / "public"
        for d in (media, src, public):
            d.mkdir()
        monkeypatch.setattr(convert_mod, "MEDIA_DIR", media)
        monkeypatch.setattr(convert_mod, "REMOTION_SRC", src)
        monkeypatch.setattr(convert_mod, "REMOTION_PUBLIC", public)
        monkeypatch.delenv("EPIDEMIC_SOUND_API_KEY", raising=False)
        monkeypatch.setattr("media.music_manager.get_smart_secondary_music", lambda *a, **k: None)
        monkeypatch.setattr("media.music_manager.get_secondary_music", lambda *a, **k: None)
        (media / "narration.mp3").write_bytes(b"ID3")

        track = tmp_path / "elsewhere" / "my_track.mp3"
        track.parent.mkdir()
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=duration=1", str(track)],
                       check=True, capture_output=True)
        set_provider("music", f"{FAKE}.FakeMusic", {"track_path": str(track)})
        set_provider("sfx", f"{FAKE}.FakeSFX")

        manifest = {"scenes": [
            {"narration": "Opening words here.", "mood": "dark"},
            {"narration": "The reveal happens.", "mood": "tense", "is_reveal_moment": True},
        ]}
        vd = convert_mod.run_convert(manifest, {"total_duration_seconds": 20.0})

        assert vd["music_file"] == "music/my_track.mp3"
        assert (public / "music" / "my_track.mp3").exists()
        assert vd["music_start_offset"] == 2.5
        reveal = [s for s in vd["scenes"] if s.get("is_reveal_moment")][0]
        assert reveal["sfx_file"].startswith("sfx/") and (public / reveal["sfx_file"]).exists()
        assert vd["scenes"][0]["ambient_file"].startswith("ambience/")
        assert json.loads((src / "video-data.json").read_text())["music_file"] == "music/my_track.mp3"

    def test_music_provider_failure_falls_back(self, set_provider, tmp_path, monkeypatch):
        from pipeline.audio_assets import select_background_music
        set_provider("music", f"{FAKE}.FakeMusic", {"track_path": ""})
        monkeypatch.setattr("media.music_manager.get_smart_music_for_video", lambda *a, **k: None)
        monkeypatch.setattr("media.music_manager.get_music_for_video", lambda *a, **k: None)
        (tmp_path / "music").mkdir()
        (tmp_path / "music" / "dark_01_scp_x1x.mp3").write_bytes(b"x")
        music, offset = select_background_music([{"mood": "dark"}], 60, tmp_path)
        assert music == "music/dark_01_scp_x1x.mp3" and offset == 0

    def test_builtin_local_sfx_provider_matches_old_behaviour(self):
        from providers.sfx.local import LocalSFXProvider
        from scripts.setup_ambience import get_ambient_file
        from scripts.setup_sfx import get_sfx_file
        p = LocalSFXProvider()
        scene = {"mood": "tense", "location": "", "visual_description": ""}
        assert p.sfx_for_scene(scene, Path("/unused")) == (get_sfx_file("tense") or None)
        assert p.ambient_for_scene(scene, Path("/unused")) == (get_ambient_file("tense") or None)


# ── Shipped examples ─────────────────────────────────────────────────────────

class TestExampleProviders:
    def test_folder_upload_example(self, set_provider, tmp_path):
        set_provider("upload", "examples.providers.folder_upload.CopyToFolder",
                     {"folder": str(tmp_path / "pub")})
        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 10)
        res = registry.get_provider("upload").upload(video, "My Title!", "d", ["t"])
        assert res["video_id"].endswith("my-title")
        assert (tmp_path / "pub" / f"{res['video_id']}.mp4").exists()

    def test_http_tts_example(self, set_provider, monkeypatch):
        import base64
        set_provider("tts", "examples.providers.http_tts.HttpTTS",
                     {"api_url": "http://tts.local/speak", "voice": "n1", "quote_voice": "q1"})
        tts = registry.get_provider("tts")
        assert tts.resolve_voice("quote") == "q1"
        resp = MagicMock()
        resp.headers = {"content-type": "application/json"}
        resp.json.return_value = {"audio_base64": base64.b64encode(b"RIFF").decode(),
                                  "format": "wav", "words": []}
        post = MagicMock(return_value=resp)
        monkeypatch.setattr("requests.post", post)
        path, words = tts.synthesize("hello", voice_id="n1")
        assert path.suffix == ".wav" and words == []
        assert post.call_args.kwargs["json"]["voice"] == "n1"
        path.unlink()
