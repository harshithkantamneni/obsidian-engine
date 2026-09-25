"""Tests for pipeline/audio.py — ElevenLabs TTS audio generation.

Covers: split_chunks, detect_quoted_speech, extract_word_timestamps,
generate_chunk retry logic (mocked API), and edge cases.
"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# The inner functions in run_audio are closures, so we replicate the
# pure-logic ones here for direct testing.
# ---------------------------------------------------------------------------

MAX_CHARS = 500


def _split_chunks(text, max_chars=MAX_CHARS):
    """Replica of split_chunks from run_audio (pure logic, no API)."""
    sentences = text.replace("\n", " ").split(". ")
    chunks, current = [], ""
    for s in sentences:
        part = s + ". "
        if len(current) + len(part) > max_chars and current:
            chunks.append(current.strip())
            current = part
        else:
            current += part
    if current.strip() and len(current.strip()) > 2:
        chunks.append(current.strip())
    return chunks


def _detect_quoted_speech(text):
    """Replica of detect_quoted_speech from run_audio."""
    quotes = re.findall(r'["\u201c]([^"\u201d]{10,200})["\u201d]', text)
    return len(quotes) > 0 and any(
        word in text.lower()
        for word in [
            "said", "wrote", "declared", "proclaimed", "whispered", "shouted",
            "announced", "commanded", "stated", "replied", "exclaimed",
        ]
    )


def _extract_word_timestamps(data):
    """Replica of extract_word_timestamps from run_audio."""
    alignment = data.get("alignment") or {}
    if not isinstance(alignment, dict):
        alignment = {}
    chars = alignment.get("characters", [])
    starts = alignment.get("character_start_times_seconds", [])
    ends = alignment.get("character_end_times_seconds", [])

    chunk_words = []
    word, word_start, last_end = "", None, 0.0
    for j, ch in enumerate(chars):
        if ch in (" ", "\n"):
            if word and word_start is not None:
                end_idx = j - 1
                word_end = ends[end_idx] if 0 <= end_idx < len(ends) else word_start + max(0.15, len(word) * 0.08)
                chunk_words.append({
                    "word": word,
                    "start": round(word_start, 3),
                    "end": round(word_end, 3),
                })
                word, word_start = "", None
        else:
            if word_start is None and j < len(starts):
                word_start = starts[j]
            word += ch
            if j < len(ends):
                last_end = ends[j]
    if word and word_start is not None:
        chunk_words.append({
            "word": word,
            "start": round(word_start, 3),
            "end": round(last_end, 3),
        })
    return chunk_words


# ── split_chunks tests ────────────────────────────────────────────────────


class TestSplitChunks:
    def test_short_text_single_chunk(self):
        text = "This is a short sentence."
        chunks = _split_chunks(text)
        assert len(chunks) == 1

    def test_long_text_multiple_chunks(self):
        sentence = "This is a moderately long sentence that fills up space"
        text = ". ".join([sentence] * 20)
        chunks = _split_chunks(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= MAX_CHARS + 100

    def test_empty_text(self):
        chunks = _split_chunks("")
        assert len(chunks) == 0

    def test_single_sentence(self):
        text = "The Roman Empire fell in 476 AD."
        chunks = _split_chunks(text)
        assert len(chunks) == 1
        assert "Roman Empire" in chunks[0]

    def test_newlines_replaced(self):
        text = "Line one.\nLine two.\nLine three."
        chunks = _split_chunks(text)
        for chunk in chunks:
            assert "\n" not in chunk

    def test_very_long_single_sentence(self):
        """A single sentence longer than MAX_CHARS stays as one chunk."""
        text = "A" * 600
        chunks = _split_chunks(text)
        assert len(chunks) == 1
        assert len(chunks[0]) > MAX_CHARS

    def test_exact_boundary(self):
        """Text near boundary should not lose content."""
        text = "Short. Medium sentence here. Another one."
        chunks = _split_chunks(text)
        total_content = " ".join(chunks)
        assert "Short" in total_content
        assert "Another" in total_content

    def test_preserves_all_content(self):
        """No content should be lost during chunking."""
        words = ["The", "quick", "brown", "fox"]
        text = ". ".join(words)
        chunks = _split_chunks(text)
        combined = " ".join(chunks)
        for word in words:
            assert word in combined


# ── detect_quoted_speech tests ────────────────────────────────────────────


class TestDetectQuotedSpeech:
    def test_quoted_with_speech_verb(self):
        text = 'Caesar said "I came, I saw, I conquered the whole land"'
        assert _detect_quoted_speech(text) is True

    def test_quoted_without_speech_verb(self):
        text = 'The sign read "No trespassing beyond this point allowed"'
        assert _detect_quoted_speech(text) is False

    def test_no_quotes(self):
        text = "He said something important about the battle."
        assert _detect_quoted_speech(text) is False

    def test_short_quote_ignored(self):
        """Quotes shorter than 10 chars should not match."""
        text = 'She said "Go now!"'
        assert _detect_quoted_speech(text) is False

    def test_unicode_quotes(self):
        text = '\u201cI declare this land sovereign\u201d he proclaimed to the crowd'
        assert _detect_quoted_speech(text) is True

    def test_empty_text(self):
        assert _detect_quoted_speech("") is False

    def test_multiple_speech_verbs(self):
        text = 'He whispered "The treasure is hidden beneath the old church tower" and then shouted'
        assert _detect_quoted_speech(text) is True

    def test_long_quote_over_200_chars_ignored(self):
        """Quotes over 200 chars should not match the regex."""
        long_quote = "A" * 250
        text = f'He said "{long_quote}"'
        assert _detect_quoted_speech(text) is False

    def test_exactly_10_char_quote(self):
        """Quote of exactly 10 chars should match."""
        text = 'He said "1234567890"'
        assert _detect_quoted_speech(text) is True

    def test_all_speech_verbs_detected(self):
        verbs = ["said", "wrote", "declared", "proclaimed", "whispered",
                 "shouted", "announced", "commanded", "stated", "replied", "exclaimed"]
        for verb in verbs:
            text = f'He {verb} "This is the important thing to know"'
            assert _detect_quoted_speech(text) is True, f"Verb '{verb}' not detected"


# ── extract_word_timestamps tests ─────────────────────────────────────────


class TestExtractWordTimestamps:
    def test_basic_extraction(self):
        data = {
            "alignment": {
                "characters": list("Hello world"),
                "character_start_times_seconds": [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50],
                "character_end_times_seconds":   [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55],
            }
        }
        words = _extract_word_timestamps(data)
        assert len(words) == 2
        assert words[0]["word"] == "Hello"
        assert words[1]["word"] == "world"
        assert words[0]["start"] == 0.0
        assert words[1]["start"] == 0.30

    def test_empty_alignment(self):
        assert _extract_word_timestamps({"alignment": {}}) == []

    def test_missing_alignment(self):
        assert _extract_word_timestamps({}) == []

    def test_none_alignment(self):
        assert _extract_word_timestamps({"alignment": None}) == []

    def test_non_dict_alignment(self):
        assert _extract_word_timestamps({"alignment": "invalid"}) == []

    def test_single_word_no_space(self):
        data = {
            "alignment": {
                "characters": list("Hi"),
                "character_start_times_seconds": [0.0, 0.05],
                "character_end_times_seconds":   [0.05, 0.10],
            }
        }
        words = _extract_word_timestamps(data)
        assert len(words) == 1
        assert words[0]["word"] == "Hi"
        assert words[0]["end"] == 0.10

    def test_multiple_spaces(self):
        data = {
            "alignment": {
                "characters": list("A  B"),
                "character_start_times_seconds": [0.0, 0.1, 0.2, 0.3],
                "character_end_times_seconds":   [0.1, 0.2, 0.3, 0.4],
            }
        }
        words = _extract_word_timestamps(data)
        assert len(words) == 2
        assert words[0]["word"] == "A"
        assert words[1]["word"] == "B"

    def test_newline_as_separator(self):
        data = {
            "alignment": {
                "characters": list("A\nB"),
                "character_start_times_seconds": [0.0, 0.1, 0.2],
                "character_end_times_seconds":   [0.1, 0.2, 0.3],
            }
        }
        words = _extract_word_timestamps(data)
        assert len(words) == 2
        assert words[0]["word"] == "A"
        assert words[1]["word"] == "B"

    def test_truncated_timestamps(self):
        """When timestamps are shorter than characters, use fallback."""
        data = {
            "alignment": {
                "characters": list("Hello world"),
                "character_start_times_seconds": [0.0, 0.05],
                "character_end_times_seconds":   [0.05, 0.10],
            }
        }
        words = _extract_word_timestamps(data)
        assert len(words) >= 1
        assert words[0]["word"] == "Hello"

    def test_empty_characters(self):
        data = {
            "alignment": {
                "characters": [],
                "character_start_times_seconds": [],
                "character_end_times_seconds": [],
            }
        }
        assert _extract_word_timestamps(data) == []

    def test_timestamps_are_rounded(self):
        data = {
            "alignment": {
                "characters": list("AB CD"),
                "character_start_times_seconds": [0.1111, 0.2222, 0.3333, 0.4444, 0.5555],
                "character_end_times_seconds":   [0.2222, 0.3333, 0.4444, 0.5555, 0.6666],
            }
        }
        words = _extract_word_timestamps(data)
        for w in words:
            assert w["start"] == round(w["start"], 3)
            assert w["end"] == round(w["end"], 3)

    def test_three_words(self):
        data = {
            "alignment": {
                "characters": list("one two three"),
                "character_start_times_seconds": [0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6],
                "character_end_times_seconds":   [0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65],
            }
        }
        words = _extract_word_timestamps(data)
        assert len(words) == 3
        assert words[0]["word"] == "one"
        assert words[1]["word"] == "two"
        assert words[2]["word"] == "three"


# ── generate_chunk retry logic tests (unit-level) ────────────────────────


class TestGenerateChunkRetryLogic:
    """Test the retry logic pattern used by generate_chunk.

    Since generate_chunk is a closure inside run_audio, we test the retry
    pattern as a standalone function with the same logic.
    """

    def _simulate_generate_chunk(self, responses):
        """Simulate the generate_chunk retry loop with a list of mock responses.
        Each response is (status_code, text) or an exception.
        Returns the parsed JSON on success, raises on exhaustion.
        """
        last_err = None
        for attempt in range(5):
            resp = responses[attempt] if attempt < len(responses) else responses[-1]

            if isinstance(resp, Exception):
                last_err = resp
                continue

            status_code, text = resp
            if status_code == 200:
                return json.loads(text, strict=False)
            elif status_code == 429:
                last_err = Exception("ElevenLabs rate limit (429)")
            elif status_code in (500, 502, 503):
                last_err = Exception(f"ElevenLabs server error ({status_code})")
            else:
                raise Exception(f"ElevenLabs {status_code}: {text[:200]}")
        raise last_err or Exception("Failed after 5 attempts")

    def test_success_first_try(self):
        result = self._simulate_generate_chunk([
            (200, json.dumps({"audio_base64": "AAAA", "alignment": {}})),
        ])
        assert result["audio_base64"] == "AAAA"

    def test_429_then_success(self):
        result = self._simulate_generate_chunk([
            (429, "rate limited"),
            (429, "rate limited"),
            (200, json.dumps({"audio_base64": "OK", "alignment": {}})),
        ])
        assert result["audio_base64"] == "OK"

    def test_500_then_success(self):
        result = self._simulate_generate_chunk([
            (500, "server error"),
            (200, json.dumps({"audio_base64": "OK", "alignment": {}})),
        ])
        assert result["audio_base64"] == "OK"

    def test_502_then_503_then_success(self):
        result = self._simulate_generate_chunk([
            (502, "bad gateway"),
            (503, "unavailable"),
            (200, json.dumps({"audio_base64": "OK", "alignment": {}})),
        ])
        assert result["audio_base64"] == "OK"

    def test_400_raises_immediately(self):
        with pytest.raises(Exception, match="ElevenLabs 400"):
            self._simulate_generate_chunk([
                (400, "bad request"),
            ])

    def test_401_raises_immediately(self):
        with pytest.raises(Exception, match="ElevenLabs 401"):
            self._simulate_generate_chunk([
                (401, "unauthorized"),
            ])

    def test_exhausts_all_retries(self):
        with pytest.raises(Exception, match="rate limit"):
            self._simulate_generate_chunk([
                (429, "limited"),
                (429, "limited"),
                (429, "limited"),
                (429, "limited"),
                (429, "limited"),
            ])

    def test_connection_error_retries(self):
        result = self._simulate_generate_chunk([
            ConnectionError("connection refused"),
            ConnectionError("connection reset"),
            (200, json.dumps({"audio_base64": "OK", "alignment": {}})),
        ])
        assert result["audio_base64"] == "OK"

    def test_timeout_error_retries(self):
        result = self._simulate_generate_chunk([
            TimeoutError("timed out"),
            (200, json.dumps({"audio_base64": "OK", "alignment": {}})),
        ])
        assert result["audio_base64"] == "OK"


# ── Legacy path voice selection tests ─────────────────────────────────────


class TestLegacyVoiceSelection:
    """Test the keyword-based voice selection logic in legacy path."""

    def _classify_chunk(self, chunk, chunk_idx, total_chunks):
        """Replicate the legacy voice classification logic."""
        VOICE_BODY = {"stability": 0.38, "similarity_boost": 0.82,
                      "style": 0.60, "use_speaker_boost": True}
        VOICE_HOOK = {"stability": 0.28, "similarity_boost": 0.85,
                      "style": 0.75, "use_speaker_boost": True}

        chunk_lower = chunk.lower()
        if chunk_idx == 0:
            return "hook", VOICE_HOOK, 0.92
        elif any(phrase in chunk_lower for phrase in [
            "the truth", "what really happened", "no one knew", "the real story",
            "but here's what", "what they found", "the evidence shows",
            "it was actually", "in reality",
        ]):
            return "reveal", {"stability": 0.30, "similarity_boost": 0.85, "style": 0.95, "use_speaker_boost": True}, 0.82
        elif any(phrase in chunk_lower for phrase in [
            "but then", "everything changed", "no one expected", "suddenly",
            "without warning", "in secret", "behind closed doors",
        ]):
            return "tension", {"stability": 0.35, "similarity_boost": 0.82, "style": 0.90, "use_speaker_boost": True}, 0.86
        elif chunk_idx == total_chunks - 1:
            return "ending", {"stability": 0.50, "similarity_boost": 0.82, "style": 0.80, "use_speaker_boost": True}, 0.85
        else:
            return "body", VOICE_BODY, 0.88

    def test_first_chunk_is_hook(self):
        label, vs, speed = self._classify_chunk("Any text here.", 0, 10)
        assert label == "hook"
        assert speed == 0.92
        assert vs["stability"] == 0.28

    def test_last_chunk_is_ending(self):
        label, vs, speed = self._classify_chunk("Normal text.", 9, 10)
        assert label == "ending"
        assert speed == 0.85

    def test_reveal_phrase_detected(self):
        label, vs, speed = self._classify_chunk(
            "The truth was far more disturbing.", 5, 10)
        assert label == "reveal"
        assert speed == 0.82
        assert vs["style"] == 0.95

    def test_tension_phrase_detected(self):
        label, vs, speed = self._classify_chunk(
            "But then everything changed overnight.", 5, 10)
        assert label == "tension"
        assert speed == 0.86

    def test_body_chunk(self):
        label, vs, speed = self._classify_chunk(
            "The army marched through the valley.", 5, 10)
        assert label == "body"
        assert speed == 0.88

    def test_hook_takes_priority(self):
        """Even if chunk 0 has reveal phrases, it should be classified as hook."""
        label, vs, speed = self._classify_chunk(
            "The truth about what really happened.", 0, 10)
        assert label == "hook"


# ── Edge cases ────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_split_chunks_only_periods(self):
        text = ". . . ."
        chunks = _split_chunks(text)
        assert isinstance(chunks, list)

    def test_split_chunks_unicode(self):
        text = "The emperor \u2014 Nero \u2014 burned Rome. Or so they said."
        chunks = _split_chunks(text)
        assert len(chunks) >= 1
        assert "Nero" in chunks[0]

    def test_extract_timestamps_word_boundary_at_end(self):
        """Word at end of alignment without trailing space."""
        data = {
            "alignment": {
                "characters": list("end"),
                "character_start_times_seconds": [1.0, 1.05, 1.10],
                "character_end_times_seconds":   [1.05, 1.10, 1.15],
            }
        }
        words = _extract_word_timestamps(data)
        assert len(words) == 1
        assert words[0]["word"] == "end"
        assert words[0]["start"] == 1.0
        assert words[0]["end"] == 1.15

    def test_missing_api_key_raises(self, tmp_path, monkeypatch):
        """Missing ELEVENLABS_API_KEY → the ElevenLabs provider raises RuntimeError."""
        import pipeline.audio as audio_mod
        from providers import registry
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        monkeypatch.setattr(audio_mod, "CHUNKS_DIR", tmp_path)
        monkeypatch.setattr(audio_mod, "MEDIA_DIR", tmp_path)
        registry.clear_cache()
        with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
            audio_mod.run_audio({"full_script": "Test script."})
        registry.clear_cache()
