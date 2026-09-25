import os
import sys
import json
import re
import time
import shutil
import subprocess
from pathlib import Path

from core.paths import MEDIA_DIR, CHUNKS_DIR
from core.log import get_logger
from pipeline.helpers import clean_script
from pipeline.voice import _get_scene_voice_settings, _get_inter_scene_pause, _generate_silence_file

logger = get_logger(__name__)


# ── TTS provider plumbing (shared with pipeline/shorts.py) ─────────────────

_even_split_warned = False


def _audio_duration(path) -> float:
    try:
        from mutagen import File as _MFile
        info = _MFile(str(path))
        if info is not None and info.info:
            return float(info.info.length)
    except Exception:
        pass
    return 0.0


def _is_pipeline_mp3(path: Path) -> bool:
    """True if path is a 44.1kHz MP3 (the format ElevenLabs returns).

    Chunks are joined with ``ffmpeg -c copy`` next to 44.1kHz mono silence
    files, so every chunk must share that sample rate; anything else (e.g.
    24kHz MP3 or WAV from another provider) is transcoded.
    """
    try:
        from mutagen.mp3 import MP3
        info = MP3(str(path)).info
        return info.sample_rate == 44100
    except Exception:
        return False


def _store_chunk_audio(src: Path, chunk_path: Path) -> None:
    """Move/transcode a provider's audio file into chunk_path as MP3 44.1kHz mono."""
    src = Path(src)
    if src.suffix.lower() == ".mp3" and _is_pipeline_mp3(src):
        shutil.copyfile(src, chunk_path)
    else:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ar", "44100", "-ac", "1",
             "-c:a", "libmp3lame", "-b:a", "192k", str(chunk_path)],
            check=True, capture_output=True,
        )
    # Clean up provider temp files (never delete files a provider keeps elsewhere)
    try:
        import tempfile
        if Path(tempfile.gettempdir()).resolve() in src.resolve().parents:
            src.unlink(missing_ok=True)
    except Exception:
        pass


def _even_word_split(text: str, duration: float) -> list[dict]:
    words = text.split()
    if not words:
        return []
    duration = duration or len(words) * 0.4
    per = duration / len(words)
    return [{"word": w, "start": round(i * per, 3), "end": round((i + 1) * per, 3)}
            for i, w in enumerate(words)]


def _align_chunk_words(chunk_path: Path, text: str) -> list[dict]:
    """Word timestamps for a chunk whose TTS provider returned none.

    Uses media/forced_alignment (Whisper if installed); if that is unavailable
    or fails, spreads words evenly over the chunk duration.
    """
    global _even_split_warned
    try:
        from media.forced_alignment import align_audio_to_text
        words = align_audio_to_text(Path(chunk_path), text)
        if words:
            return words
    except Exception as e:
        logger.debug(f"[Audio] Forced alignment failed: {e}")
    if not _even_split_warned:
        _even_split_warned = True
        logger.warning("[Audio] TTS provider returned no word timestamps and forced alignment "
                       "is unavailable — spreading words evenly (captions will be approximate). "
                       "Install openai-whisper for accurate captions.")
    return _even_word_split(text, _audio_duration(chunk_path))


def synthesize_chunk(tts, text: str, chunk_path: Path, voice_settings=None,
                     speed: float = 1.0, role: str = "narrator") -> list[dict]:
    """Synthesize one chunk with the configured TTS provider.

    Writes MP3 audio to chunk_path and returns word timestamps relative to the
    start of the chunk. Timestamps come from the provider when it has them,
    otherwise from forced alignment (see _align_chunk_words).
    """
    voice_id = tts.resolve_voice(role) or None
    audio_file, words = tts.synthesize(text, voice_id=voice_id,
                                       voice_settings=voice_settings, speed=speed)
    _store_chunk_audio(Path(audio_file), Path(chunk_path))
    words = [
        {"word": str(w["word"]), "start": round(float(w["start"]), 3), "end": round(float(w["end"]), 3)}
        for w in (words or []) if str(w.get("word", "")).strip()
    ]
    if not words:
        words = _align_chunk_words(Path(chunk_path), text)
    return words


def _log_tts_cost(n_chars: int) -> None:
    try:
        from core.cost_tracker import get_active_run_id, log_cost
        from providers.registry import get_provider_name
        _rid = get_active_run_id()
        if _rid:
            log_cost(_rid, "audio", get_provider_name("tts"), n_chars, "characters")
    except Exception:
        pass


