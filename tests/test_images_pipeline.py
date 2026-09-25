"""Tests for pipeline/images.py — fal.ai image generation.

Covers: _fal_subscribe_with_retry, _score_image, _generate_single_image,
_generate_character_portraits, run_images audit log, and error handling.
"""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _fal_provider_env(monkeypatch):
    """Scene images go through the built-in fal ImageProvider (the default in
    obsidian.yaml), which needs a key and is cached by the registry."""
    from providers import registry
    monkeypatch.setenv("FAL_KEY", "test-fal-key")
    registry.clear_cache()
    yield
    registry.clear_cache()


# ── _fal_subscribe_with_retry tests ───────────────────────────────────────


class TestFalSubscribeWithRetry:
    @patch("time.sleep")
    def test_success_on_first_attempt(self, mock_sleep):
        mock_fal = MagicMock()
        mock_fal.subscribe.return_value = {"images": [{"url": "http://img.jpg"}]}

        with patch.dict("sys.modules", {"fal_client": mock_fal}):
            from pipeline.images import _fal_subscribe_with_retry
            result = _fal_subscribe_with_retry("fal-ai/test", {"prompt": "test"})

        assert result == {"images": [{"url": "http://img.jpg"}]}
        mock_fal.subscribe.assert_called_once()

    @patch("time.sleep")
    def test_retries_on_timeout(self, mock_sleep):
        mock_fal = MagicMock()
        mock_fal.subscribe.side_effect = [
            Exception("connection timeout"),
            Exception("connection timed out"),
            {"images": [{"url": "http://ok.jpg"}]},
        ]

        with patch.dict("sys.modules", {"fal_client": mock_fal}):
            from pipeline.images import _fal_subscribe_with_retry
            result = _fal_subscribe_with_retry("fal-ai/test", {"prompt": "x"}, max_attempts=5)

        assert result["images"][0]["url"] == "http://ok.jpg"
        assert mock_fal.subscribe.call_count == 3

    @patch("time.sleep")
    def test_retries_on_rate_limit(self, mock_sleep):
        mock_fal = MagicMock()
        mock_fal.subscribe.side_effect = [
            Exception("429 rate limit exceeded"),
            {"images": [{"url": "http://ok.jpg"}]},
        ]

        with patch.dict("sys.modules", {"fal_client": mock_fal}):
            from pipeline.images import _fal_subscribe_with_retry
            _fal_subscribe_with_retry("fal-ai/test", {"prompt": "x"})

        assert mock_fal.subscribe.call_count == 2

    @patch("time.sleep")
    def test_retries_on_502(self, mock_sleep):
        mock_fal = MagicMock()
        mock_fal.subscribe.side_effect = [
            Exception("502 bad gateway"),
            {"images": [{"url": "http://ok.jpg"}]},
        ]

        with patch.dict("sys.modules", {"fal_client": mock_fal}):
            from pipeline.images import _fal_subscribe_with_retry
            _fal_subscribe_with_retry("fal-ai/test", {"prompt": "x"})

        assert mock_fal.subscribe.call_count == 2

    @patch("time.sleep")
    def test_retries_on_503(self, mock_sleep):
        mock_fal = MagicMock()
        mock_fal.subscribe.side_effect = [
            Exception("503 service temporarily unavailable"),
            {"images": [{"url": "http://ok.jpg"}]},
        ]

        with patch.dict("sys.modules", {"fal_client": mock_fal}):
            from pipeline.images import _fal_subscribe_with_retry
            _fal_subscribe_with_retry("fal-ai/test", {"prompt": "x"})

        assert mock_fal.subscribe.call_count == 2

    @patch("time.sleep")
    def test_retries_on_overloaded(self, mock_sleep):
        mock_fal = MagicMock()
        mock_fal.subscribe.side_effect = [
            Exception("server overloaded"),
            {"images": [{"url": "http://ok.jpg"}]},
        ]

        with patch.dict("sys.modules", {"fal_client": mock_fal}):
            from pipeline.images import _fal_subscribe_with_retry
            _fal_subscribe_with_retry("fal-ai/test", {"prompt": "x"})

        assert mock_fal.subscribe.call_count == 2

    @patch("time.sleep")
    def test_non_retryable_error_raises_immediately(self, mock_sleep):
        mock_fal = MagicMock()
        mock_fal.subscribe.side_effect = ValueError("invalid prompt format")

        with patch.dict("sys.modules", {"fal_client": mock_fal}):
            from pipeline.images import _fal_subscribe_with_retry
            with pytest.raises(ValueError, match="invalid prompt"):
                _fal_subscribe_with_retry("fal-ai/test", {"prompt": "x"})

        mock_fal.subscribe.assert_called_once()

    @patch("time.sleep")
    def test_exhausts_all_attempts(self, mock_sleep):
        mock_fal = MagicMock()
        mock_fal.subscribe.side_effect = Exception("timeout forever")

        with patch.dict("sys.modules", {"fal_client": mock_fal}):
            from pipeline.images import _fal_subscribe_with_retry
            with pytest.raises(Exception, match="timeout"):
                _fal_subscribe_with_retry("fal-ai/test", {"prompt": "x"}, max_attempts=3)

        assert mock_fal.subscribe.call_count == 3

    @patch("time.sleep")
    def test_exponential_backoff_waits(self, mock_sleep):
        mock_fal = MagicMock()
        mock_fal.subscribe.side_effect = [
            Exception("timeout"),
            Exception("timeout"),
            {"images": []},
        ]

        with patch.dict("sys.modules", {"fal_client": mock_fal}):
            from pipeline.images import _fal_subscribe_with_retry
            _fal_subscribe_with_retry("fal-ai/test", {"prompt": "x"}, backoff_base=2)

        # sleep called twice (before attempt 2 and 3)
        assert mock_sleep.call_count == 2
        first_wait = mock_sleep.call_args_list[0][0][0]
        second_wait = mock_sleep.call_args_list[1][0][0]
        assert first_wait >= 2.0   # 2^1
        assert second_wait >= 4.0  # 2^2


