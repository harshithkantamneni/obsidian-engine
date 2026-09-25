"""Tests for the provider abstraction layer."""

from unittest.mock import patch

import pytest

from providers.base import (
    FootageProvider,
    ImageProvider,
    LLMProvider,
    MusicProvider,
    SFXProvider,
    TTSProvider,
    UploadProvider,
)
from providers import registry


# ── Abstract base class tests ─────────────────────────────────────────────────

class TestProviderBaseClasses:
    """Verify abstract base classes can't be instantiated directly."""

    def test_llm_provider_is_abstract(self):
        with pytest.raises(TypeError):
            LLMProvider()

    def test_tts_provider_is_abstract(self):
        with pytest.raises(TypeError):
            TTSProvider()

    def test_image_provider_is_abstract(self):
        with pytest.raises(TypeError):
            ImageProvider()

    def test_footage_provider_is_abstract(self):
        with pytest.raises(TypeError):
            FootageProvider()

    def test_upload_provider_is_abstract(self):
        with pytest.raises(TypeError):
            UploadProvider()

    def test_music_provider_is_abstract(self):
        with pytest.raises(TypeError):
            MusicProvider()

    def test_sfx_provider_is_abstract(self):
        with pytest.raises(TypeError):
            SFXProvider()


# ── Concrete provider tests ───────────────────────────────────────────────────

class TestLocalSaveProvider:
    """Test the local save upload provider (no external services needed)."""

    def test_instantiation(self, tmp_path):
        from providers.upload.local import LocalSaveProvider
        p = LocalSaveProvider(output_dir=tmp_path)
        assert p.name == "Local Save"

    def test_upload_creates_files(self, tmp_path):
        from providers.upload.local import LocalSaveProvider
        p = LocalSaveProvider(output_dir=tmp_path)

        # Create a fake video file
        video = tmp_path / "input.mp4"
        video.write_bytes(b"fake video data")

        result = p.upload(
            video_path=video,
            title="Test Video",
            description="A test",
            tags=["test", "demo"],
        )

        assert result["status"] == "saved_locally"
        assert result["video_id"].startswith("local_")
        assert result["url"].startswith("file://")

        # Check video was copied
        output_files = list(tmp_path.glob("*.mp4"))
        assert len(output_files) == 2  # input + copied

        # Check metadata was saved
        meta_files = list(tmp_path.glob("*.json"))
        assert len(meta_files) == 1

    def test_upload_with_thumbnail(self, tmp_path):
        from providers.upload.local import LocalSaveProvider
        p = LocalSaveProvider(output_dir=tmp_path)

        video = tmp_path / "input.mp4"
        video.write_bytes(b"fake video data")
        thumb = tmp_path / "thumb.jpg"
        thumb.write_bytes(b"fake thumbnail")

        result = p.upload(
            video_path=video,
            title="With Thumb",
            description="Has thumbnail",
            tags=["test"],
            thumbnail_path=thumb,
        )

        assert result["status"] == "saved_locally"
        thumb_files = list(tmp_path.glob("*.thumb.jpg"))
        assert len(thumb_files) == 1

    def test_default_output_dir(self):
        from providers.upload.local import LocalSaveProvider
        p = LocalSaveProvider()
        assert p._output_dir.exists()

    def test_is_upload_provider(self, tmp_path):
        from providers.upload.local import LocalSaveProvider
        p = LocalSaveProvider(output_dir=tmp_path)
        assert isinstance(p, UploadProvider)


class TestPexelsProvider:
    """Test Pexels provider instantiation (no API calls)."""

    def test_instantiation(self):
        from providers.footage.pexels import PexelsProvider
        p = PexelsProvider()
        assert p.name == "Pexels"
        assert isinstance(p, FootageProvider)

    def test_search_without_key_returns_empty(self):
        from providers.footage.pexels import PexelsProvider
        with patch.dict("os.environ", {}, clear=True):
            p = PexelsProvider()
            p._api_key = None
            results = p.search("nature")
            assert results == []


class TestFalProvider:
    """Test fal.ai provider instantiation."""

    def test_requires_api_key(self):
        """Constructing never raises (so `python -m providers` can list it);
        generating without a key raises a clear error."""
        from providers.images.fal import FalProvider
        with patch.dict("os.environ", {}, clear=True):
            p = FalProvider()
            with pytest.raises(RuntimeError, match="FAL_KEY"):
                p.generate("a castle")

    def test_instantiation_with_key(self):
        from providers.images.fal import FalProvider
        with patch.dict("os.environ", {"FAL_KEY": "test-key"}):
            p = FalProvider()
            assert p.name == "fal.ai"
            assert isinstance(p, ImageProvider)


class TestOpenAITTSProvider:
    """Test OpenAI TTS provider instantiation and mocked generation."""

    def test_instantiation_with_key(self):
        from providers.tts.openai_tts import OpenAIProvider
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-test-key"}):
            p = OpenAIProvider()
            assert p.name == "OpenAI"
            assert isinstance(p, TTSProvider)

            # Check list_voices and check_credits format
            assert len(p.list_voices()) > 5
            assert p.check_credits()["remaining"] == -1

    def test_requires_api_key_for_synthesis(self):
        from providers.tts.openai_tts import OpenAIProvider
        with patch.dict("os.environ", {}, clear=True):
            p = OpenAIProvider()
            with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
                p.synthesize("This should fail")

    @patch("requests.post")
    def test_synthesize_mocked(self, mock_post):
        from providers.tts.openai_tts import OpenAIProvider

        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-test-key"}):
            p = OpenAIProvider()

            mock_response = mock_post.return_value
            mock_response.content = b"fake mp3 audio data"
            mock_response.raise_for_status.return_value = None

            audio_path, timestamps = p.synthesize("Hello world")

            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert kwargs["json"]["input"] == "Hello world"
            assert kwargs["headers"]["Authorization"] == "Bearer fake-test-key"

            assert audio_path.exists()
            assert audio_path.read_bytes() == b"fake mp3 audio data"
            assert timestamps == []

            audio_path.unlink()