def run_audio(script_data, scene_data=None, display_script=None):
    try:
        from mutagen.mp3 import MP3
    except ImportError:
        logger.info("[Audio] Installing mutagen...")
        subprocess.run([sys.executable, "-m", "pip", "install", "mutagen"], check=True)
        from mutagen.mp3 import MP3

    # TTS provider from obsidian.yaml (providers.tts). Each provider raises its
    # own clear error if credentials are missing.
    from providers.registry import get_provider
    tts = get_provider("tts")

    # Voice config from obsidian.yaml (via pipeline_config)
    from core.pipeline_config import (
        NARRATOR_VOICE_ID, QUOTE_VOICE_ID as _QUOTE_VID,
        VOICE_BODY, VOICE_HOOK, VOICE_QUOTE, VOICE_SPEED_BODY,
        AUDIO_CHUNK_MAX_CHARS,
    )
    VOICE_ID = NARRATOR_VOICE_ID
    MAX_CHARS = AUDIO_CHUNK_MAX_CHARS
    VOICE_SPEED = VOICE_SPEED_BODY
    QUOTE_VOICE_ID = _QUOTE_VID

    # Split text into chunks at sentence boundaries
    def split_chunks(text):
        sentences = text.replace('\n', ' ').split('. ')
        chunks, current = [], ""
        for s in sentences:
            part = s + ". "
            if len(current) + len(part) > MAX_CHARS and current:
                chunks.append(current.strip())
                current = part
            else:
                current += part
        if current.strip() and len(current.strip()) > 2:
            chunks.append(current.strip())
        return chunks

    def detect_quoted_speech(text):
        """Detect if a chunk contains quoted speech (historical figure dialogue)."""
        quotes = re.findall(r'["\u201c]([^"\u201d]{10,200})["\u201d]', text)
        return len(quotes) > 0 and any(
            word in text.lower() for word in
            ['said', 'wrote', 'declared', 'proclaimed', 'whispered', 'shouted',
             'announced', 'commanded', 'stated', 'replied', 'exclaimed']
        )

    # ── Determine chunking strategy ───────────────────────────────────────────
    # Scene-aware: chunk by scene narration with mood-specific voice settings
    # Legacy: chunk by text splitting with keyword-based prosody detection

    all_scenes = []
    scenes = []
    if scene_data and isinstance(scene_data, dict):
        all_scenes = scene_data.get("scenes", [])

    # Build list of (original_index, scene) pairs, filtering empty narration
    narrated_scenes = [
        (orig_idx, s) for orig_idx, s in enumerate(all_scenes)
        if (s.get("narration", "") or "").strip()
    ]

    use_scene_aware = len(narrated_scenes) >= 3  # Need meaningful scene data

    if use_scene_aware:
        # ── Resolve scene intent for ALL scenes (not just narrated) ──────
        # This ensures convert.py can reuse cached intents instead of
        # re-resolving on a potentially different scene list.
        try:
            from media.scene_intent import resolve_all_scenes
            all_scenes_resolved = resolve_all_scenes(all_scenes)
            # Write intent cache so convert.py can skip re-resolution
            _intent_cache_path = MEDIA_DIR / "scene_intents_cache.json"
            _intent_cache = []
            for _rs in all_scenes_resolved:
                _intent_cache.append({
                    k: v for k, v in _rs.items()
                    if k.startswith("intent_")
                })
            import tempfile
            _tmp_fd, _tmp_path = tempfile.mkstemp(
                dir=str(MEDIA_DIR), suffix=".json"
            )
            try:
                with os.fdopen(_tmp_fd, "w") as _tmp_f:
                    json.dump(_intent_cache, _tmp_f, indent=2)
                os.replace(_tmp_path, str(_intent_cache_path))
                logger.info(f"[Audio] Cached scene intents for {len(_intent_cache)} scenes")
            except Exception:
                Path(_tmp_path).unlink(missing_ok=True)
                raise

            # Update all_scenes with resolved data (used by narrated_scenes below)
            all_scenes = all_scenes_resolved

            # Rebuild narrated_scenes from updated all_scenes
            narrated_scenes = [
                (orig_idx, all_scenes[orig_idx])
                for orig_idx, _ in narrated_scenes
            ]
        except Exception as _intent_err:
            logger.warning(f"[Audio] WARNING: Scene intent resolution failed: {_intent_err}")

        # ── SCENE-AWARE PATH ──────────────────────────────────────────────
        # Build chunk list from scene narrations with mood-specific voice settings
        # Each entry: (text, voice_settings, voice_id, speed, scene_idx)
        # Plus optional silence entries: (None, None, None, None, scene_idx, pause_duration)

        total_scenes = len(narrated_scenes)
        scenes = [s for _, s in narrated_scenes]
        total_words_est = sum(len((s.get("narration", "") or "").split()) for s in scenes)
        logger.info(f"[Audio] Scene-aware mode: {total_scenes} scenes, ~{total_words_est} words")

        chunk_plan = []  # list of dicts describing each chunk
        for si, (orig_idx, scene) in enumerate(narrated_scenes):
            narration = clean_script(scene.get("narration", "").strip())
            if not narration:
                continue

            vs, vid, spd = _get_scene_voice_settings(scene, si, total_scenes)
            # Apply intent pace modifier from scene_intent resolution
            pace_mod = scene.get("intent_pace_modifier", 1.0)
            final_speed = round(max(0.65, min(1.0, spd * pace_mod)), 2)
            logger.info(f"  [Voice] Scene {orig_idx} (pos {si}): raw_spd={spd:.2f} × pace={pace_mod} = {final_speed}")
            vs["speed"] = final_speed
            spd = final_speed
            mood = (scene.get("mood", "") or "dark").lower()
            label = f"scene {si+1}/{total_scenes} [{mood}]"

            # Sub-chunk long narrations
            if len(narration) > MAX_CHARS:
                sub_chunks = split_chunks(narration)
                for sci, sc_text in enumerate(sub_chunks):
                    chunk_plan.append({
                        "type": "audio", "text": sc_text,
                        "vs": vs, "vid": vid, "spd": spd,
                        "scene_idx": si, "label": f"{label} part {sci+1}/{len(sub_chunks)}",
                    })
            else:
                chunk_plan.append({
                    "type": "audio", "text": narration,
                    "vs": vs, "vid": vid, "spd": spd,
                    "scene_idx": si, "label": label,
                })

            # Add inter-scene silence
            next_scene = scenes[si + 1] if si < total_scenes - 1 else None
            pause = _get_inter_scene_pause(scene, next_scene, si, total_scenes)
            if pause > 0:
                chunk_plan.append({
                    "type": "silence", "duration": pause,
                    "scene_idx": si, "label": f"pause {pause:.1f}s after scene {si+1}",
                })

        logger.info(f"[Audio] {len(chunk_plan)} chunks planned "
              f"({sum(1 for c in chunk_plan if c['type'] == 'audio')} audio + "
              f"{sum(1 for c in chunk_plan if c['type'] == 'silence')} pauses)")

    else:
        # ── LEGACY PATH ───────────────────────────────────────────────────
        full_script = clean_script(script_data.get("full_script", ""))
        logger.info(f"[Audio] Legacy mode: {len(full_script)} chars, {len(full_script.split())} words")
        text_chunks = split_chunks(full_script)

        chunk_plan = []
        for i, chunk in enumerate(text_chunks):
            chunk_lower = chunk.lower()
            if i == 0:
                vs, vid, spd = VOICE_HOOK, VOICE_ID, 0.92
            elif detect_quoted_speech(chunk):
                from core.param_overrides import get_override
                vs, vid, spd = VOICE_QUOTE, QUOTE_VOICE_ID, get_override("voice_speed.quote_legacy", 0.85)
            elif any(phrase in chunk_lower for phrase in [
                "the truth", "what really happened", "no one knew", "the real story",
                "but here's what", "what they found", "the evidence shows",
                "it was actually", "in reality",
            ]):
                vs = {"stability": 0.30, "similarity_boost": 0.85, "style": 0.95, "use_speaker_boost": True}
                vid, spd = VOICE_ID, 0.82
            elif any(phrase in chunk_lower for phrase in [
                "but then", "everything changed", "no one expected", "suddenly",
                "without warning", "in secret", "behind closed doors",
            ]):
                vs = {"stability": 0.35, "similarity_boost": 0.82, "style": 0.90, "use_speaker_boost": True}
                vid, spd = VOICE_ID, 0.86
            elif i == len(text_chunks) - 1:
                vs = {"stability": 0.50, "similarity_boost": 0.82, "style": 0.80, "use_speaker_boost": True}
                vid, spd = VOICE_ID, 0.85
            else:
                vs, vid, spd = VOICE_BODY, VOICE_ID, VOICE_SPEED
            chunk_plan.append({
                "type": "audio", "text": chunk,
                "vs": vs, "vid": vid, "spd": spd,
                "scene_idx": None, "label": f"chunk {i+1}/{len(text_chunks)}",
            })

        logger.info(f"[Audio] {len(chunk_plan)} chunks")

    # ── Clear stale chunks from previous runs ─────────────────────────────
    for stale in CHUNKS_DIR.glob("chunk_*.mp3"):
        stale.unlink(missing_ok=True)
    for stale in CHUNKS_DIR.glob("chunk_*_ts.json"):
        stale.unlink(missing_ok=True)
    for stale in CHUNKS_DIR.glob("silence_*.mp3"):
        stale.unlink(missing_ok=True)

    # ── Generate audio for each chunk ─────────────────────────────────────
    all_words = []
    time_offset = 0.0
    concat_files = []  # ordered list of MP3 files for ffmpeg concat
    scene_word_ranges = {}  # scene_idx → (first_word_idx, last_word_idx)

    for i, plan in enumerate(chunk_plan):

        if plan["type"] == "silence":
            # Generate silent audio file
            silence_path = CHUNKS_DIR / f"silence_{i:02d}.mp3"
            pause_dur = plan["duration"]
            try:
                _generate_silence_file(pause_dur, silence_path)
                concat_files.append(silence_path)
                time_offset += pause_dur
                logger.info(f"  [{plan['label']}]")
            except Exception as e:
                logger.warning(f"  [Silence] Failed to generate pause ({e}) — skipping")
            continue

        # Audio chunk
        chunk_path = CHUNKS_DIR / f"chunk_{i:02d}.mp3"
        chunk_ts   = CHUNKS_DIR / f"chunk_{i:02d}_ts.json"

        if chunk_path.exists() and chunk_ts.exists():
            logger.info(f"[Audio] {plan['label']}: cached")
            with open(chunk_ts) as f:
                chunk_words = json.load(f)
        else:
            logger.info(f"[Audio] {plan['label']}: generating ({len(plan['text'])} chars, "
                  f"stab={plan['vs'].get('stability', '?')}, "
                  f"style={plan['vs'].get('style', '?')}, "
                  f"spd={plan['spd']})...")
            role = "quote" if plan["vid"] == QUOTE_VOICE_ID else "narrator"
            chunk_words = synthesize_chunk(tts, plan["text"], chunk_path,
                                           voice_settings=plan["vs"], speed=plan["spd"],
                                           role=role)
            _log_tts_cost(len(plan["text"]))

            with open(chunk_ts, "w") as f:
                json.dump(chunk_words, f)
            time.sleep(0.5)

        concat_files.append(chunk_path)

        # Use ACTUAL MP3 duration for correct offset
        actual_duration = MP3(chunk_path).info.length
        logger.info(f"  {len(chunk_words)} words, {actual_duration:.2f}s, offset={time_offset:.2f}s")

        # Track scene word boundaries for precise alignment
        si = plan.get("scene_idx")
        first_word_idx = len(all_words)

        for w in chunk_words:
            all_words.append({"word": w["word"],
                "start": round(w["start"] + time_offset, 3),
                "end":   round(w["end"]   + time_offset, 3)})

        last_word_idx = len(all_words) - 1
        if si is not None and chunk_words:
            if si in scene_word_ranges:
                # Extend range for multi-chunk scenes
                scene_word_ranges[si] = (scene_word_ranges[si][0], last_word_idx)
            else:
                scene_word_ranges[si] = (first_word_idx, last_word_idx)

        time_offset += actual_duration

    # ── Concat all audio files with ffmpeg ────────────────────────────────
    raw_audio_path = str(MEDIA_DIR / "narration_raw.mp3")
    audio_path = str(MEDIA_DIR / "narration.mp3")
    if not concat_files:
        raise ValueError("[Audio] No audio chunks generated — script may be empty or TTS failed for all chunks")
    elif len(concat_files) == 1:
        shutil.copy2(concat_files[0], raw_audio_path)
    else:
        concat_list = CHUNKS_DIR / "concat_list.txt"
        with open(concat_list, "w") as f:
            for cf in concat_files:
                f.write(f"file '{cf}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", str(raw_audio_path)],
            check=True, capture_output=True
        )
        concat_list.unlink(missing_ok=True)

    # Audio mastering: loudness normalization to YouTube standard (LUFS -14)
    try:
        logger.info("[Audio] Mastering: loudness normalization (LUFS -14)...")
        subprocess.run(
            ["ffmpeg", "-y", "-i", raw_audio_path,
             "-af", "loudnorm=I=-14:LRA=11:TP=-1.5",
             "-ar", "44100", "-b:a", "192k",
             str(audio_path)],
            check=True, capture_output=True
        )
        Path(raw_audio_path).unlink(missing_ok=True)
        logger.info("[Audio] ✓ Mastered to LUFS -14")
    except Exception as e:
        logger.warning(f"[Audio] Mastering failed ({e}), using raw audio")
        shutil.copy2(raw_audio_path, audio_path)

    # Build scene word ranges list (ordered by scene index)
    scene_boundaries = []
    if scene_word_ranges:
        for si in sorted(scene_word_ranges.keys()):
            scene_boundaries.append(list(scene_word_ranges[si]))

    # ── Fix caption text: display_script overrides ───────────────────────
    # display_script contains original spellings (not phonetic). When the
    # TTS format agent rewrites words for pronunciation, captions should
    # show the original display_script text. We align TTS words to display
    # words positionally and replace any that differ.
    if display_script and display_script.get("full_script"):
        try:
            _display_words = display_script["full_script"].split()
            _tts_words = (script_data.get("full_script", "") or "").split()
            # Build a positional map: TTS word index → display word
            # Only works when TTS script and display script have the same word count
            # (the TTS format agent replaces words 1:1 in most cases)
            if len(_display_words) == len(_tts_words):
                _tts_to_display = {}
                for _wi, (_tw, _dw) in enumerate(zip(_tts_words, _display_words)):
                    if _tw.lower() != _dw.lower():
                        _tts_to_display[_tw.lower()] = _dw
                if _tts_to_display:
                    _applied = 0
                    for w in all_words:
                        _wl = w["word"].lower()
                        if _wl in _tts_to_display:
                            w["word"] = _tts_to_display[_wl]
                            _applied += 1
                    if _applied:
                        logger.info(f"[Audio] Fixed {_applied} caption words from display_script")
            else:
                logger.debug(f"[Audio] display_script word count ({len(_display_words)}) != "
                             f"TTS script ({len(_tts_words)}) — skipping positional override")
        except Exception as _ds_err:
            logger.warning(f"[Audio] display_script override failed (non-fatal): {_ds_err}")

    ts_path = str(MEDIA_DIR / "timestamps.json")
    with open(ts_path, "w") as f:
        ts_data = {"words": all_words}
        if scene_boundaries:
            ts_data["scene_word_ranges"] = scene_boundaries
        json.dump(ts_data, f, indent=2)

    total_narration = sum(len(s.get("narration", "").split()) for s in scenes) if scenes else len(
        clean_script(script_data.get("full_script", "")).split())
    total_duration = (all_words[-1]["end"] if all_words else time_offset) + 1.5  # 1.5s tail buffer

    mode_str = f"scene-aware ({len(scenes)} scenes)" if use_scene_aware else "legacy"
    logger.info(f"[Audio] ✓ {len(all_words)} words, {total_duration:.1f}s ({total_duration/60:.1f}min) [{mode_str}]")
    if scene_boundaries:
        logger.info(f"[Audio] ✓ {len(scene_boundaries)} scene boundaries tracked for precise alignment")

    # WPM validation gate
    if total_duration > 0 and total_narration > 0:
        from core.quality_gates import gate_wpm_range
        from core import pipeline_config
        wpm_ok, wpm_msg = gate_wpm_range(total_narration, total_duration)
        if not wpm_ok:
            if pipeline_config.ENFORCE_WPM_GATE:
                raise RuntimeError(f"WPM gate failed: {wpm_msg}")
            else:
                logger.warning(f"[Audio] WARNING: {wpm_msg} (gate not enforced)")

    return {"audio_path": audio_path, "timestamps_path": ts_path,
            "total_duration_seconds": total_duration, "word_count": total_narration}
