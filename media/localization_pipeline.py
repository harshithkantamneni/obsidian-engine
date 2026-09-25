#!/usr/bin/env python3
"""
Localization Pipeline — Multi-language dubbing and subtitle generation.
Runs AFTER the main pipeline produces an English video.
Translates script, dubs audio, generates subtitles, assembles localized videos, and uploads.
"""

import sys
import os
import json
import requests
import time
import subprocess
import re
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.append(str(_BASE))

from dotenv import load_dotenv
load_dotenv(dotenv_path=_BASE / ".env", interpolate=False)

from core.agent_wrapper import call_agent

# ── Config ────────────────────────────────────────────────────────────────────

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
try:
    from core.pipeline_config import NARRATOR_VOICE_ID
    VOICE_ID = NARRATOR_VOICE_ID
except Exception:
    VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # George (fallback)
MAX_CHARS = 500  # chunk size at sentence boundaries

SUPPORTED_LANGUAGES = {
    "es": {"name": "Spanish",    "badge": "ES", "elevenlabs_model": "eleven_multilingual_v2"},
    "pt": {"name": "Portuguese", "badge": "PT", "elevenlabs_model": "eleven_multilingual_v2"},
    "hi": {"name": "Hindi",      "badge": "HI", "elevenlabs_model": "eleven_multilingual_v2"},
    "fr": {"name": "French",     "badge": "FR", "elevenlabs_model": "eleven_multilingual_v2"},
    "de": {"name": "German",     "badge": "DE", "elevenlabs_model": "eleven_multilingual_v2"},
}

DEFAULT_LANGUAGES = ["es", "pt", "hi"]


# ── Subtitle Generation ──────────────────────────────────────────────────────

def generate_srt(word_timestamps: list[dict], output_path: str) -> str:
    """
    Convert word-level timestamps into a proper SRT subtitle file.
    Groups words into 2-line segments of ~8-10 words each.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not word_timestamps:
        print("[Localization] No word timestamps provided, skipping SRT generation")
        return str(output_path)

    # Group words into subtitle segments of ~8-10 words
    segments = []
    current_words = []
    for wt in word_timestamps:
        current_words.append(wt)
        if len(current_words) >= 9:
            segments.append(current_words)
            current_words = []
    if current_words:
        segments.append(current_words)

    # Write SRT
    lines = []
    for idx, segment in enumerate(segments, start=1):
        start_time = segment[0]["start"]
        end_time = segment[-1]["end"]

        # Handle timing gaps — ensure end >= start
        if end_time <= start_time:
            end_time = start_time + 2.0

        start_srt = _seconds_to_srt_time(start_time)
        end_srt = _seconds_to_srt_time(end_time)

        # Split words into two lines for readability
        words = [w["word"] for w in segment]
        mid = len(words) // 2
        line1 = " ".join(words[:mid]) if mid > 0 else " ".join(words)
        line2 = " ".join(words[mid:]) if mid > 0 else ""

        text = line1
        if line2:
            text += "\n" + line2

        lines.append(f"{idx}")
        lines.append(f"{start_srt} --> {end_srt}")
        lines.append(text)
        lines.append("")  # blank line between entries

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[Localization] Generated SRT with {len(segments)} segments → {output_path.name}")
    return str(output_path)


def _seconds_to_srt_time(seconds: float) -> str:
    """Convert seconds to SRT timestamp format: HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def translate_srt(srt_path: str, target_lang: str) -> str:
    """
    Translate SRT content while preserving timing codes.
    Uses Claude Haiku for fast, cost-effective translation.
    """
    srt_path = Path(srt_path)
    if not srt_path.exists():
        raise FileNotFoundError(f"SRT file not found: {srt_path}")

    lang_info = SUPPORTED_LANGUAGES.get(target_lang)
    if not lang_info:
        raise ValueError(f"Unsupported language: {target_lang}")

    srt_content = srt_path.read_text(encoding="utf-8")

    system_prompt = (
        f"You are a professional subtitle translator. Translate the following SRT subtitle "
        f"content from English to {lang_info['name']}. "
        f"CRITICAL RULES:\n"
        f"1. Preserve ALL timing codes exactly as they are (e.g., 00:01:23,456 --> 00:01:27,890)\n"
        f"2. Preserve ALL sequence numbers exactly as they are\n"
        f"3. Only translate the text lines between timing codes\n"
        f"4. Maintain the same line breaks within each subtitle block\n"
        f"5. Keep proper names and place names in their original form\n"
        f"6. Return ONLY the translated SRT content, nothing else"
    )

    translated = call_agent(
        "localization_pipeline",
        system_prompt=system_prompt,
        user_prompt=srt_content,
        max_tokens=8000,
        expect_json=False,
        effort_offset=-1,  # Haiku for SRT translation
    )

    output_path = srt_path.parent / f"{srt_path.stem}_{target_lang}.srt"
    output_path.write_text(translated.strip(), encoding="utf-8")
    print(f"[Localization] Translated SRT → {lang_info['name']}: {output_path.name}")
    return str(output_path)