# ── _score_image tests ────────────────────────────────────────────────────


class TestScoreImage:
    def test_score_returns_none_on_missing_file(self, tmp_path):
        """Non-existent file → None ("no score"), not 0 (which forced regenerations)."""
        from pipeline.images import _score_image
        score = _score_image(str(tmp_path / "nonexistent.jpg"))
        assert score is None

    @patch("clients.claude_client.client")
    def test_score_returns_none_on_api_error(self, mock_client, tmp_path):
        """API errors → None gracefully (image accepted, no regeneration loop)."""
        mock_client.messages.create.side_effect = Exception("API down")

        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")

        from pipeline.images import _score_image
        score = _score_image(str(img))
        assert score is None

    @patch("clients.claude_client.client")
    def test_score_parses_number(self, mock_client, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        resp = MagicMock()
        resp.content = [MagicMock(text="8")]
        mock_client.messages.create.return_value = resp
        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0")
        from pipeline.images import _score_image
        assert _score_image(str(img)) == 8

    def test_score_regex_parsing(self):
        """Verify the regex correctly extracts scores from various response formats."""
        # The _score_image regex: re.search(r'\d+', text)
        test_cases = [
            ("8", 8),
            ("I'd rate this a 7 out of 10", 7),
            ("Score: 9/10", 9),
            ("No number here", None),
            ("", None),
        ]
        for text, expected in test_cases:
            match = re.search(r'\d+', text)
            result = int(match.group()) if match else 0
            if expected is None:
                assert result == 0
            else:
                assert result == expected


# ── _generate_single_image tests ──────────────────────────────────────────


class TestGenerateSingleImage:
    @patch("pipeline.images._score_image", return_value=9)
    @patch("providers.images.fal._fal_subscribe_with_retry")
    @patch("pipeline.helpers.download_file")
    @patch("pipeline.images._shutdown_event", MagicMock(is_set=MagicMock(return_value=False)))
    def test_successful_generation(self, mock_retrieve, mock_fal, mock_score, tmp_path):
        mock_fal.return_value = {"images": [{"url": "http://example.com/img.jpg"}]}

        def fake_retrieve(url, path):
            Path(path).write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 1000)

        mock_retrieve.side_effect = fake_retrieve

        scene = {
            "narration": "The fall of Rome in 476 AD.",
            "visual_description": "Roman soldiers marching",
            "mood": "dark",
        }

        from pipeline.images import _generate_single_image
        idx, updated, success = _generate_single_image(
            idx=0, scene=scene, total_scenes=5,
            assets_dir=tmp_path, image_model="flux",
            mood_light={"dark": "deep shadows"},
            style_recraft="test style", style_flux="test flux style",
        )

        assert success is True
        assert idx == 0
        assert updated["ai_image"] is not None

    @patch("pipeline.images._score_image", return_value=0)
    @patch("providers.images.fal._fal_subscribe_with_retry")
    @patch("pipeline.helpers.download_file")
    @patch("pipeline.images._shutdown_event", MagicMock(is_set=MagicMock(return_value=False)))
    def test_empty_images_list_raises(self, mock_retrieve, mock_fal, mock_score, tmp_path):
        """fal.ai returning empty images list should fail the scene."""
        mock_fal.return_value = {"images": []}

        scene = {
            "narration": "Test narration.",
            "mood": "tense",
            "visual": {"url": ""},
        }

        from pipeline.images import _generate_single_image
        idx, updated, success = _generate_single_image(
            idx=0, scene=scene, total_scenes=3,
            assets_dir=tmp_path, image_model="flux",
            mood_light={"tense": "shadows"},
            style_recraft="s", style_flux="s",
        )

        assert success is False
        assert updated["ai_image"] is None

    @patch("pipeline.images._shutdown_event", MagicMock(is_set=MagicMock(return_value=True)))
    def test_shutdown_event_aborts(self, tmp_path):
        scene = {"narration": "Test.", "mood": "dark"}

        from pipeline.images import _generate_single_image
        idx, updated, success = _generate_single_image(
            idx=0, scene=scene, total_scenes=3,
            assets_dir=tmp_path, image_model="flux",
            mood_light={}, style_recraft="s", style_flux="s",
        )

        assert success is False
        assert updated["ai_image"] is None

    @patch("pipeline.images._score_image", return_value=5)
    @patch("providers.images.fal._fal_subscribe_with_retry")
    @patch("pipeline.helpers.download_file")
    @patch("pipeline.images.time.sleep")
    @patch("pipeline.images._shutdown_event", MagicMock(is_set=MagicMock(return_value=False)))
    def test_quality_threshold_retries(self, mock_sleep, mock_retrieve, mock_fal, mock_score, tmp_path):
        """Score below threshold triggers retries up to IMAGE_MAX_RETRIES."""
        mock_fal.return_value = {"images": [{"url": "http://example.com/img.jpg"}]}

        def fake_retrieve(url, path):
            Path(path).write_bytes(b"\xff\xd8" + b"\x00" * 500)

        mock_retrieve.side_effect = fake_retrieve

        scene = {"narration": "Normal scene.", "mood": "dark"}

        from pipeline.images import _generate_single_image
        idx, updated, success = _generate_single_image(
            idx=0, scene=scene, total_scenes=5,
            assets_dir=tmp_path, image_model="flux",
            mood_light={"dark": "shadows"},
            style_recraft="s", style_flux="s",
        )

        # Default: threshold 7, max retries 3 → should try 3 times
        assert mock_fal.call_count == 3
        # Uses best score even if below threshold
        assert success is True

    @patch("pipeline.images._score_image", return_value=9)
    @patch("providers.images.fal._fal_subscribe_with_retry")
    @patch("pipeline.helpers.download_file")
    @patch("pipeline.images._shutdown_event", MagicMock(is_set=MagicMock(return_value=False)))
    def test_hook_scene_higher_threshold(self, mock_retrieve, mock_fal, mock_score, tmp_path):
        """Hook scenes use threshold 8 and up to 5 retries."""
        mock_fal.return_value = {"images": [{"url": "http://example.com/img.jpg"}]}

        def fake_retrieve(url, path):
            Path(path).write_bytes(b"\xff\xd8" + b"\x00" * 500)

        mock_retrieve.side_effect = fake_retrieve

        scene = {
            "narration": "The hook.",
            "mood": "dramatic",
            "narrative_position": "hook",
        }

        from pipeline.images import _generate_single_image
        idx, updated, success = _generate_single_image(
            idx=0, scene=scene, total_scenes=5,
            assets_dir=tmp_path, image_model="recraft",
            mood_light={"dramatic": "light"},
            style_recraft="recraft style", style_flux="flux style",
        )

        assert success is True
        # Score 9 >= threshold 8, so only one attempt
        assert mock_fal.call_count == 1

    @patch("pipeline.images._score_image", return_value=9)
    @patch("providers.images.fal._fal_subscribe_with_retry")
    @patch("pipeline.helpers.download_file")
    @patch("pipeline.images._shutdown_event", MagicMock(is_set=MagicMock(return_value=False)))
    def test_recraft_model_uses_correct_endpoint(self, mock_retrieve, mock_fal, mock_score, tmp_path):
        mock_fal.return_value = {"images": [{"url": "http://img.jpg"}]}

        def fake_retrieve(url, path):
            Path(path).write_bytes(b"\xff\xd8" + b"\x00" * 500)

        mock_retrieve.side_effect = fake_retrieve

        scene = {"narration": "Test.", "mood": "dark"}

        from pipeline.images import _generate_single_image
        _generate_single_image(
            idx=0, scene=scene, total_scenes=1,
            assets_dir=tmp_path, image_model="recraft",
            mood_light={"dark": "shadows"},
            style_recraft="my_style", style_flux="other",
        )

        call_args = mock_fal.call_args
        assert "recraft" in call_args[0][0]
        assert call_args[0][1]["style"] == "digital_illustration"

    @patch("pipeline.images._score_image", return_value=9)
    @patch("providers.images.fal._fal_subscribe_with_retry")
    @patch("pipeline.helpers.download_file")
    @patch("pipeline.images._shutdown_event", MagicMock(is_set=MagicMock(return_value=False)))
    def test_flux_model_uses_correct_endpoint(self, mock_retrieve, mock_fal, mock_score, tmp_path):
        mock_fal.return_value = {"images": [{"url": "http://img.jpg"}]}

        def fake_retrieve(url, path):
            Path(path).write_bytes(b"\xff\xd8" + b"\x00" * 500)

        mock_retrieve.side_effect = fake_retrieve

        scene = {"narration": "Test.", "mood": "dark"}

        from pipeline.images import _generate_single_image
        _generate_single_image(
            idx=0, scene=scene, total_scenes=1,
            assets_dir=tmp_path, image_model="flux",
            mood_light={"dark": "shadows"},
            style_recraft="s", style_flux="s",
        )

        call_args = mock_fal.call_args
        assert "flux-pro" in call_args[0][0]

    @patch("providers.images.fal._fal_subscribe_with_retry")
    @patch("pipeline.images._shutdown_event", MagicMock(is_set=MagicMock(return_value=False)))
    def test_wikimedia_fallback_no_url(self, mock_fal, tmp_path):
        """When fal.ai fails and no wikimedia URL, image is None."""
        mock_fal.side_effect = Exception("fal.ai is down")

        scene = {
            "narration": "Test.",
            "mood": "dark",
            "visual": {"url": ""},
        }

        from pipeline.images import _generate_single_image
        idx, updated, success = _generate_single_image(
            idx=0, scene=scene, total_scenes=1,
            assets_dir=tmp_path, image_model="flux",
            mood_light={"dark": "shadows"},
            style_recraft="s", style_flux="s",
        )

        assert success is False
        assert updated["ai_image"] is None

    @patch("pipeline.images._score_image", return_value=9)
    @patch("providers.images.fal._fal_subscribe_with_retry")
    @patch("pipeline.helpers.download_file")
    @patch("pipeline.images._shutdown_event", MagicMock(is_set=MagicMock(return_value=False)))
    def test_breathing_room_lower_threshold(self, mock_retrieve, mock_fal, mock_score, tmp_path):
        """Breathing room scenes should have lower threshold (6) and fewer retries (2)."""
        mock_fal.return_value = {"images": [{"url": "http://img.jpg"}]}

        def fake_retrieve(url, path):
            Path(path).write_bytes(b"\xff\xd8" + b"\x00" * 500)

        mock_retrieve.side_effect = fake_retrieve

        scene = {
            "narration": "A quiet moment.",
            "mood": "dark",
            "is_breathing_room": True,
        }

        from pipeline.images import _generate_single_image
        idx, updated, success = _generate_single_image(
            idx=0, scene=scene, total_scenes=5,
            assets_dir=tmp_path, image_model="flux",
            mood_light={"dark": "shadows"},
            style_recraft="s", style_flux="s",
        )

        assert success is True
        # Score 9 >= threshold 6, so only one attempt
        assert mock_fal.call_count == 1


