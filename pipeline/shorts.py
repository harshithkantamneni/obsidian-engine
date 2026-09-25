import os
import sys
import json
import re
import time
import shutil
import subprocess
import hashlib
from pathlib import Path
from datetime import datetime

from core.paths import MEDIA_DIR, ASSETS_DIR, CHUNKS_DIR, REMOTION_SRC, REMOTION_PUBLIC, OUTPUT_DIR, BASE_DIR
from core.log import get_logger
from pipeline.helpers import clean_script
from pipeline.render import validate_video_ffprobe

logger = get_logger(__name__)


def run_short_audio(short_script_data):
    try:
        from mutagen.mp3 import MP3
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "mutagen"], check=True)
        from mutagen.mp3 import MP3

    # TTS goes through the configured provider (providers.tts in obsidian.yaml)
    from providers.registry import get_provider
    from pipeline.audio import synthesize_chunk
    tts = get_provider("tts")
    from core.pipeline_config import AUDIO_CHUNK_MAX_CHARS
    MAX_CHARS = AUDIO_CHUNK_MAX_CHARS

    # Voice presets — build from overrides (never mutate originals, Fix 144)
    from core.param_overrides import get_override
    VOICE_BODY = {"stability": get_override("short.voice_stability", 0.38),
                  "similarity_boost": get_override("short.similarity_boost", 0.82),
                  "style": get_override("short.voice_style", 0.60),
                  "use_speaker_boost": True}
    VOICE_HOOK = {"stability": get_override("short.hook_stability", 0.28),
                  "similarity_boost": get_override("short.hook_similarity_boost", 0.85),
                  "style": get_override("short.hook_style", 0.75),
                  "use_speaker_boost": True}
    VOICE_SPEED = get_override("short.voice_speed", 0.88)
    HOOK_SPEED = get_override("short.hook_speed", 0.92)

    full_script = clean_script(short_script_data.get("full_script", ""))
    logger.info(f"[Short Audio] Script: {len(full_script)} chars, {len(full_script.split())} words")

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

    SHORT_CHUNKS_DIR = CHUNKS_DIR / "short"
    SHORT_CHUNKS_DIR.mkdir(parents=True, exist_ok=True)

    # Clear stale chunks from previous runs
    for stale in SHORT_CHUNKS_DIR.glob("chunk_*.mp3"):
        stale.unlink(missing_ok=True)
    for stale in SHORT_CHUNKS_DIR.glob("chunk_*_ts.json"):
        stale.unlink(missing_ok=True)

    chunks = split_chunks(full_script)
    logger.info(f"[Short Audio] {len(chunks)} chunk(s)")

    # Param hash for chunk cache invalidation — new params = new filenames (Fix 145)
    _param_sig = json.dumps({"body": VOICE_BODY, "hook": VOICE_HOOK,
                              "speed": VOICE_SPEED, "hook_speed": HOOK_SPEED},
                             sort_keys=True)
    _param_hash = hashlib.md5(_param_sig.encode()).hexdigest()[:8]

    all_words, time_offset = [], 0.0

    for i, chunk in enumerate(chunks):
        chunk_path = SHORT_CHUNKS_DIR / f"chunk_{i:02d}_{_param_hash}.mp3"
        chunk_ts   = SHORT_CHUNKS_DIR / f"chunk_{i:02d}_{_param_hash}_ts.json"

        if chunk_path.exists() and chunk_ts.exists():
            logger.info(f"[Short Audio] Chunk {i+1}/{len(chunks)}: cached")
            with open(chunk_ts) as f:
                chunk_words = json.load(f)
        else:
            # First chunk = hook — more dramatic delivery
            vs  = VOICE_HOOK if i == 0 else VOICE_BODY
            spd = HOOK_SPEED if i == 0 else VOICE_SPEED  # hook slightly faster for punch
            logger.info(f"[Short Audio] Chunk {i+1}/{len(chunks)}: generating ({len(chunk)} chars)...")
            chunk_words = synthesize_chunk(tts, chunk, chunk_path, voice_settings=vs,
                                           speed=spd, role="narrator")

            with open(chunk_ts, "w") as f:
                json.dump(chunk_words, f)
            time.sleep(0.5)

        actual_duration = MP3(chunk_path).info.length
        logger.info(f"  {len(chunk_words)} words, {actual_duration:.2f}s, offset={time_offset:.2f}s")

        for w in chunk_words:
            all_words.append({"word": w["word"],
                "start": round(w["start"] + time_offset, 3),
                "end":   round(w["end"]   + time_offset, 3)})

        time_offset += actual_duration

    # Concat all chunks with ffmpeg — only current param hash (avoid stale mixed audio)
    raw_audio_path = str(MEDIA_DIR / "short_narration_raw.mp3")
    audio_path = str(MEDIA_DIR / "short_narration.mp3")
    chunk_files = sorted(SHORT_CHUNKS_DIR.glob(f"chunk_*_{_param_hash}.mp3"))
    if not chunk_files:
        raise ValueError("[Short Audio] No audio chunks generated — script may be empty or TTS failed for all chunks")
    elif len(chunk_files) == 1:
        shutil.copy2(chunk_files[0], raw_audio_path)
    else:
        concat_list = SHORT_CHUNKS_DIR / "concat_list.txt"
        with open(concat_list, "w") as f:
            for cf in chunk_files:
                f.write(f"file '{cf}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", str(raw_audio_path)],
            check=True, capture_output=True
        )
        concat_list.unlink(missing_ok=True)

    # Audio mastering: loudness normalization to YouTube standard (LUFS -14)
    try:
        logger.info("[Short Audio] Mastering: loudness normalization (LUFS -14)...")
        subprocess.run(
            ["ffmpeg", "-y", "-i", raw_audio_path,
             "-af", "loudnorm=I=-14:LRA=11:TP=-1.5",
             "-ar", "44100", "-b:a", "192k",
             str(audio_path)],
            check=True, capture_output=True
        )
        Path(raw_audio_path).unlink(missing_ok=True)
        logger.info("[Short Audio] ✓ Mastered to LUFS -14")
    except Exception as e:
        logger.warning(f"[Short Audio] Mastering failed ({e}), using raw audio")
        shutil.copy2(raw_audio_path, audio_path)

    # ── Hard-cap audio file at 58s (YouTube Shorts 60s limit - 2s buffer) ──
    # The metadata truncation in run_short_convert only trims JSON, not the
    # actual audio file — Remotion still renders the full-length audio.
    _SHORT_AUDIO_MAX = 58.0
    try:
        from mutagen.mp3 import MP3
        _actual_len = MP3(audio_path).info.length
        if _actual_len > _SHORT_AUDIO_MAX:
            logger.warning(f"[Short Audio] Audio {_actual_len:.1f}s exceeds {_SHORT_AUDIO_MAX}s — truncating with ffmpeg")
            _trunc_tmp = str(MEDIA_DIR / "short_narration_truncated.mp3")
            subprocess.run(
                ["ffmpeg", "-y", "-i", audio_path, "-t", str(_SHORT_AUDIO_MAX),
                 "-acodec", "copy", _trunc_tmp],
                check=True, capture_output=True,
            )
            shutil.move(_trunc_tmp, audio_path)
            # Trim word timestamps that fall beyond the cap
            all_words = [w for w in all_words if w["start"] < _SHORT_AUDIO_MAX]
            time_offset = _SHORT_AUDIO_MAX
            logger.info(f"[Short Audio] Truncated audio to {_SHORT_AUDIO_MAX}s")
    except Exception as trunc_err:
        logger.warning(f"[Short Audio] Audio truncation failed ({trunc_err}), proceeding with original")

    ts_path = str(MEDIA_DIR / "short_timestamps.json")
    with open(ts_path, "w") as f:
        json.dump({"words": all_words}, f, indent=2)

    tail_buffer = get_override("short.tail_buffer_sec", 1.5)
    total_duration = (all_words[-1]["end"] if all_words else time_offset) + tail_buffer
    logger.info(f"[Short Audio] ✓ {len(all_words)} words, {total_duration:.1f}s")

    return {"audio_path": audio_path, "timestamps_path": ts_path,
            "total_duration_seconds": total_duration, "word_count": len(full_script.split()),
            "production_params": {
                "voice_speed": VOICE_SPEED, "hook_speed": HOOK_SPEED,
                "voice_stability": VOICE_BODY["stability"],
                "voice_style": VOICE_BODY["style"],
                "similarity_boost": VOICE_BODY["similarity_boost"],
                "hook_stability": VOICE_HOOK["stability"],
                "hook_style": VOICE_HOOK["style"],
                "hook_similarity_boost": VOICE_HOOK["similarity_boost"],
                "tail_buffer_sec": tail_buffer,
            }}


