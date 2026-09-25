"""Reflection scene injection must keep narration audio, captions and scene cuts in sync.

pipeline/convert.py injects a synthetic 3s reflection scene at the act3→ending
boundary and shifts later scenes/words by +3s. The Remotion narration copy must
get exactly the same silence spliced in at exactly the same timestamp — and if
the splice fails, nothing is shifted.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline import convert
from pipeline.convert import (
    REFLECTION_DURATION,
    _inject_reflection_scene,
    _probe_audio,
    splice_silence,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _make_sine_mp3(path: Path, seconds: float = 10.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}",
         "-ar", "44100", "-b:a", "192k", str(path)],
        check=True, capture_output=True,
    )
    return path


def _max_volume_db(path: Path, start: float, dur: float) -> float:
    out = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", f"{start}", "-t", f"{dur}", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", out.stderr)
    assert m, out.stderr[-500:]
    return float("-inf") if m.group(1) == "-inf" else float(m.group(1))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ═══════════════════════════════════════════════════════════════════════════════
# splice_silence (real ffmpeg)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSpliceSilence:

    def test_splice_inserts_silence_at_timestamp(self, tmp_path):
        src = _make_sine_mp3(tmp_path / "narration.mp3", 10.0)
        dst = tmp_path / "public" / "narration.mp3"

        assert splice_silence(src, dst, 4.0, 3.0) is True

        assert _probe_audio(dst)["duration"] == pytest.approx(13.0, abs=0.1)
        # Inserted gap 4.0–7.0 is silent (trim a hair inside the edges)
        assert _max_volume_db(dst, 4.05, 2.9) < -60.0
        # Tone is intact before and after the gap
        assert _max_volume_db(dst, 3.0, 0.9) > -30.0
        assert _max_volume_db(dst, 7.1, 2.0) > -30.0

    def test_output_is_44100_hz(self, tmp_path):
        src = _make_sine_mp3(tmp_path / "narration.mp3", 10.0)
        dst = tmp_path / "out.mp3"
        assert splice_silence(src, dst, 4.0, 3.0)
        assert _probe_audio(dst)["sample_rate"] == 44100

    def test_idempotent_and_source_untouched(self, tmp_path):
        src = _make_sine_mp3(tmp_path / "narration.mp3", 10.0)
        dst = tmp_path / "public" / "narration.mp3"
        before = _sha(src)

        assert splice_silence(src, dst, 4.0, 3.0)
        assert splice_silence(src, dst, 4.0, 3.0)  # re-run: rebuilt from source, not stacked

        assert _sha(src) == before
        assert _probe_audio(dst)["duration"] == pytest.approx(13.0, abs=0.1)

    def test_refuses_to_overwrite_source(self, tmp_path):
        src = _make_sine_mp3(tmp_path / "narration.mp3", 10.0)
        before = _sha(src)
        assert splice_silence(src, src, 4.0, 3.0) is False
        assert _sha(src) == before

    def test_missing_source_fails_cleanly(self, tmp_path):
        dst = tmp_path / "out.mp3"
        assert splice_silence(tmp_path / "nope.mp3", dst, 4.0, 3.0) is False
        assert not dst.exists()

    def test_splice_point_past_end_fails(self, tmp_path):
        src = _make_sine_mp3(tmp_path / "narration.mp3", 5.0)
        dst = tmp_path / "out.mp3"
        assert splice_silence(src, dst, 9.0, 3.0) is False
        assert not dst.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# _inject_reflection_scene
# ═══════════════════════════════════════════════════════════════════════════════

def _scenes():
    return [
        {"narrative_position": "act2", "start_time": 0.0, "end_time": 4.0, "ai_image": "a.jpg"},
        {"narrative_position": "act3", "start_time": 4.0, "end_time": 6.0, "ai_image": "b.jpg"},
        {"narrative_position": "ending", "start_time": 6.4, "end_time": 10.0, "ai_image": "c.jpg"},
    ]


def _words():
    return [
        {"word": "one", "start": 1.0, "end": 1.5},
        {"word": "two", "start": 5.5, "end": 6.0},   # last act3 word ends at boundary
        {"word": "three", "start": 6.4, "end": 7.0},
        {"word": "four", "start": 9.0, "end": 9.5},
    ]


class TestInjectReflection:

    def test_splice_failure_means_no_injection(self, tmp_path):
        scenes, words = _scenes(), _words()
        scenes_before = json.loads(json.dumps(scenes))
        words_before = json.loads(json.dumps(words))
        failing = MagicMock(return_value=False)

        out_scenes, out_words, total, spliced = _inject_reflection_scene(
            scenes, words, 10.0, tmp_path / "src.mp3", tmp_path / "dst.mp3", splice_fn=failing)

        failing.assert_called_once()
        assert spliced is False
        assert total == 10.0
        assert out_scenes == scenes_before and len(out_scenes) == 3
        assert not any(s.get("is_synthetic") for s in out_scenes)
        assert out_words == words_before
        # Inputs were not mutated either
        assert scenes == scenes_before and words == words_before

    def test_success_shifts_from_same_timestamp_as_splice(self, tmp_path):
        calls = []

        def ok(src, dst, at, dur):
            calls.append((at, dur))
            return True

        out_scenes, out_words, total, spliced = _inject_reflection_scene(
            _scenes(), _words(), 10.0, tmp_path / "s.mp3", tmp_path / "d.mp3", splice_fn=ok)

        assert spliced is True
        assert calls == [(6.0, REFLECTION_DURATION)]  # act3 end_time
        assert total == pytest.approx(13.0)
        refl = out_scenes[2]
        assert refl["is_synthetic"] and refl["start_time"] == 6.0 and refl["end_time"] == 9.0
        assert out_scenes[3]["start_time"] == pytest.approx(9.4)
        assert out_scenes[3]["end_time"] == pytest.approx(13.0)  # last scene ends at new total
        assert [w["start"] for w in out_words] == [1.0, 5.5, 9.4, 12.0]

    def test_no_boundary_no_splice(self, tmp_path):
        splice = MagicMock(return_value=True)
        scenes = [{"narrative_position": "act1", "start_time": 0, "end_time": 5}]
        out = _inject_reflection_scene(scenes, [], 5.0, "a", "b", splice_fn=splice)
        assert out == (scenes, [], 5.0, False)
        splice.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# run_convert end-to-end (real ffmpeg, external services stubbed)
# ═══════════════════════════════════════════════════════════════════════════════

def _run_convert(tmp_path, splice_ok=True):
    media, src_dir, public = tmp_path / "media", tmp_path / "src", tmp_path / "public"
    for d in (media, src_dir, public):
        d.mkdir()
    _make_sine_mp3(media / "narration.mp3", 10.0)
    words = [
        {"word": "a", "start": 0.2, "end": 3.0},
        {"word": "b", "start": 3.2, "end": 6.0},
        {"word": "c", "start": 6.4, "end": 8.5},
    ]
    (media / "timestamps.json").write_text(json.dumps(
        {"words": words, "scene_word_ranges": [[0, 0], [1, 1], [2, 2]]}))
    manifest = {"scenes": [
        {"narration": "a", "narrative_position": "act2", "mood": "dark"},
        {"narration": "b", "narrative_position": "act3", "mood": "dark"},
        {"narration": "c", "narrative_position": "ending", "mood": "dark"},
    ]}
    mm = MagicMock()
    mm.get_smart_music_for_video.return_value = None
    mm.get_music_for_video.return_value = None
    patches = [
        patch.object(convert, "MEDIA_DIR", media),
        patch.object(convert, "REMOTION_SRC", src_dir),
        patch.object(convert, "REMOTION_PUBLIC", public),
        patch.dict("sys.modules", {"media.music_manager": mm}),
        patch("core.param_overrides.get_override", side_effect=lambda k, d: d),
    ]
    if not splice_ok:
        patches.append(patch.object(convert, "splice_silence", return_value=False))
    for p in patches:
        p.start()
    try:
        vd = convert.run_convert(manifest, {"total_duration_seconds": 10.0})
    finally:
        for p in reversed(patches):
            p.stop()
    written = json.loads((src_dir / "video-data.json").read_text())
    return vd, written, media / "narration.mp3", public / "narration.mp3"


class TestRunConvertSync:

    def test_duration_and_audio_shifted_exactly_once(self, tmp_path):
        vd, written, src, dst = _run_convert(tmp_path)
        assert written["total_duration_seconds"] == pytest.approx(13.0)
        assert vd["total_duration_seconds"] == pytest.approx(13.0)
        assert any(s.get("is_synthetic") for s in written["scenes"])
        assert written["scenes"][-1]["end_time"] == pytest.approx(13.0)
        assert [w["start"] for w in written["word_timestamps"]] == [0.2, 3.2, 9.4]
        # Remotion copy is 3s longer than the (untouched) source narration
        assert _probe_audio(dst)["duration"] - _probe_audio(src)["duration"] == pytest.approx(3.0, abs=0.1)
        assert _probe_audio(src)["duration"] == pytest.approx(10.0, abs=0.1)
        # scene_manifest computed after the shift
        assert written["scene_manifest"][-1]["end_time"] == pytest.approx(13.0)

    def test_splice_failure_ships_unshifted(self, tmp_path):
        vd, written, src, dst = _run_convert(tmp_path, splice_ok=False)
        assert written["total_duration_seconds"] == pytest.approx(10.0)
        assert not any(s.get("is_synthetic") for s in written["scenes"])
        assert [w["start"] for w in written["word_timestamps"]] == [0.2, 3.2, 6.4]
        assert _sha(dst) == _sha(src)  # plain copy