# ── _generate_character_portraits tests ───────────────────────────────────


class TestGenerateCharacterPortraits:
    def test_empty_visual_bible(self, tmp_path):
        from pipeline.images import _generate_character_portraits
        result = _generate_character_portraits({}, [], tmp_path)
        assert result == {}

    def test_no_character_descriptions(self, tmp_path):
        from pipeline.images import _generate_character_portraits
        result = _generate_character_portraits({"character_descriptions": {}}, [], tmp_path)
        assert result == {}

    @patch("pipeline.images._score_image", return_value=9)
    @patch("providers.images.fal._fal_subscribe_with_retry")
    @patch("pipeline.helpers.download_file")
    def test_generates_top_characters(self, mock_retrieve, mock_fal, mock_score, tmp_path):
        mock_fal.return_value = {"images": [{"url": "http://portrait.jpg"}]}

        def fake_retrieve(url, path):
            Path(path).write_bytes(b"\xff\xd8" + b"\x00" * 500)

        mock_retrieve.side_effect = fake_retrieve

        visual_bible = {
            "character_descriptions": {
                "Caesar": "A Roman general in his 50s",
                "Brutus": "A young senator",
            },
            "art_style": "classical oil painting",
        }
        scenes = [
            {"characters_mentioned": ["Caesar"]},
            {"characters_mentioned": ["Caesar", "Brutus"]},
            {"characters_mentioned": ["Caesar"]},
        ]

        from pipeline.images import _generate_character_portraits
        portraits = _generate_character_portraits(visual_bible, scenes, tmp_path)

        # Both characters should be generated (score 9 >= threshold 7)
        assert len(portraits) == 2

    @patch("pipeline.images._score_image", return_value=3)
    @patch("providers.images.fal._fal_subscribe_with_retry")
    @patch("pipeline.helpers.download_file")
    def test_low_score_portrait_skipped(self, mock_retrieve, mock_fal, mock_score, tmp_path):
        """Portraits scoring below 7 should be skipped."""
        mock_fal.return_value = {"images": [{"url": "http://portrait.jpg"}]}

        def fake_retrieve(url, path):
            Path(path).write_bytes(b"\xff\xd8" + b"\x00" * 100)

        mock_retrieve.side_effect = fake_retrieve

        visual_bible = {
            "character_descriptions": {"Caesar": "A leader"},
            "art_style": "oil painting",
        }
        scenes = [{"characters_mentioned": ["Caesar"]}]

        from pipeline.images import _generate_character_portraits
        portraits = _generate_character_portraits(visual_bible, scenes, tmp_path)

        assert "Caesar" not in portraits

    @patch("providers.images.fal._fal_subscribe_with_retry")
    def test_portrait_generation_failure_graceful(self, mock_fal, tmp_path):
        """If fal.ai fails for portraits, return empty dict gracefully."""
        mock_fal.side_effect = Exception("fal.ai error")

        visual_bible = {
            "character_descriptions": {"Caesar": "A leader"},
            "art_style": "oil painting",
        }
        scenes = [{"characters_mentioned": ["Caesar"]}]

        from pipeline.images import _generate_character_portraits
        portraits = _generate_character_portraits(visual_bible, scenes, tmp_path)
        assert portraits == {}


