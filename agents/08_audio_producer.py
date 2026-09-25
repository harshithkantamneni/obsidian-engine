"""
Agent 08 - Audio Producer
Sends script to ElevenLabs in chunks and merges audio + timestamps.
"""

import sys
import os
import json
import requests
import time
import shutil
import subprocess
from pathlib import Path
_BASE = Path(__file__).resolve().parent.parent
sys.path.append(str(_BASE))
from dotenv import load_dotenv
load_dotenv(dotenv_path=_BASE / ".env", interpolate=False)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
try:
    from core.pipeline_config import NARRATOR_VOICE_ID
    VOICE_ID = NARRATOR_VOICE_ID
except Exception:
    VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # George (fallback)
MAX_CHARS = 4500  # safe under 5000 limit

def split_into_chunks(text, max_chars=MAX_CHARS):
    """Split text at sentence boundaries."""
    sentences = text.replace('\n', ' \n ').split('. ')
    chunks = []
    current = ""
    for sentence in sentences:
        part = sentence + ". "
        if len(current) + len(part) > max_chars and current:
            chunks.append(current.strip())
            current = part
        else:
            current += part
    if current.strip():
        chunks.append(current.strip())
    return chunks

def generate_chunk(text, chunk_idx, voice_settings=None, speed=None):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/with-timestamps"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    if voice_settings is None:
        voice_settings = {
            "stability": 0.42,
            "similarity_boost": 0.85,
            "style": 0.88,
            "use_speaker_boost": True
        }
    payload = {
        "text": text,
        "model_id": "eleven_v3",
        "voice_settings": voice_settings
    }
    if speed is not None:
        payload["speed"] = speed
    last_err = None
    for attempt in range(5):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=120)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as ce:
            wait = 30 * (attempt + 1)
            print(f"  Connection/timeout error, waiting {wait}s...")
            time.sleep(wait)
            last_err = ce
            continue
        if response.status_code == 200:
            return json.loads(response.text, strict=False)
        elif response.status_code == 429:
            wait = 60 * (attempt + 1)
            print(f"  Rate limited, waiting {wait}s (attempt {attempt+1}/5)...")
            time.sleep(wait)
            last_err = Exception("ElevenLabs rate limit (429)")
        elif response.status_code in (500, 502, 503):
            wait = 30 * (attempt + 1)
            print(f"  Server error ({response.status_code}), waiting {wait}s (attempt {attempt+1}/5)...")
            time.sleep(wait)
            last_err = Exception(f"ElevenLabs server error ({response.status_code})")
        else:
            raise Exception(f"ElevenLabs error {response.status_code}: {response.text[:200]}")
    raise last_err or Exception("Failed after 5 attempts")

def _get_prosody_for_chunk(text, chunk_idx, total_chunks):
    """Analyze text content and return appropriate voice_settings + speed."""
    # Default: documentary narration
    settings = {
        "stability": 0.42,
        "similarity_boost": 0.85,
        "style": 0.88,
        "use_speaker_boost": True
    }
    speed = 0.78  # Base: target ~130 WPM

    text_lower = text.lower()

    # Hook (first chunk): more dramatic, slightly faster
    if chunk_idx == 0:
        settings["stability"] = 0.32
        settings["style"] = 0.95
        speed = 0.82
    # Reveal/twist indicators: slow down, more dramatic
    elif any(phrase in text_lower for phrase in [
        "the truth", "what really happened", "no one knew", "the real story",
        "but here's what", "what they found", "the shocking", "in reality",
        "the evidence shows", "it was actually", "the twist",
    ]):
        settings["stability"] = 0.30
        settings["style"] = 0.95
        speed = 0.74  # Slow for dramatic effect
    # Tension building: slightly unstable voice
    elif any(phrase in text_lower for phrase in [
        "but then", "everything changed", "no one expected", "suddenly",
        "without warning", "in secret", "behind closed doors",
    ]):
        settings["stability"] = 0.35
        settings["style"] = 0.90
        speed = 0.80
    # Ending/reflection (last chunk): calm, measured
    elif chunk_idx == total_chunks - 1:
        settings["stability"] = 0.50
        settings["style"] = 0.80
        speed = 0.76
    # Contains ellipsis or em-dash — dramatic pauses present
    elif text.count('...') >= 2 or text.count('—') >= 2:
        settings["stability"] = 0.38
        speed = 0.78

    return settings, speed

