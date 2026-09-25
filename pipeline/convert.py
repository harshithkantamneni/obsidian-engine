import json
import os
import re
import shutil
from pathlib import Path

from core.paths import MEDIA_DIR, REMOTION_SRC, REMOTION_PUBLIC
from core.log import get_logger
from pipeline.audio_assets import select_background_music, select_scene_ambient, select_scene_sfx

logger = get_logger(__name__)


def align_scenes_to_words(n_scenes, words, total_duration, scene_word_ranges=None):
    """Distribute scenes aligned to actual word timestamps.
    If scene_word_ranges is provided (from scene-aware audio), use precise boundaries.
    Otherwise fall back to even word distribution."""
    if n_scenes == 0:
        return []
    if not words or len(words) == 0:
        return [(round(i / n_scenes * total_duration, 3),
                 round((i + 1) / n_scenes * total_duration, 3))
                for i in range(n_scenes)]

    # ── Precise alignment using scene word ranges from scene-aware audio ──
    if scene_word_ranges and len(scene_word_ranges) == n_scenes:
        timings = []
        for i, (w_start_idx, w_end_idx) in enumerate(scene_word_ranges):
            # Clamp indices to valid range
            w_start_idx = max(0, min(w_start_idx, len(words) - 1))
            w_end_idx = max(w_start_idx, min(w_end_idx, len(words) - 1))

            start_t = words[w_start_idx]["start"]
            end_t = words[w_end_idx]["end"] if i < n_scenes - 1 else total_duration

            # Ensure minimum scene duration of 4.0s (multi-scene videos only)
            if end_t - start_t < 4.0 and n_scenes > 1:
                end_t = min(start_t + 4.0, total_duration)
            timings.append((round(start_t, 3), round(end_t, 3)))
        # Post-pass: shrink any scene whose end_time overlaps next scene's start_time
        for i in range(len(timings) - 1):
            s_i, e_i = timings[i]
            s_next, _ = timings[i + 1]
            if e_i > s_next:
                timings[i] = (s_i, s_next)
        logger.info("[Convert] Scene alignment: precise (scene-aware word ranges)")
        return timings

    # ── Fallback: even word distribution ──
    total_words = len(words)
    if total_words < n_scenes:
        logger.warning(f"[Convert] WARNING: Only {total_words} words for {n_scenes} scenes — using even spacing")
        return [(round(i / n_scenes * total_duration, 3),
                 round((i + 1) / n_scenes * total_duration, 3))
                for i in range(n_scenes)]
    timings = []
    for i in range(n_scenes):
        w_start = min(int(i * total_words / n_scenes), total_words - 1)
        w_end   = min(int((i + 1) * total_words / n_scenes) - 1, total_words - 1)
        w_end   = max(w_end, w_start)  # never let end < start
        start_t = words[w_start]["start"]
        end_t   = words[w_end]["end"] if i < n_scenes - 1 else total_duration
        if end_t - start_t < 4.0 and n_scenes > 1:
            end_t = min(start_t + 4.0, total_duration)
        timings.append((round(start_t, 3), round(end_t, 3)))
    # Post-pass: shrink any scene whose end_time overlaps next scene's start_time
    for i in range(len(timings) - 1):
        s_i, e_i = timings[i]
        s_next, _ = timings[i + 1]
        if e_i > s_next:
            timings[i] = (s_i, s_next)
    return timings