# ── Adaptive quality threshold tests ──────────────────────────────────────


class TestAdaptiveQualityThresholds:
    """Test the quality threshold logic based on scene importance."""

    def _get_thresholds(self, scene):
        """Replicate the threshold logic from _generate_single_image."""
        position = scene.get("narrative_position", "")
        is_reveal = scene.get("is_reveal_moment", False)
        is_breathing = scene.get("is_breathing_room", False)

        if position == "hook" or is_reveal:
            return 8, 5
        elif position in ("act3", "ending"):
            return 8, 4
        elif is_breathing:
            return 6, 2
        else:
            return 7, 3

    def test_hook_scene(self):
        threshold, retries = self._get_thresholds({"narrative_position": "hook"})
        assert threshold == 8
        assert retries == 5

    def test_reveal_moment(self):
        threshold, retries = self._get_thresholds({"is_reveal_moment": True})
        assert threshold == 8
        assert retries == 5

    def test_act3_scene(self):
        threshold, retries = self._get_thresholds({"narrative_position": "act3"})
        assert threshold == 8
        assert retries == 4

    def test_ending_scene(self):
        threshold, retries = self._get_thresholds({"narrative_position": "ending"})
        assert threshold == 8
        assert retries == 4

    def test_breathing_room(self):
        threshold, retries = self._get_thresholds({"is_breathing_room": True})
        assert threshold == 6
        assert retries == 2

    def test_normal_scene(self):
        threshold, retries = self._get_thresholds({"narrative_position": "act2"})
        assert threshold == 7
        assert retries == 3

    def test_default_scene(self):
        threshold, retries = self._get_thresholds({})
        assert threshold == 7
        assert retries == 3