# ── Short pipeline: Images (portrait 9:16) ─────────────────────────────────────
def run_short_images(short_storyboard_data):
    # Images go through the configured provider (providers.images in obsidian.yaml)
    from providers.registry import get_provider, get_provider_name
    from pipeline.images import _place_image
    if get_provider_name("images") == "fal":
        from providers.images.fal import FalProvider
        if not FalProvider.has_credentials():
            logger.warning("[Short Images] WARNING: FAL_API_KEY not set — skipping")
            return short_storyboard_data
    image_provider = get_provider("images")

    scenes = short_storyboard_data.get("scenes", [])

    IMAGE_MODEL = os.getenv("IMAGE_MODEL", "flux").lower()

    STYLE_FLUX = ("oil painting style, dramatic cinematic lighting, dark atmospheric shadows, "
                  "rich Renaissance color palette, high detail, PORTRAIT vertical 9:16 composition, "
                  "documentary historical illustration, no text, no watermarks, no borders")
    STYLE_RECRAFT = ("dramatic cinematic lighting, dark atmospheric shadows, "
                     "rich Renaissance color palette, PORTRAIT vertical 9:16 composition, "
                     "documentary historical illustration, no text, no watermarks")

    MOOD_LIGHT = {
        "tense":    "sinister torch light, blood-red shadows",
        "dramatic": "golden candlelight, high contrast chiaroscuro",
        "dark":     "deep shadows, single light source, mysterious",
        "cold":     "cold moonlight, blue-grey tones, desolate",
        "reverent": "warm amber light, solemn sacred atmosphere",
        "wonder":   "vast golden hour light, awe-inspiring scale",
        "warmth":   "soft warm firelight, intimate amber tones",
        "absurdity":"bright surreal lighting, vivid dreamlike tones",
    }

    logger.info(f"[Short Images] Using model: {IMAGE_MODEL}")

    for i, scene in enumerate(scenes):
        img_path = ASSETS_DIR / f"short_scene_{i:02d}_ai.jpg"
        if img_path.exists():
            logger.info(f"[Short Images] Scene {i+1}/{len(scenes)}: cached")
            scene["ai_image"] = str(img_path)
            continue

        prompt = scene.get("image_prompt", "")
        mood   = scene.get("mood", "dark")
        style  = STYLE_RECRAFT if IMAGE_MODEL == "recraft" else STYLE_FLUX
        # Reinforce vertical composition in the prompt
        prompt = f"VERTICAL PORTRAIT COMPOSITION, subject centred upper frame, dark lower half. {prompt}, {MOOD_LIGHT.get(mood, 'deep shadows')}, {style}"

        logger.info(f"[Short Images] Scene {i+1}/{len(scenes)} ({mood}): generating...")
        try:
            # Portrait 9:16 (fal: Recraft 1440x2560, or Flux Ultra aspect_ratio 9:16)
            out = image_provider.generate(prompt, style=IMAGE_MODEL, width=1440, height=2560)
            _place_image(out, img_path)
            logger.info(f"  ✓ {img_path.name} ({img_path.stat().st_size // 1024}KB)")
            scene["ai_image"] = str(img_path)
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"  ✗ {image_provider.name} failed for short image: {e}")
            # Fallback: try to reuse a long-form AI image if available
            fallback_long = ASSETS_DIR / f"scene_{i:03d}_ai.jpg"
            if fallback_long.exists():
                scene["ai_image"] = str(fallback_long)
                logger.info(f"  ↳ Using long-form fallback: {fallback_long.name}")
            else:
                scene["ai_image"] = None

    short_storyboard_data["scenes"] = scenes
    generated = sum(1 for s in scenes if s.get("ai_image"))
    logger.info(f"[Short Images] ✓ {generated}/{len(scenes)} images generated")
    return short_storyboard_data