# ── Stage 11: Convert to Remotion ─────────────────────────────────────────────
def run_convert(manifest, audio_data, topic="", era=""):
    ts_file = MEDIA_DIR / "timestamps.json"
    scene_word_ranges = None
    if ts_file.exists():
        with open(ts_file) as f:
            ts = json.load(f)
        words = ts.get("words", [])
        # Filter standalone em-dashes and typographic punctuation that appear as
        # separate caption words (AI-revealing artifact fix)
        _PUNCT_ONLY = re.compile(r'^[\u2014\u2013\u2012\u2015\u2010\-\u2026]+$')
        words = [w for w in words if not _PUNCT_ONLY.match(w.get("word", "").strip())]
        scene_word_ranges = ts.get("scene_word_ranges")  # From scene-aware audio
    else:
        logger.warning("[Convert] ⚠️  timestamps.json not found — scenes will use even spacing")
        words = []

    total_duration = audio_data.get("total_duration_seconds") if isinstance(audio_data, dict) else None
    if not total_duration:
        raise ValueError("[Convert] audio_data missing total_duration_seconds — ensure Stage 8 completed")
    scenes = manifest.get("scenes", [])
    n              = len(scenes)

    if n == 0:
        raise ValueError("[Convert] No scenes in manifest — cannot build video data")

    # Build scene timings aligned to word timestamps
    # Uses precise scene boundaries from scene-aware audio when available
    timings = align_scenes_to_words(n, words, total_duration, scene_word_ranges=scene_word_ranges)

    remotion_scenes = []
    for i, scene in enumerate(scenes):
        start_time, end_time = timings[i]
        narration  = scene.get("narration","") or scene.get("visual_query","")

        # Detect year/location from narration
        year_match = re.search(r'\b(\d{1,4}\s*(?:AD|BC|BCE|CE))\b', narration, re.IGNORECASE)
        year = year_match.group(1) if year_match else ""
        location = ""
        for loc in ["Rome","Athens","Egypt","Constantinople","Jerusalem","London","Paris","Alexandria"]:
            if loc.lower() in narration.lower():
                location = loc
                break

        # Copy ai_image to remotion/public (always overwrite to pick up regenerated images)
        ai_image_name = None
        ai_image_src  = scene.get("ai_image")
        if ai_image_src:
            src = Path(ai_image_src)
            if src.exists():
                dest = REMOTION_PUBLIC / src.name
                shutil.copy2(src, dest)
                ai_image_name = src.name

        # Ambient sound per scene mood — via providers.sfx (built-ins: Epidemic API → local)
        ambient_file = select_scene_ambient(scene, REMOTION_PUBLIC)

        # SFX one-shot per scene (only on key moments) — via providers.sfx
        sfx_file = None
        sfx_start_offset = 0
        try:
            from scripts.setup_sfx import should_play_sfx
            if should_play_sfx(scene):
                sfx_file = select_scene_sfx(scene, REMOTION_PUBLIC)
                # Align SFX to reveal word timestamp if available
                if sfx_file and scene.get("is_reveal_moment") and words:
                    scene_words = [w for w in words
                                   if start_time <= w.get("start", 0) <= end_time]
                    if len(scene_words) > 3:
                        # Place SFX 2 words before scene midpoint for maximum impact
                        mid_idx = len(scene_words) // 2
                        sfx_word = scene_words[max(0, mid_idx - 2)]
                        sfx_start_offset = round(sfx_word["start"] - start_time, 3)
        except Exception:
            pass

        remotion_scenes.append({
            "narration":  narration,
            "start_time": start_time,
            "end_time":   end_time,
            "mood":       scene.get("mood","dark"),
            "year":       year or scene.get("year", ""),
            "location":   location or scene.get("location", ""),
            "ai_image":   ai_image_name,
            "is_reveal_moment":    scene.get("is_reveal_moment", False),
            "narrative_position":  scene.get("narrative_position", ""),
            "characters_mentioned": scene.get("characters_mentioned", []),
            # Motion graphics cues from scene breakdown
            "show_map":       scene.get("show_map", False),
            "show_timeline":  scene.get("show_timeline", False),
            "lower_third":    scene.get("lower_third"),
            "key_text":       scene.get("key_text"),
            "key_text_type":  scene.get("key_text_type"),
            "era_start":      scene.get("era_start", ""),
            "era_end":        scene.get("era_end", ""),
            "ambient_file":   ambient_file,
            "sfx_file":       sfx_file,
            "sfx_start_offset": sfx_start_offset,
            "visual_treatment": scene.get("visual_treatment", "standard"),
            "is_breathing_room": scene.get("is_breathing_room", False),
            "narrative_function": scene.get("narrative_function", "exposition"),
        })

    # Last scene ends exactly at total duration
    if not remotion_scenes:
        raise ValueError("[Convert] No scenes produced — cannot build video data")
    remotion_scenes[-1]["end_time"] = total_duration

    # Multi-image crossfade for long scenes (>12s):
    # Borrow images from adjacent scenes for visual variety within a single scene.
    # Falls back to sub-scene splitting if no adjacent images available.
    MAX_SCENE_SECS = 12.0
    for i, scene in enumerate(remotion_scenes):
        duration = scene["end_time"] - scene["start_time"]
        if duration > MAX_SCENE_SECS and scene.get("ai_image"):
            # Collect nearby unique images (current + neighbors)
            nearby_images = []
            if scene["ai_image"]:
                nearby_images.append(scene["ai_image"])
            for offset in [-1, 1, -2, 2]:
                ni = i + offset
                if 0 <= ni < len(remotion_scenes):
                    img = remotion_scenes[ni].get("ai_image")
                    if img and img not in nearby_images:
                        nearby_images.append(img)
                    if len(nearby_images) >= 3:
                        break
            if len(nearby_images) >= 2:
                scene["ai_images"] = nearby_images
                logger.info(f"[Convert] Multi-image scene: {duration:.1f}s with {len(nearby_images)} images")

    # Sub-scene split for remaining long single-image scenes
    split_scenes = []
    for scene in remotion_scenes:
        duration = scene["end_time"] - scene["start_time"]
        if duration > MAX_SCENE_SECS and scene.get("ai_image") and not scene.get("ai_images"):
            n_splits = max(2, min(4, int(duration / 8)))  # 2-4 sub-scenes
            sub_dur = duration / n_splits
            for j in range(n_splits):
                sub = dict(scene)
                sub["start_time"] = round(scene["start_time"] + j * sub_dur, 3)
                sub["end_time"]   = round(scene["start_time"] + (j + 1) * sub_dur, 3)
                split_scenes.append(sub)
            logger.info(f"[Convert] Split {duration:.1f}s scene into {n_splits} sub-scenes")
        else:
            split_scenes.append(scene)
    remotion_scenes = split_scenes

    # Select background music — providers.music first, then local library fallbacks
    music_file, music_start_offset = select_background_music(remotion_scenes, total_duration, REMOTION_PUBLIC)

    # Attempt track adaptation (exact duration + stems) if API available
    music_adapted = False
    music_stems = None
    _music_track_id = None
    if music_file and os.getenv("EPIDEMIC_SOUND_API_KEY"):
        # Extract track_id from epidemic filename (epidemic_api_mood_title_TRACKID.mp3)
        _mf_name = music_file.rsplit("/", 1)[-1] if "/" in music_file else music_file
        if _mf_name.startswith("epidemic_"):
            parts = _mf_name.rsplit("_", 1)
            if len(parts) == 2:
                _music_track_id = parts[1].replace(".mp3", "")

        if _music_track_id:
            try:
                from media.track_adapter import adapt_to_duration
                adapted = adapt_to_duration(
                    _music_track_id, total_duration,
                    scenes=remotion_scenes,
                    download_stems=True,
                )
                if adapted:
                    music_file = adapted["adapted_file"]
                    music_start_offset = 0
                    music_adapted = not adapted.get("loopable", False)
                    music_stems = adapted.get("stems")
                    logger.info(f"[Convert] Track adapted: {music_file} "
                                f"(stems={'yes' if music_stems else 'no'}, "
                                f"loopable={adapted.get('loopable', False)})")
            except Exception as _adapt_err:
                logger.warning(f"[Convert] Track adaptation failed (using original): {_adapt_err}")

    # Select secondary music for Act 3 crossfade
    music_file_secondary = None
    music_secondary_start_offset = 0
    if music_file:
        try:
            from media import music_manager
            # Determine dominant mood for contrast selection
            mood_counts = {}
            for s in remotion_scenes:
                m = s.get("mood", "dark")
                mood_counts[m] = mood_counts.get(m, 0) + 1
            dominant = max(mood_counts, key=mood_counts.get) if mood_counts else "dark"
            smart_sec = music_manager.get_smart_secondary_music(
                dominant, remotion_scenes, primary_track=music_file, total_duration=total_duration
            )
            if smart_sec:
                music_file_secondary = smart_sec["music_file"]
                music_secondary_start_offset = smart_sec["music_start_offset"]
            else:
                music_file_secondary = music_manager.get_secondary_music(dominant, remotion_scenes, primary_track=music_file)
        except Exception as e:
            logger.warning(f"[Convert] Secondary music selection failed (non-fatal): {e}")

    # Fetch recommended video for endscreen
    endscreen_rec = None
    if topic and era:
        try:
            from clients.supabase_client import get_client
            client = get_client()
            result = client.table("videos").select("youtube_id, topic, era, views, thumbnail_url") \
                .eq("era", era) \
                .order("views", desc=True) \
                .limit(10) \
                .execute()
            for row in (result.data or []):
                yt_id = row.get("youtube_id", "")
                row_topic = row.get("topic", "")
                if yt_id and row_topic.lower() != topic.lower():
                    thumb = row.get("thumbnail_url") or f"https://i.ytimg.com/vi/{yt_id}/hqdefault.jpg"
                    endscreen_rec = {
                        "youtube_id": yt_id,
                        "title": row_topic[:60],
                        "thumbnail": thumb,
                    }
                    logger.info(f"[Convert] Endscreen recommended: {row_topic[:50]} ({yt_id})")
                    break
        except Exception as e:
            logger.warning(f"[Convert] Endscreen recommendation lookup skipped: {e}")

    video_data = {
        "total_duration_seconds": total_duration,
        "scenes": remotion_scenes,
        "word_timestamps": words,
        "music_file": music_file,
        "music_start_offset": music_start_offset,
        "music_file_secondary": music_file_secondary,
        "music_secondary_start_offset": music_secondary_start_offset,
        "music_adapted": music_adapted,
        "music_stems": music_stems,
        "showEndscreen": True,
        "endscreen_recommended": endscreen_rec,
    }

    # Validate video data before writing
    for i, sc in enumerate(remotion_scenes):
        if sc["end_time"] <= sc["start_time"]:
            logger.warning(f"[Convert] WARNING: Scene {i} has end_time <= start_time — fixing")
            remotion_scenes[i]["end_time"] = sc["start_time"] + 1.0
        if sc.get("ai_image") and not (REMOTION_PUBLIC / sc["ai_image"]).exists():
            logger.warning(f"[Convert] WARNING: Scene {i} image {sc['ai_image']} not found in public/ — clearing")
            remotion_scenes[i]["ai_image"] = None

    # Resolve scene intents → concrete rendering parameters
    try:
        from media.scene_intent import resolve_all_scenes
        remotion_scenes = resolve_all_scenes(remotion_scenes)
        logger.info(f"[Convert] ✓ Scene intents resolved for {len(remotion_scenes)} scenes")
    except Exception as e:
        logger.warning(f"[Convert] Scene intent resolution skipped (non-fatal): {e}")

    # Post-resolve: enforce Invariant 17 — silence beat hard constraint.
    # Suppress all audio and clamp music volume on silence beat scenes and their follower.
    for i, sc in enumerate(remotion_scenes):
        if sc.get("intent_silence_beat"):
            sc["sfx_file"] = None
            sc["sfx_volume"] = 0
            sc["ambient_file"] = None
            sc["intent_music_volume_base"] = min(sc.get("intent_music_volume_base", 0.5), 0.50)
            # Clamp the immediately following scene's music volume too (avoids jarring jump)
            if i + 1 < len(remotion_scenes):
                following = remotion_scenes[i + 1]
                following["intent_music_volume_base"] = min(
                    following.get("intent_music_volume_base", 0.5), 0.50
                )

    # Post-resolve: speech intensity delta cap.
    # Clamp scene-to-scene speech intensity jumps to <=0.25 to prevent jarring voice shifts.
    for i in range(1, len(remotion_scenes)):
        prev_intensity = remotion_scenes[i - 1].get("intent_speech_intensity", 0.5)
        curr_intensity = remotion_scenes[i].get("intent_speech_intensity", 0.5)
        delta = curr_intensity - prev_intensity
        if abs(delta) > 0.25:
            clamped = prev_intensity + 0.25 * (1 if delta > 0 else -1)
            logger.warning(
                f"[Convert] Speech intensity delta capped: scene {i} "
                f"{curr_intensity:.2f}->{clamped:.2f} (delta={delta:+.2f})"
            )
            remotion_scenes[i]["intent_speech_intensity"] = round(clamped, 3)

    # Pacing audit: warn on non-exempt scenes below 25s documentary target
    # Must run AFTER resolve_all_scenes() so intent_silence_beat is populated.
    _EXEMPT_FUNCTIONS = {"cold_open", "hook", "silence", "breathing_room"}
    for _i, _sc in enumerate(remotion_scenes):
        _dur = _sc["end_time"] - _sc["start_time"]
        _fn = _sc.get("narrative_function", "")
        _is_silence = _sc.get("intent_silence_beat", False) or _sc.get("is_breathing_room", False)
        if _dur < 25.0 and _fn not in _EXEMPT_FUNCTIONS and not _is_silence:
            logger.warning(
                f"[Pacing] Scene {_i} ({_fn}) is {_dur:.1f}s — below 25s documentary target"
            )

    # Inject synthetic reflection scene at act3→ending boundary
    try:
        inject_idx = None
        for idx in range(len(remotion_scenes) - 1):
            curr_pos = remotion_scenes[idx].get("narrative_position", "")
            next_pos = remotion_scenes[idx + 1].get("narrative_position", "")
            if curr_pos == "act3" and next_pos == "ending":
                inject_idx = idx + 1
                break
        if inject_idx is not None:
            act3_end_time = remotion_scenes[inject_idx - 1].get("end_time", 0)
            reflection_scene = {
                "narration": "",
                "start_time": act3_end_time,
                "end_time": act3_end_time + 3.0,
                "mood": "dark",
                "visual_treatment": "standard",
                "intent_music_volume_base": 1.15,
                "intent_scene_energy": 0.1,
                "narrative_position": "ending",
                "narrative_function": "breathing_room",
                "is_synthetic": True,
                "is_breathing_room": True,
                "words": [],
                "ai_image": remotion_scenes[inject_idx - 1].get("ai_image", ""),
                "image_url": "",
            }
            remotion_scenes.insert(inject_idx, reflection_scene)
            # Shift subsequent scene start times by 3.0s
            for s in remotion_scenes[inject_idx + 1:]:
                s["start_time"] = s.get("start_time", 0) + 3.0
                s["end_time"] = s.get("end_time", 0) + 3.0
            total_duration += 3.0
            video_data["total_duration_seconds"] = total_duration
            # Shift word timestamps that fall at or after the injection boundary
            # so captions stay in sync with their shifted scenes
            reflection_dur = 3.0
            for w in words:
                if w.get("start", 0) >= act3_end_time:
                    w["start"] = w.get("start", 0) + reflection_dur
                    w["end"] = w.get("end", 0) + reflection_dur
            logger.info(f"[Convert] ✓ Injected {reflection_dur}s reflection scene at act3→ending boundary (index {inject_idx})")
    except Exception as _refl_err:
        logger.warning(f"[Convert] Reflection scene injection skipped: {_refl_err}")

    # Compute film grain and vignette intensity per scene
    for sc in remotion_scenes:
        mood = (sc.get("mood", "") or "dark").lower()
        is_reveal = sc.get("is_reveal_moment", False)
        grain = 0.10  # default
        vignette = 0.15  # default
        if mood in ("dark", "tense"):
            grain = 0.20
            vignette = 0.30
        elif mood in ("wonder", "warmth"):
            grain = 0.08
            vignette = 0.08
        elif mood == "dramatic":
            grain = 0.15
            vignette = 0.25
        if is_reveal:
            grain = min(0.30, grain + 0.05)
            vignette = min(0.40, vignette + 0.05)
        sc["film_grain_intensity"] = round(max(0.0, min(0.30, grain)), 2)
        sc["vignette_intensity"] = round(max(0.0, min(0.40, vignette)), 2)

    video_data["scenes"] = remotion_scenes

    # Inject audio_config for Remotion ducking/volume (reads from overrides if set)
    try:
        from core.param_overrides import get_override
        audio_cfg = {
            "ducking": {
                "speechVolume": get_override("ducking.speech_volume", 0.18),
                "silenceVolume": get_override("ducking.silence_volume", 0.28),
                "attackSeconds": get_override("ducking.attack_seconds", 0.1),
                "releaseSeconds": get_override("ducking.release_seconds", 0.4),
            },
            "actMultipliers": {
                "act1": get_override("volume.act1", 0.85),
                "act2": get_override("volume.act2", 1.00),
                "act3": get_override("volume.act3", 1.25),
                "ending": get_override("volume.ending", 0.90),
            },
            "crossfadeStartPct": get_override("music.crossfade_start_pct", 0.60),
        }
        # Stem ducking config — reads from optimizer (self-tunes from analytics)
        if music_stems:
            audio_cfg["stemDucking"] = {
                "bass": {
                    "speechVolume": get_override("stem_ducking.bass.speech", 0.10),
                    "silenceVolume": get_override("stem_ducking.bass.silence", 0.35),
                },
                "drums": {
                    "speechVolume": get_override("stem_ducking.drums.speech", 0.08),
                    "silenceVolume": get_override("stem_ducking.drums.silence", 0.30),
                },
                "instruments": {
                    "speechVolume": get_override("stem_ducking.instruments.speech", 0.05),
                    "silenceVolume": get_override("stem_ducking.instruments.silence", 0.40),
                },
            }
        video_data["audio_config"] = audio_cfg
    except Exception:
        pass  # Non-fatal — Remotion uses its own defaults if missing

    # Build scene manifest for analytics BEFORE json.dump so it's included in video-data.json
    scene_manifest = []
    for i, sc in enumerate(remotion_scenes):
        st = sc.get("start_time", 0) or 0
        et = sc.get("end_time", 0) or 0
        scene_manifest.append({
            "scene_id": i,
            "start_pct": round(st / total_duration, 4) if total_duration else 0,
            "end_pct": round(et / total_duration, 4) if total_duration else 0,
            "start_time": round(st, 2),
            "end_time": round(et, 2),
            "mood": sc.get("mood", "dark"),
            "narrative_function": sc.get("narrative_function", "exposition"),
            "narrative_position": sc.get("narrative_position", ""),
            "is_reveal_moment": sc.get("is_reveal_moment", False),
            "is_breathing_room": sc.get("is_breathing_room", False),
            "intent_silence_beat": sc.get("intent_silence_beat", False),
            "intent_scene_energy": sc.get("intent_scene_energy", 0.5),
            "intent_speech_intensity": sc.get("intent_speech_intensity", 0.5),
            "intent_music_volume_base": sc.get("intent_music_volume_base", 0.5),
            "intent_transition_type": sc.get("intent_transition_type", "normal"),
            "intent_pace_modifier": sc.get("intent_pace_modifier", 1.0),
            "intent_caption_style": sc.get("intent_caption_style", "standard"),
            "intent_motion_seed": sc.get("intent_motion_seed", 0),
            "visual_treatment": sc.get("visual_treatment", "standard"),
            "claim_confidence": sc.get("claim_confidence"),
            "has_ai_image": bool(sc.get("ai_image")),
            "has_sfx": bool(sc.get("sfx_file")),
            "has_ambient": bool(sc.get("ambient_file")),
            "word_count": len((sc.get("narration") or "").split()),
            "characters_mentioned": sc.get("characters_mentioned", []),
        })
    video_data["scene_manifest"] = scene_manifest

    vd_path = REMOTION_SRC / "video-data.json"
    with open(vd_path, "w") as f:
        json.dump(video_data, f, indent=2)

    # Copy audio
    shutil.copy2(MEDIA_DIR / "narration.mp3", REMOTION_PUBLIC / "narration.mp3")

    scenes_with_images = sum(1 for s in remotion_scenes if s.get("ai_image"))
    logger.info(f"[Convert] ✓ {len(remotion_scenes)} scenes, {len(words)} words, {scenes_with_images} images")
    logger.info(f"[Convert] ✓ Duration: {total_duration/60:.1f} min")

    # Save music metadata to Supabase for analytics
    if topic and music_file:
        try:
            from clients.supabase_client import save_music_metadata
            _mf = music_file or ""
            _source = "epidemic_api" if "epidemic_api_" in _mf else (
                "epidemic_local" if "epidemic_" in _mf else "kevin_macleod"
            )
            _mood_counts = {}
            for _s in remotion_scenes:
                _m = _s.get("mood", "dark")
                _mood_counts[_m] = _mood_counts.get(_m, 0) + 1
            _dominant = max(_mood_counts, key=_mood_counts.get) if _mood_counts else "dark"
            save_music_metadata(topic, {
                "track_id": _music_track_id,
                "mood": _dominant,
                "source": _source,
                "adapted": music_adapted,
                "stems_used": bool(music_stems),
            })
        except Exception as _meta_err:
            logger.warning(f"[Convert] Music metadata save failed (non-fatal): {_meta_err}")

    return video_data