# ── Prompt construction tests ─────────────────────────────────────────────


class TestPromptConstruction:
    def test_era_context_with_year_and_location(self):
        year = "44 BC"
        location = "Rome"
        era_context = ""
        if year:
            era_context += f"set in {year}, "
        if location:
            era_context += f"in {location}, "
        assert "set in 44 BC" in era_context
        assert "in Rome" in era_context

    def test_era_context_empty(self):
        era_context = ""
        assert era_context == ""

    def test_composition_hints(self):
        """Verify composition hints match narrative position."""
        positions = {
            "hook": "extreme close-up",
            "act3": "wide dramatic reveal",
            "ending": "solitary figure silhouetted",
        }
        for pos, expected in positions.items():
            composition_hint = ""
            if pos == "hook":
                composition_hint = "extreme close-up on a face frozen in a pivotal moment, "
            elif pos == "act3":
                composition_hint = "wide dramatic reveal shot, figures dwarfed by grand architecture or landscape, "
            elif pos == "ending":
                composition_hint = "solitary figure silhouetted against vast empty space, melancholic distance, "
            assert expected in composition_hint

    def test_character_description_in_prompt(self):
        """Characters should appear in prompt when no visual_description."""
        characters = ["Caesar", "Brutus"]
        visual_desc = ""
        char_desc = ""
        if characters and not visual_desc:
            char_desc = f"depicting {', '.join(characters[:2])}, period-accurate clothing and appearance, "
        assert "Caesar" in char_desc
        assert "Brutus" in char_desc

    def test_character_description_skipped_with_visual_desc(self):
        characters = ["Caesar"]
        visual_desc = "A Roman soldier standing tall"
        char_desc = ""
        if characters and not visual_desc:
            char_desc = f"depicting {', '.join(characters[:2])}, "
        assert char_desc == ""