class TestEpidemicMusicProvider:
    """Test Epidemic Sound music provider instantiation."""

    def test_instantiation(self):
        from providers.music.epidemic import EpidemicMusicProvider
        with patch.dict("os.environ", {"EPIDEMIC_SOUND_API_KEY": "fake-test-key"}):
            p = EpidemicMusicProvider()
            assert p.name == "Epidemic Sound"
            assert isinstance(p, MusicProvider)

    def test_check_status_no_key(self):
        from providers.music.epidemic import EpidemicMusicProvider
        with patch.dict("os.environ", {}, clear=True):
            p = EpidemicMusicProvider()
            status = p.check_status()
            assert status["status"] == "no_key"


class TestLocalMusicProvider:
    """Test local music library provider."""

    def test_instantiation(self):
        from providers.music.local import LocalMusicProvider
        p = LocalMusicProvider()
        assert p.name == "Local Library"
        assert isinstance(p, MusicProvider)


class TestEpidemicSFXProvider:
    """Test Epidemic Sound SFX provider instantiation."""

    def test_instantiation(self):
        from providers.sfx.epidemic import EpidemicSFXProvider
        with patch.dict("os.environ", {"EPIDEMIC_SOUND_API_KEY": "fake-test-key"}):
            p = EpidemicSFXProvider()
            assert p.name == "Epidemic Sound SFX"
            assert isinstance(p, SFXProvider)

    def test_check_status_no_key(self):
        from providers.sfx.epidemic import EpidemicSFXProvider
        with patch.dict("os.environ", {}, clear=True):
            p = EpidemicSFXProvider()
            status = p.check_status()
            assert status["status"] == "no_key"


class TestLocalSFXProvider:
    """Test local SFX provider."""

    def test_instantiation(self):
        from providers.sfx.local import LocalSFXProvider
        p = LocalSFXProvider()
        assert p.name == "Local SFX"
        assert isinstance(p, SFXProvider)


# ── Registry tests ────────────────────────────────────────────────────────────

class TestProviderRegistry:
    """Test the provider registry/factory."""

    def setup_method(self):
        registry.clear_cache()

    def test_list_all_providers(self):
        result = registry.list_providers()
        assert "llm" in result
        assert "tts" in result
        assert "images" in result
        assert "footage" in result
        assert "upload" in result
        assert "music" in result
        assert "sfx" in result
        assert "anthropic" in result["llm"]
        assert "openai" in result["llm"]
        assert "epidemic_sound" in result["music"]
        assert "local" in result["music"]

    def test_list_single_type(self):
        result = registry.list_providers("tts")
        assert "tts" in result
        assert "elevenlabs" in result["tts"]
        assert len(result) == 1

    def test_list_unknown_type(self):
        result = registry.list_providers("nonexistent")
        assert result == {"nonexistent": []}

    def test_unknown_provider_type_raises(self):
        with pytest.raises(ValueError, match="Unknown provider type"):
            registry.get_provider("nonexistent")

    def test_get_upload_provider(self):
        provider = registry.get_provider("upload")
        assert isinstance(provider, UploadProvider)
        assert provider.name == "Local Save"

    def test_caching(self):
        p1 = registry.get_provider("upload")
        p2 = registry.get_provider("upload")
        assert p1 is p2

    def test_fresh_bypasses_cache(self):
        p1 = registry.get_provider("upload")
        p2 = registry.get_provider("upload", fresh=True)
        assert p1 is not p2

    def test_clear_cache(self):
        p1 = registry.get_provider("upload")
        registry.clear_cache()
        p2 = registry.get_provider("upload")
        assert p1 is not p2

    def test_get_footage_provider(self):
        provider = registry.get_provider("footage")
        assert isinstance(provider, FootageProvider)
        assert provider.name == "Pexels"

    def test_get_image_provider_with_key(self):
        with patch.dict("os.environ", {"FAL_KEY": "test"}):
            provider = registry.get_provider("images", fresh=True)
            assert isinstance(provider, ImageProvider)

    def test_custom_provider_bad_path_raises(self):
        with patch.object(registry, "_resolve_provider_config", return_value=("bad_name", {})):
            with pytest.raises(RuntimeError, match="Unknown"):
                registry.get_provider("upload", fresh=True)

    def test_defaults_dict_complete(self):
        """Every provider type has a default."""
        for ptype in registry._BASE_CLASSES:
            assert ptype in registry._DEFAULTS

    def test_builtins_dict_complete(self):
        """Every provider type has at least one built-in."""
        for ptype in registry._BASE_CLASSES:
            assert ptype in registry._BUILTIN_PROVIDERS
            assert len(registry._BUILTIN_PROVIDERS[ptype]) >= 1


# ── Config integration tests ─────────────────────────────────────────────────

class TestProviderConfig:
    """Test that obsidian.yaml providers section is read correctly."""

    def setup_method(self):
        registry.clear_cache()

    def test_resolve_defaults(self):
        name, options = registry._resolve_provider_config("upload")
        assert name == "local"

    def test_resolve_with_config(self):
        """If config has providers.upload.name, it should be used."""
        name, options = registry._resolve_provider_config("llm")
        # Should get "anthropic" from either config or defaults
        assert name in ("anthropic", "openai")

    def test_resolve_unknown_type_returns_empty(self):
        name, options = registry._resolve_provider_config("nonexistent")
        assert name == ""
        assert options == {}