# ── Short pipeline: Convert to Remotion ───────────────────────────────────────
def run_short_convert(short_storyboard_data, short_audio_data):
    from pipeline.convert import align_scenes_to_words

    ts_path = MEDIA_DIR / "short_timestamps.json"
    if ts_path.exists():
        with open(ts_path) as f:
            ts = json.load(f)
        words = ts.get("words", [])
    else:
        logger.warning("[Short Convert] ⚠️  short_timestamps.json not found — scenes will use even spacing")
        words = []

    total_duration = short_audio_data.get("total_duration_seconds") if isinstance(short_audio_data, dict) else None
    if not total_duration:
        raise ValueError("[Short Convert] short_audio_data missing total_duration_seconds")
    scenes = short_storyboard_data.get("scenes", [])
    n              = len(scenes)

    if n == 0:
        raise ValueError("[Short Convert] No scenes in storyboard — cannot build short video data")

    # Build scene timings aligned to actual word timestamps
    timings = align_scenes_to_words(n, words, total_duration)
    remotion_scenes = []
    for i, scene in enumerate(scenes):
        start_time, end_time = timings[i]

        # Copy portrait AI image to remotion/public/ (always overwrite)
        ai_image_name = None
        ai_src = scene.get("ai_image")
        if ai_src:
            src = Path(ai_src)
            if src.exists():
                dest = REMOTION_PUBLIC / src.name
                shutil.copy2(src, dest)
                ai_image_name = src.name

        remotion_scenes.append({
            "narration_segment": scene.get("narration_segment", ""),
            "start_time":        round(start_time, 3),
            "end_time":          round(end_time,   3),
            "mood":              scene.get("mood", "dark"),
            "ai_image":          ai_image_name,
        })

    if remotion_scenes:
        remotion_scenes[-1]["end_time"] = total_duration

    # Select background music — Epidemic API first, then local manager, then hardcoded fallback
    short_music_file = None
    try:
        from media import epidemic_music_manager
        epi_result = epidemic_music_manager.search_for_video(remotion_scenes, total_duration)
        if epi_result and epi_result.get("music_file"):
            short_music_file = epi_result["music_file"]
            logger.info(f"[Short Convert] Background music (Epidemic API): {short_music_file}")
    except Exception as _epi_err:
        logger.debug(f"[Short Convert] Epidemic music unavailable: {_epi_err}")

    if not short_music_file:
        try:
            from media import music_manager
            short_music_file = music_manager.get_music_for_video(remotion_scenes, total_duration)
            if short_music_file:
                logger.info(f"[Short Convert] Background music (local): {short_music_file}")
        except Exception as _music_err:
            logger.warning(f"[Short Convert] Music manager unavailable: {_music_err}")

    if not short_music_file:
        mood_counts = {}
        for s in remotion_scenes:
            m = s.get("mood", "dark")
            mood_counts[m] = mood_counts.get(m, 0) + 1
        dominant_mood = max(mood_counts, key=mood_counts.get) if mood_counts else "dark"
        MOOD_MUSIC = {
            "dark":      "music/dark_01_scp_x1x.mp3",
            "tense":     "music/tense_01_stay_the_course.mp3",
            "dramatic":  "music/dramatic_01_strength_of_titans.mp3",
            "cold":      "music/cold_01_scp_x5x.mp3",
            "reverent":  "music/reverent_01_ancient_rite.mp3",
            "wonder":    "music/wonder_01_the_descent.mp3",
            "warmth":    "music/warmth_01_hearth_and_home.mp3",
            "absurdity": "music/absurdity_01_scheming_weasel.mp3",
        }
        local = MOOD_MUSIC.get(dominant_mood, MOOD_MUSIC["dark"])
        if (REMOTION_PUBLIC / local).exists():
            short_music_file = local
            logger.info(f"[Short Convert] Background music (local): {short_music_file}")
        else:
            logger.warning("[Short Convert] No background music found")

    # ── Validate word timestamps (defense in depth) ──
    _valid_words = []
    for w in words:
        word_text = (w.get("word", "") or "").strip()
        if not word_text:
            continue
        _ws = w.get("start", 0)
        _we = w.get("end", 0)
        if _we <= _ws:
            _we = _ws + 0.1
        if _ws < 0:
            _ws = 0
        _valid_words.append({"word": word_text, "start": _ws, "end": _we})
    if len(_valid_words) < len(words):
        logger.warning(f"[Short Convert] Cleaned {len(words) - len(_valid_words)} invalid word timestamps")
    words = _valid_words

    # ── Hook breathing room — delay narration to match BREATH_FRAMES ──
    # ShortVideo.tsx delays audio playback by BREATH_FRAMES=45 frames (1.5s at 30fps).
    # Word timestamps must be shifted by the same amount so captions stay in sync.
    _HOOK_BUFFER = 1.5  # 45 frames / 30 fps
    for w in words:
        w["start"] = w["start"] + _HOOK_BUFFER
        w["end"] = w["end"] + _HOOK_BUFFER
    total_duration += _HOOK_BUFFER

    # ── Hard cap Short duration at 58s (60s YouTube limit - 2s buffer) ──
    _SHORT_MAX_DURATION = 58.0
    if total_duration > _SHORT_MAX_DURATION:
        logger.warning(f"[Short Convert] Duration {total_duration:.1f}s exceeds {_SHORT_MAX_DURATION}s cap — truncating")
        words = [w for w in words if w.get("start", 0) < _SHORT_MAX_DURATION]
        for s in remotion_scenes:
            if s.get("end_time", 0) > _SHORT_MAX_DURATION:
                s["end_time"] = _SHORT_MAX_DURATION
        remotion_scenes = [s for s in remotion_scenes if s.get("start_time", 0) < _SHORT_MAX_DURATION]
        total_duration = _SHORT_MAX_DURATION

    short_data = {
        "total_duration_seconds": total_duration,
        "scenes":                 remotion_scenes,
        "word_timestamps":        words,
        "music_file":             short_music_file,
    }

    # Inject audio_config for Remotion shorts ducking (reads from overrides if set)
    try:
        from core.param_overrides import get_override
        short_data["audio_config"] = {
            "ducking": {
                "speechVolume": get_override("short.music_speech_vol", 0.12),
                "silenceVolume": get_override("short.music_silent_vol", 0.25),
                "rampSeconds": 0.3,
            },
        }
    except Exception:
        pass  # Non-fatal

    out_path = REMOTION_SRC / "short-video-data.json"
    with open(out_path, "w") as f:
        json.dump(short_data, f, indent=2)

    # Copy audio to remotion/public/
    shutil.copy2(MEDIA_DIR / "short_narration.mp3", REMOTION_PUBLIC / "short_narration.mp3")

    images_with_data = sum(1 for s in remotion_scenes if s.get("ai_image"))
    logger.info(f"[Short Convert] ✓ {len(remotion_scenes)} scenes, {len(words)} words, {images_with_data} images")
    logger.info(f"[Short Convert] ✓ Duration: {total_duration:.1f}s")
    return short_data