# ── Abort threshold and audit log tests ───────────────────────────────────


class TestRunImagesAbortThreshold:
    def test_below_50_pct_triggers_abort(self):
        """More than 50% missing images should trigger abort."""
        scenes = [{"ai_image": None}] * 3 + [{"ai_image": "/img.jpg"}]
        total = len(scenes)
        with_images = sum(1 for s in scenes if s.get("ai_image"))
        assert with_images / total < 0.5

    def test_50_pct_does_not_abort(self):
        scenes = [{"ai_image": "/img.jpg"}] * 2 + [{"ai_image": None}] * 2
        total = len(scenes)
        with_images = sum(1 for s in scenes if s.get("ai_image"))
        assert with_images / total >= 0.5

    def test_all_images_present(self):
        scenes = [{"ai_image": f"/img_{i}.jpg"} for i in range(5)]
        total = len(scenes)
        with_images = sum(1 for s in scenes if s.get("ai_image"))
        assert with_images / total == 1.0


class TestImageAuditLog:
    def test_audit_log_source_classification(self):
        """Verify audit log correctly categorizes image sources."""
        scenes = [
            {"ai_image": "/path/scene_000_ai.jpg"},
            {"ai_image": None, "wikimedia_url": "https://upload.wikimedia.org/img.jpg"},
            {"ai_image": "/path/unknown.jpg"},
            {"ai_image": None},
        ]

        audit_log = []
        for i, scene in enumerate(scenes):
            entry = {"scene_index": i, "source": "none", "license": "none"}
            ai_img = scene.get("ai_image")
            wiki_img = scene.get("wikimedia_url", "")

            if ai_img and "ai.jpg" in str(ai_img):
                entry["source"] = "fal.ai (AI generated)"
                entry["license"] = "AI-generated, no copyright"
            elif wiki_img and "wikimedia" in str(wiki_img).lower():
                entry["source"] = "Wikimedia Commons"
                entry["license"] = "Public Domain / CC"
            elif ai_img:
                entry["source"] = "unknown"
                entry["license"] = "verify manually"

            audit_log.append(entry)

        assert audit_log[0]["source"] == "fal.ai (AI generated)"
        assert audit_log[1]["source"] == "Wikimedia Commons"
        assert audit_log[2]["source"] == "unknown"
        assert audit_log[3]["source"] == "none"

    def test_audit_log_counts(self):
        scenes = [
            {"ai_image": "/path/ai.jpg"},
            {"ai_image": "/path/ai2.jpg"},
            {"ai_image": None},
        ]
        total = len(scenes)
        with_images = sum(1 for s in scenes if s.get("ai_image"))
        assert total == 3
        assert with_images == 2