def run(script_data, scenes_data):
    import base64

    full_script = script_data.get("full_script", "")
    script_data.get("topic", "")

    print(f"[Audio Producer] Script: {len(full_script.split())} words, {len(full_script)} chars")

    (_BASE / "outputs" / "media").mkdir(parents=True, exist_ok=True)

    chunks = split_into_chunks(full_script)
    print(f"[Audio Producer] Split into {len(chunks)} chunks")

    all_words = []
    time_offset = 0.0
    chunks_dir = _BASE / "outputs" / "media" / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    for i, chunk in enumerate(chunks):
        print(f"[Audio Producer] Chunk {i+1}/{len(chunks)} ({len(chunk)} chars)...")
        voice_settings, speed = _get_prosody_for_chunk(chunk, i, len(chunks))
        data = generate_chunk(chunk, i, voice_settings=voice_settings, speed=speed)

        # Decode audio
        audio_b64 = data.get("audio_base64", "")
        audio_bytes = base64.b64decode(audio_b64)

        # Parse timestamps — ElevenLabs returns character-level alignment
        alignment = data.get("alignment", {})
        chars      = alignment.get("characters", [])
        starts     = alignment.get("character_start_times_seconds", [])
        ends       = alignment.get("character_end_times_seconds", [])

        # Convert character timestamps → word timestamps
        words = []
        word = ""
        word_start = None
        last_valid_end = 0.0
        for j, ch in enumerate(chars):
            if ch == " " or ch == "\n":
                if word and word_start is not None:
                    # Safely get end time: use previous char's end, or estimate
                    if j > 0 and j - 1 < len(ends):
                        word_end = ends[j - 1]
                    else:
                        # Estimate based on word length: ~80ms per character
                        word_end = word_start + max(0.15, len(word) * 0.08)
                    last_valid_end = word_end
                    words.append({
                        "word": word,
                        "start": round(word_start + time_offset, 3),
                        "end":   round(word_end + time_offset, 3)
                    })
                    word = ""
                    word_start = None
            else:
                if word_start is None and j < len(starts):
                    word_start = starts[j]
                word += ch

        # Don't forget last word
        if word and word_start is not None:
            word_end = ends[-1] if ends else last_valid_end + max(0.15, len(word) * 0.08)
            words.append({
                "word": word,
                "start": round(word_start + time_offset, 3),
                "end":   round(word_end + time_offset, 3)
            })

        all_words.extend(words)

        # Update time offset from last character end time
        if ends:
            time_offset += ends[-1] + 0.05  # small gap between chunks

        # Save chunk to individual file for ffmpeg concat
        chunk_path = chunks_dir / f"chunk_{i:02d}.mp3"
        chunk_path.write_bytes(audio_bytes)

        print(f"  ✓ {len(words)} words, chunk ends at {time_offset:.1f}s")
        time.sleep(0.5)

    # Concat all chunks with ffmpeg
    audio_path = str(_BASE / "outputs" / "media" / "narration.mp3")
    chunk_files = sorted(chunks_dir.glob("chunk_*.mp3"))
    if len(chunk_files) == 1:
        shutil.copy2(chunk_files[0], audio_path)
    else:
        concat_list = chunks_dir / "concat_list.txt"
        with open(concat_list, "w") as f:
            for cf in chunk_files:
                f.write(f"file '{cf}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c", "copy", str(audio_path)],
            check=True, capture_output=True
        )
        concat_list.unlink(missing_ok=True)

    # Save word timestamps
    timestamps_path = str(_BASE / "outputs" / "media" / "timestamps.json")
    with open(timestamps_path, "w") as f:
        json.dump({"words": all_words}, f, indent=2)

    total_duration = all_words[-1]["end"] if all_words else len(full_script.split()) / 2.2

    audio_size = os.path.getsize(audio_path)
    print(f"[Audio Producer] ✓ Audio: {audio_path} ({audio_size//1024}KB)")
    print(f"[Audio Producer] ✓ {len(all_words)} words, {total_duration:.1f}s ({total_duration/60:.1f} min)")

    return {
        "audio_path": audio_path,
        "timestamps_path": timestamps_path,
        "total_duration_seconds": total_duration,
        "word_count": len(full_script.split())
    }

if __name__ == "__main__":
    import glob
    scripts = sorted(glob.glob("outputs/*FINAL_SCRIPT.txt"))
    if not scripts:
        print("No script found")
        exit(1)
    script_path = scripts[-1]
    print(f"Using: {script_path}")
    with open(script_path) as f:
        full_script = f.read()
    result = run({"full_script": full_script, "topic": script_path}, {})
    print(result)