# ── Short pipeline: Render ─────────────────────────────────────────────────────
def run_short_render(topic):
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r'[^a-z0-9]+', '_', topic.lower())[:40]
    output = OUTPUT_DIR / f"{ts}_{slug}_SHORT.mp4"

    # Clean stale short renders
    for stale in OUTPUT_DIR.glob("*_SHORT.mp4"):
        try:
            if stale != output:
                stale.unlink()
                logger.info(f"[Short Render] Removed stale file: {stale.name}")
        except Exception:
            pass

    # Ensure video-data.json exists — it's gitignored so absent on fresh Railway deploy.
    # ObsidianVideo.tsx imports it at Webpack compile time; without it the render fails.
    vd_path = REMOTION_SRC / "video-data.json"
    if not vd_path.exists():
        vd_path.write_text(json.dumps({
            "total_duration_seconds": 0,
            "scenes": [],
            "word_timestamps": [],
            "music_file": None,
            "showEndscreen": True,
            "endscreen_recommended": None,
        }))
        logger.info("[Short Render] Created stub video-data.json for Webpack compilation")

    # Validate required assets exist before rendering
    short_audio = REMOTION_PUBLIC / "short_narration.mp3"
    if not short_audio.exists():
        raise FileNotFoundError(f"[Short Render] Missing {short_audio} — short audio stage must complete first")
    svd = REMOTION_SRC / "short-video-data.json"
    if not svd.exists():
        raise FileNotFoundError(f"[Short Render] Missing {svd} — short convert stage must complete first")

    logger.info(f"[Short Render] Rendering 1080x1920 Short to {output.name}...")

    proc = subprocess.Popen(
        ["npx", "remotion", "render", "ObsidianShort", str(output),
         "--concurrency=4", "--gl=swangle", "--codec=h264",
         "--crf=10",
         "--enable-multiprocess-on-linux"],
        cwd=BASE_DIR / "remotion",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1
    )
    last_progress = ""
    error_lines = []
    for line in proc.stdout:
        line = line.rstrip()
        m = re.search(r'(?:Frame|Rendered)[\s:]*(\d+)/(\d+)', line)
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            pct = int(cur / total * 100) if total > 0 else 0
            progress = f"[Short Render] Frame {cur}/{total} ({pct}%)"
            if progress != last_progress:
                logger.info(progress)
                last_progress = progress
        elif line.strip():
            logger.info(f"[Short Render] {line}")
            if any(w in line.lower() for w in ('error', 'failed', 'fatal', 'exception', 'cannot', 'unable', 'chromium', 'chrome', 'browser')):
                error_lines.append(line)
    proc.wait()
    if proc.returncode != 0:
        detail = "\n".join(error_lines[-10:]) if error_lines else "no error detail captured"
        raise Exception(f"Short Remotion render failed:\n{detail}")

    size_mb = output.stat().st_size / 1024 / 1024
    logger.info(f"[Short Render] ✓ {output.name} ({size_mb:.1f}MB)")

    # Read expected duration from short-video-data.json for validation
    svd_check = REMOTION_SRC / "short-video-data.json"
    expected_dur = 0
    if svd_check.exists():
        try:
            expected_dur = json.loads(svd_check.read_text()).get("total_duration_seconds", 0)
        except Exception:
            pass
    ok, info = validate_video_ffprobe(str(output), expected_duration=expected_dur, min_bitrate=500)
    if not ok:
        raise Exception(f"[Short Render] Video validation FAILED: {info} — not uploading corrupted video")

    return str(output)