# ── Script Translation ────────────────────────────────────────────────────────

def translate_script(script_text: str, target_lang: str) -> str:
    """
    Translate the full narration script using Claude Sonnet.
    Preserves dramatic documentary tone and style.
    """
    lang_info = SUPPORTED_LANGUAGES.get(target_lang)
    if not lang_info:
        raise ValueError(f"Unsupported language: {target_lang}")

    system_prompt = (
        f"You are an expert literary translator specializing in documentary narration. "
        f"Translate the following English documentary script into {lang_info['name']}.\n\n"
        f"TRANSLATION GUIDELINES:\n"
        f"1. Maintain the dramatic, cinematic documentary tone throughout\n"
        f"2. Preserve present-tense narration style — do NOT shift to past tense\n"
        f"3. Keep named characters and place names in their original language\n"
        f"4. Adapt idioms and cultural references naturally — do not translate literally\n"
        f"5. Preserve paragraph breaks and structural formatting\n"
        f"6. Match the intensity and pacing of the original\n"
        f"7. Return ONLY the translated script text, no commentary or notes"
    )

    translated = call_agent(
        "localization_pipeline",
        system_prompt=system_prompt,
        user_prompt=script_text,
        max_tokens=16000,
        expect_json=False,
    )

    print(f"[Localization] Translated script → {lang_info['name']} ({len(translated)} chars)")
    return translated.strip()


# ── Audio Dubbing ─────────────────────────────────────────────────────────────

def _split_into_chunks(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """Split text at sentence boundaries, max_chars per chunk."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = sentence
        else:
            current = (current + " " + sentence).strip() if current else sentence
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _generate_tts_chunk(text: str, target_lang: str, chunk_idx: int) -> bytes:
    """Call ElevenLabs multilingual v2 API for a single chunk. Returns raw audio bytes."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.38,
            "similarity_boost": 0.82,
            "style": 0.60,
            "use_speaker_boost": True,
        },
    }

    last_err = None
    for attempt in range(5):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=120)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as ce:
            wait = 30 * (attempt + 1)
            print(f"  [Localization] Connection error on chunk {chunk_idx}, waiting {wait}s...")
            time.sleep(wait)
            last_err = ce
            continue

        if response.status_code == 200:
            return response.content
        elif response.status_code == 429:
            wait = 60 * (attempt + 1)
            print(f"  [Localization] Rate limited on chunk {chunk_idx}, waiting {wait}s (attempt {attempt+1}/5)...")
            time.sleep(wait)
            last_err = Exception("ElevenLabs rate limit (429)")
        elif response.status_code in (500, 502, 503):
            wait = 30 * (attempt + 1)
            print(f"  [Localization] Server error ({response.status_code}) on chunk {chunk_idx}, waiting {wait}s...")
            time.sleep(wait)
            last_err = Exception(f"ElevenLabs server error ({response.status_code})")
        else:
            raise Exception(f"ElevenLabs error {response.status_code}: {response.text[:200]}")

    raise last_err or Exception(f"Failed TTS chunk {chunk_idx} after 5 attempts")