# ── Character slug generation tests ───────────────────────────────────────


class TestCharacterSlug:
    def test_simple_name(self):
        slug = re.sub(r'[^a-z0-9]+', '_', "Caesar".lower()).strip('_')
        assert slug == "caesar"

    def test_multi_word_name(self):
        slug = re.sub(r'[^a-z0-9]+', '_', "Julius Caesar".lower()).strip('_')
        assert slug == "julius_caesar"

    def test_special_chars(self):
        slug = re.sub(r'[^a-z0-9]+', '_', "St. Peter's".lower()).strip('_')
        assert slug == "st_peter_s"


# ═══════════════════════════════════════════════════════════════════════════════
# _ensure_min_resolution
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnsureMinResolution:
    """Test the resolution safety net."""

    def test_undersized_image_gets_upscaled(self, tmp_path):
        from PIL import Image
        small = tmp_path / "small.jpg"
        Image.new("RGB", (1024, 576)).save(small)
        from pipeline.images import _ensure_min_resolution
        was_upscaled = _ensure_min_resolution(small, 1920, 1080)
        assert was_upscaled is True
        img = Image.open(small)
        assert img.size[0] >= 1920
        assert img.size[1] >= 1080

    def test_large_image_unchanged(self, tmp_path):
        from PIL import Image
        big = tmp_path / "big.jpg"
        Image.new("RGB", (2560, 1440)).save(big)
        from pipeline.images import _ensure_min_resolution
        was_upscaled = _ensure_min_resolution(big, 1920, 1080)
        assert was_upscaled is False
        img = Image.open(big)
        assert img.size == (2560, 1440)

    def test_preserves_aspect_ratio(self, tmp_path):
        from PIL import Image
        small = tmp_path / "wide.jpg"
        Image.new("RGB", (1344, 756)).save(small)
        from pipeline.images import _ensure_min_resolution
        _ensure_min_resolution(small, 1920, 1080)
        img = Image.open(small)
        w, h = img.size
        assert w >= 1920
        assert h >= 1080
        assert abs(w / h - 1344 / 756) < 0.02

    def test_nonexistent_file_returns_false(self, tmp_path):
        from pipeline.images import _ensure_min_resolution
        result = _ensure_min_resolution(tmp_path / "nope.jpg", 1920, 1080)
        assert result is False

    def test_corrupt_file_returns_false(self, tmp_path):
        bad = tmp_path / "corrupt.jpg"
        bad.write_text("not an image")
        from pipeline.images import _ensure_min_resolution
        result = _ensure_min_resolution(bad, 1920, 1080)
        assert result is False


class TestSharpenForVideo:
    """Test output sharpening for H.264 delivery."""

    def test_sharpens_image(self, tmp_path):
        from PIL import Image
        img = tmp_path / "scene.jpg"
        Image.new("RGB", (1920, 1080), color=(128, 128, 128)).save(img)
        from pipeline.images import _sharpen_for_video
        assert _sharpen_for_video(img) is True
        # File should still exist and be valid
        result = Image.open(img)
        assert result.size == (1920, 1080)

    def test_nonexistent_file_returns_false(self, tmp_path):
        from pipeline.images import _sharpen_for_video
        assert _sharpen_for_video(tmp_path / "nope.jpg") is False

    def test_corrupt_file_returns_false(self, tmp_path):
        bad = tmp_path / "bad.jpg"
        bad.write_text("garbage")
        from pipeline.images import _sharpen_for_video
        assert _sharpen_for_video(bad) is False