def dub_audio(script_text: str, target_lang: str, output_path: str) -> str:
    """
    Translate script and generate dubbed audio using ElevenLabs multilingual v2.
    Applies ffmpeg loudnorm mastering to match the main pipeline.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lang_info = SUPPORTED_LANGUAGES.get(target_lang)
    if not lang_info:
        raise ValueError(f"Unsupported language: {target_lang}")

    # Step 1: Translate the script
    print(f"[Localization] Translating script to {lang_info['name']}...")
    translated_script = translate_script(script_text, target_lang)

    # Step 2: Split into chunks and generate TTS
    chunks = _split_into_chunks(translated_script)
    print(f"[Localization] Generating {lang_info['name']} audio: {len(chunks)} chunks")

    chunks_dir = output_path.parent / f"dub_chunks_{target_lang}"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    for i, chunk in enumerate(chunks):
        print(f"[Localization] TTS chunk {i+1}/{len(chunks)} ({len(chunk)} chars)...")
        audio_bytes = _generate_tts_chunk(chunk, target_lang, i)
        chunk_path = chunks_dir / f"chunk_{i:03d}.mp3"
        chunk_path.write_bytes(audio_bytes)
        time.sleep(0.5)  # gentle rate limiting

    # Step 3: Concatenate chunks with ffmpeg
    chunk_files = sorted(chunks_dir.glob("chunk_*.mp3"))
    raw_path = output_path.parent / f"dub_raw_{target_lang}.mp3"

    if len(chunk_files) == 1:
        import shutil
        shutil.copy2(chunk_files[0], str(raw_path))
    else:
        concat_list = chunks_dir / "concat_list.txt"
        with open(concat_list, "w") as f:
            for cf in chunk_files:
                f.write(f"file '{cf}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", str(raw_path)],
            capture_output=True, check=True,
        )

    # Step 4: Apply loudnorm mastering (I=-14, LRA=11, TP=-1.5)
    print("[Localization] Applying loudnorm mastering...")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(raw_path),
         "-af", "loudnorm=I=-14:LRA=11:TP=-1.5",
         "-ar", "44100", "-ab", "192k",
         str(output_path)],
        capture_output=True, check=True,
    )

    # Cleanup raw file
    raw_path.unlink(missing_ok=True)

    duration_probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(output_path)],
        capture_output=True, text=True,
    )
    duration = float(duration_probe.stdout.strip()) if duration_probe.stdout.strip() else 0
    print(f"[Localization] Dubbed audio ({lang_info['name']}): {duration:.1f}s → {output_path.name}")
    return str(output_path)


# ── Video Assembly ────────────────────────────────────────────────────────────

def create_localized_video(
    original_video: str,
    dubbed_audio: str,
    subtitles_srt: str,
    target_lang: str,
    output_path: str,
) -> str:
    """
    Assemble localized video: replace audio track with dubbed version,
    burn subtitles into video, output as MP4 with language code suffix.
    """
    original_video = Path(original_video)
    dubbed_audio = Path(dubbed_audio)
    subtitles_srt = Path(subtitles_srt)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not original_video.exists():
        raise FileNotFoundError(f"Original video not found: {original_video}")
    if not dubbed_audio.exists():
        raise FileNotFoundError(f"Dubbed audio not found: {dubbed_audio}")

    lang_info = SUPPORTED_LANGUAGES.get(target_lang, {})

    # Build ffmpeg command
    cmd = [
        "ffmpeg", "-y",
        "-i", str(original_video),
        "-i", str(dubbed_audio),
        "-map", "0:v:0",    # video from original
        "-map", "1:a:0",    # audio from dubbed version
        "-c:v", "copy",     # no re-encode of video
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
    ]

    # Burn subtitles if SRT exists
    if subtitles_srt.exists():
        # Need to re-encode video to burn subtitles
        srt_escaped = str(subtitles_srt).replace(":", "\\:").replace("'", "\\'")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(original_video),
            "-i", str(dubbed_audio),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-vf", f"subtitles='{srt_escaped}':force_style='FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2'",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(output_path),
        ]
    else:
        cmd.append(str(output_path))

    print(f"[Localization] Assembling {lang_info.get('name', target_lang)} video...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[Localization] ffmpeg stderr: {result.stderr[-500:]}")
        raise RuntimeError(f"ffmpeg failed assembling {target_lang} video (exit {result.returncode})")

    size_mb = output_path.stat().st_size / (1024 * 1024) if output_path.exists() else 0
    print(f"[Localization] Assembled video ({lang_info.get('name', target_lang)}): {size_mb:.1f}MB → {output_path.name}")
    return str(output_path)


# ── Upload ────────────────────────────────────────────────────────────────────

def upload_localized(
    video_path: str,
    original_seo: dict,
    target_lang: str,
    video_id_original: str,
) -> dict:
    """
    Upload localized video to YouTube with translated metadata.
    Links back to the original English video in the description.
    """
    from importlib import import_module
    uploader = import_module("11_youtube_uploader")

    lang_info = SUPPORTED_LANGUAGES.get(target_lang)
    if not lang_info:
        raise ValueError(f"Unsupported language: {target_lang}")

    original_title = original_seo.get("title", "The Obsidian Archive")
    original_desc = original_seo.get("description", "")
    original_tags = original_seo.get("tags", [])

    # Translate title and description with Claude Haiku
    system_prompt = (
        f"Translate the following YouTube video metadata from English to {lang_info['name']}. "
        f"Return valid JSON with keys: \"title\", \"description\", \"tags\".\n"
        f"Rules:\n"
        f"- Keep the channel name 'The Obsidian Archive' untranslated\n"
        f"- Translate tags into {lang_info['name']} equivalents\n"
        f"- Maintain dramatic documentary tone\n"
        f"- Keep hashtags functional"
    )

    meta_input = json.dumps({
        "title": original_title,
        "description": original_desc,
        "tags": original_tags[:15],
    })

    translated_meta = call_agent(
        "localization_pipeline",
        system_prompt=system_prompt,
        user_prompt=meta_input,
        max_tokens=4000,
        effort_offset=-1,  # Haiku for metadata translation
    )

    # Build final metadata
    badge = lang_info["badge"]
    translated_title = f"[{badge}] {translated_meta.get('title', original_title)}"
    original_url = f"https://youtu.be/{video_id_original}" if video_id_original else ""

    link_back = f"\n\n🎬 Original (English): {original_url}" if original_url else ""
    translated_desc = translated_meta.get("description", original_desc) + link_back
    translated_tags = translated_meta.get("tags", original_tags)

    print(f"[Localization] Uploading {lang_info['name']} video: {translated_title}")

    result = uploader.upload_video(
        video_path=str(video_path),
        title=translated_title,
        description=translated_desc,
        tags=translated_tags,
        privacy="private",
    )

    return result


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run(topic: str, state: dict, target_languages: list[str] = None) -> dict:
    """
    Main entry point for the localization pipeline.
    Runs after the main pipeline produces an English video.
    """
    # Check opt-in
    if os.getenv("LOCALIZATION_ENABLED", "").lower() != "true":
        print("[Localization] Disabled (set LOCALIZATION_ENABLED=true to enable)")
        return {}

    # Determine target languages
    env_langs = os.getenv("LOCALIZATION_LANGUAGES", "")
    if target_languages:
        languages = target_languages
    elif env_langs:
        languages = [lang.strip() for lang in env_langs.split(",") if lang.strip()]
    else:
        languages = DEFAULT_LANGUAGES

    # Validate languages
    languages = [lang for lang in languages if lang in SUPPORTED_LANGUAGES]
    if not languages:
        print("[Localization] No valid target languages specified")
        return {}

    print(f"[Localization] Starting for: {', '.join(SUPPORTED_LANGUAGES[lang]['name'] for lang in languages)}")

    # Load pipeline state data
    script_text = state.get("stage_4", {}).get("full_script", "")
    if not script_text:
        print("[Localization] No script text found in state (stage_4.full_script)")
        return {}

    # Word timestamps — try stage_8 first, then look in chunk-level data
    word_timestamps = state.get("stage_8", {}).get("word_timestamps", [])
    if not word_timestamps:
        # Try to find in chunk-level data
        chunks_data = state.get("stage_8", {}).get("chunks", [])
        for chunk in chunks_data:
            chunk_words = chunk.get("word_timestamps", [])
            word_timestamps.extend(chunk_words)

    seo_data = state.get("stage_6", {})
    video_id_original = state.get("stage_11", {}).get("video_id", "")

    # Find original FINAL video
    final_videos = sorted((_BASE / "outputs").glob("*_FINAL.mp4"))
    if not final_videos:
        print("[Localization] No *_FINAL.mp4 found in outputs/")
        return {}
    original_video = str(final_videos[-1])
    print(f"[Localization] Original video: {Path(original_video).name}")

    # Setup output directory
    loc_dir = _BASE / "outputs" / "localized"
    loc_dir.mkdir(parents=True, exist_ok=True)

    # Generate English SRT first (base for translations)
    english_srt = str(loc_dir / "subtitles_en.srt")
    generate_srt(word_timestamps, english_srt)

    results = {}

    for lang in languages:
        lang_info = SUPPORTED_LANGUAGES[lang]
        print(f"\n[Localization] ── {lang_info['name']} ({lang.upper()}) ──────────────────")
        try:
            # 1. Dub audio (includes script translation)
            dubbed_path = str(loc_dir / f"dubbed_{lang}.mp3")
            dub_audio(script_text, lang, dubbed_path)

            # 2. Translate subtitles
            translated_srt = translate_srt(english_srt, lang)

            # 3. Assemble localized video
            safe_topic = re.sub(r'[^\w\s-]', '', topic)[:50].strip().replace(' ', '_')
            video_out = str(loc_dir / f"{safe_topic}_{lang}_FINAL.mp4")
            create_localized_video(original_video, dubbed_path, translated_srt, lang, video_out)

            # 4. Upload
            upload_result = None
            try:
                upload_result = upload_localized(video_out, seo_data, lang, video_id_original)
            except Exception as ue:
                print(f"[Localization] Upload failed for {lang_info['name']}: {ue}")
                upload_result = {"error": str(ue)}

            results[lang] = {
                "video_path": video_out,
                "dubbed_audio": dubbed_path,
                "subtitles": translated_srt,
                "upload_result": upload_result,
            }
            print(f"[Localization] ✓ {lang_info['name']} complete")

        except Exception as e:
            print(f"[Localization] ✗ {lang_info['name']} FAILED: {e}")
            results[lang] = {"error": str(e)}
            continue  # one language failing shouldn't block others

    # Save results
    results_path = _BASE / "outputs" / "localization_results.json"
    results_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\n[Localization] Results saved → {results_path.name}")
    print(f"[Localization] Done: {sum(1 for r in results.values() if 'error' not in r)}/{len(languages)} languages succeeded")

    return results


# ── CLI Entry Point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Force enable for direct CLI runs
    os.environ.setdefault("LOCALIZATION_ENABLED", "true")

    # Load latest state file
    state_files = sorted((_BASE / "outputs").glob("pipeline_state_*.json"), key=lambda p: p.stat().st_mtime)
    if not state_files:
        print("[Localization] No pipeline state file found in outputs/")
        sys.exit(1)

    latest_state = state_files[-1]
    print(f"[Localization] Loading state: {latest_state.name}")
    state = json.loads(latest_state.read_text(encoding="utf-8"))

    topic = state.get("topic", state.get("stage_0", {}).get("topic", "unknown"))

    # Parse optional language args from CLI
    cli_langs = None
    if len(sys.argv) > 1:
        cli_langs = [lang.strip() for lang in sys.argv[1].split(",")]

    results = run(topic=topic, state=state, target_languages=cli_langs)

    if results:
        for lang, data in results.items():
            name = SUPPORTED_LANGUAGES.get(lang, {}).get("name", lang)
            if "error" in data:
                print(f"  {name}: FAILED — {data['error']}")
            else:
                print(f"  {name}: {Path(data['video_path']).name}")
