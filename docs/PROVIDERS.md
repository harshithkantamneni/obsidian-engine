# Providers: bring your own services

Every external service the pipeline uses goes through a **provider**:

| Type | Used for | Built-in names |
|------|----------|----------------|
| `llm` | every agent (research, script, SEO, scenes…) | `anthropic` (default), `openai` |
| `tts` | narration (long-form and Shorts) | `elevenlabs` (default), `openai`, `epidemic_sound` |
| `images` | scene images, character portraits, Shorts images, thumbnails | `fal` (default) |
| `footage` | stock video for atmospheric scenes | `pexels` (default) |
| `upload` | publishing the final video and Short (stage 13) | `local` (default), `youtube` |
| `music` | background track | `auto` (default), `local`, `epidemic_sound` |
| `sfx` | per-scene sound effects and ambience | `auto` (default), `local`, `epidemic_sound` |

`auto` picks `epidemic_sound` when `EPIDEMIC_SOUND_API_KEY` is set and `local` otherwise.

You can replace any of them with your own class. You don't need to edit
pipeline code or register anything.

## Quick start

1. Write a class that extends the matching base class in `providers/base.py`:

   ```python
   # my_providers/tts.py   (next to run_pipeline.py)
   from providers.base import TTSProvider

   class MyTTS(TTSProvider):
       def __init__(self, api_url: str, voice: str = "narrator"):
           self.api_url = api_url
           self.voices = {"narrator": voice}
       def synthesize(self, text, voice_id=None, voice_settings=None, speed=1.0):
           ...  # call your service, write audio to a file
           return audio_path, []          # [] = let the pipeline align words
       def list_voices(self): return []
       def check_credits(self): return {"remaining": -1, "limit": -1, "unit": "characters"}
       @property
       def name(self): return "My TTS"
   ```

2. Point `obsidian.yaml` at it. Each key under `options` becomes a keyword argument to the constructor:

   ```yaml
   providers:
     tts:
       name: my_providers.tts.MyTTS
       options:
         api_url: http://localhost:8000/tts
         voice: narrator
   ```

3. Check that it loads:

   ```bash
   python -m providers
   ```

   This imports and constructs every configured provider and prints `ok` or the error for each one. It makes no API calls.

**Loading rules**
- `name` is either a built-in name or a dotted `module.path.ClassName`. The module must be importable from the project root, either as a file or package inside the repo or as an installed package. If it can't be imported, the error names the module.
- If an option doesn't match a constructor argument, the error lists the option keys you passed.
- Each provider is created once per process and reused.
- Read API keys from environment variables (`.env` is loaded). Check them when the provider is used, not in `__init__`, so that `python -m providers` works without keys.

Working examples are in `examples/providers/`: `http_tts.py` (any HTTP TTS service) and `folder_upload.py` (copy the video to a folder, a starting point for S3/R2/Vimeo).

---

## `llm`: `LLMProvider`

```python
def generate(self, system_prompt: str, user_prompt: str, model: str | None = None,
             max_tokens: int = 4000, expect_json: bool = True,
             output_schema: dict | None = None) -> dict | list | str
def generate_with_search(self, system_prompt, user_prompt, model=None,
                         max_tokens=4000, output_schema=None) -> str
def estimate_cost(self, input_tokens: int, output_tokens: int, model=None) -> float
name: str  (property)
# optional
def resolve_model(self, tier: str) -> str | None     # default: self.models.get(tier)
```

- **How it's called.** All agents go through `clients/claude_client.call_claude()` and `call_claude_with_search()`. If the provider is anything other than `anthropic`, these call your `generate()` or `generate_with_search()`.
- **Model tiers.** The pipeline asks for a tier, not a model: `premium` (creative writing), `full` (reasoning), or `light` (formatting and classification). It passes `model=resolve_model(tier)`. If that returns `None`, `generate()` should fall back to its default model. The easiest approach is to accept a `models` option: `models: {premium: ..., full: ..., light: ...}`.
- **JSON.** When `expect_json=True`, return a parsed `dict` or `list`. If you return a string, the pipeline parses it leniently: it removes code fences, trailing text and truncation. `output_schema` is a JSON Schema. Enforce it if your API supports structured output; otherwise treat it as a hint.
- **Search.** `generate_with_search` returns plain text. If your model has no web search, generate without it (the built-in `openai` provider does this).
- **Limitations.** Token costs aren't tracked for LLMs other than Anthropic, so the per-run budget cap doesn't cover LLM spend. The pipeline logs this once per run. Claude-vision features (image and thumbnail quality scoring) only run with `anthropic`. Without it, images are accepted as generated and thumbnails get a neutral score.
- **Built-in `openai` options:** `default_model`, `models`, `base_url` (for any OpenAI-compatible server, e.g. Ollama), `api_key_env`.

## `tts`: `TTSProvider`

```python
def synthesize(self, text: str, voice_id: str | None = None,
               voice_settings: dict | None = None, speed: float = 1.0
               ) -> tuple[Path, list[dict]]
def list_voices(self) -> list[dict]          # [{"id", "name", "description"}]
def check_credits(self) -> dict              # {"remaining", "limit", "unit"}
name: str
# optional
def resolve_voice(self, role: str) -> str | None   # role: "narrator" | "quote"; default: self.voices.get(role)
```

- **How it's called.** The pipeline splits narration into chunks of roughly one scene each. For each chunk it calls `synthesize(text, voice_id=resolve_voice(role), voice_settings=..., speed=...)`. `role` is `"quote"` for quoted historical speech and `"narrator"` for everything else.
- **`voice_settings` and `speed`.** `voice_settings` holds ElevenLabs-style delivery settings (`stability`, `similarity_boost`, `style`, `use_speaker_boost`, `speed`) that vary by scene mood. Ignore any you can't use. `speed` is usually between 0.65 and 1.0.
- **Audio.** Return any file ffmpeg can read. Anything that isn't a 44.1 kHz MP3 is transcoded. Temp files are fine; files in the system temp dir are deleted after they are copied.
- **Timestamps.** Return `[{"word", "start", "end"}]` in seconds from the start of the clip. If you return `[]`, the pipeline runs forced alignment (`media/forced_alignment.py`, which uses Whisper if `openai-whisper` is installed). Otherwise it spreads the words evenly over the clip and logs a warning; captions will then be approximate.
- **`check_credits`.** Called once at startup. Return `limit <= 0` if the quota is unknown. Raise an exception whose message contains `401` to abort the run when the key is invalid.
- **Built-in options:**
  - `elevenlabs`: `model` (default `voice.model`), `voices`
  - `openai`: `model` (`tts-1`), `voice` (`alloy`), `quote_voice`

## `images`: `ImageProvider`

```python
def generate(self, prompt: str, style: str | None = None, width: int = 1920,
             height: int = 1080, seed: int | None = None) -> Path
def estimate_cost(self) -> float
name: str
# optional
supports_reference_images: bool = False
def generate_with_reference(self, prompt, reference_image: Path, width=1920,
                            height=1080, seed=None) -> Path
```

- **Sizes requested:**

  | Use | Size |
  |---|---|
  | Scenes | 1920x1080 |
  | Shorts | 1440x2560 |
  | Thumbnails | 1280x720 |
  | Character portraits | 1024x1024 |

  Undersized scene images are upscaled.
- **`style`.** Set from the `IMAGE_MODEL` env var (`flux` or `recraft`). The built-in fal provider uses it to choose a model; your provider can ignore it.
- **Output.** Return a JPEG or PNG. It is copied into place.
- **Threads.** Scene images are generated by 3 worker threads, so `generate` must be thread-safe.
- **Reference images.** Set `supports_reference_images = True` and implement `generate_with_reference` to keep characters consistent. The pipeline then generates reference portraits of the main characters and passes them in. Without it, portraits are skipped.
- **Quality scoring.** Only runs with the `anthropic` LLM and `ANTHROPIC_API_KEY` set. Otherwise the first image is used and nothing is regenerated.

## `footage`: `FootageProvider`

```python
def search(self, query: str, orientation: str = "landscape", min_duration: int = 5,
           max_results: int = 5) -> list[dict]
    # [{"url", "duration", "width", "height", "preview_url", "credit"?}], best first
def download(self, url: str, output_path: Path) -> Path
name: str
```

- The footage hunter (stage 9) calls `search(query, orientation="landscape", min_duration=0, max_results=5)` for atmospheric scenes. It stores the first result's `url`, `width`, `height`, `duration` and `credit` in the visuals manifest, with `source` set to the provider name. The default credit is `"Footage via <name>"`.
- Results are cached for 7 days by provider name and query.
- Historical scenes use Wikimedia Commons first; that isn't a provider.

## `upload`: `UploadProvider`

```python
def upload(self, video_path: Path, title: str, description: str, tags: list[str],
           thumbnail_path: Path | None = None) -> dict
    # {"video_id": str (non-empty), "url": str, "status": str}
name: str
```

- **Where it's called.** Stage 13 uses it for the long-form video and the Shorts pipeline uses it for the Short. The pipeline builds the title, the description (with chapters and sources) and the tags from the SEO stage. For the long-form video it also picks the best thumbnail.
- **Empty `video_id`.** An empty `video_id` counts as a failed upload.
- **YouTube-only steps.** Only the `youtube` provider triggers them: the sources comment, era playlist, endscreen and cards, the post-upload analytics agent, comment analysis, the optimizer's retention observation and the community post. Other providers skip all of these.
- **Built-in options:**
  - `local`: `output_dir` (default `outputs/final/`)
  - `youtube`: `privacy` (`public`, `unlisted` or `private`); needs `client_secrets.json` or `GOOGLE_CLIENT_SECRETS_JSON`

## `music`: `MusicProvider`

```python
def select_for_video(self, scenes: list[dict], total_duration: float) -> dict | None
    # {"music_file": str, "music_start_offset": float?, ...} (other keys optional)
def search(self, mood: str, duration: float = 600, **kwargs) -> list[dict]
def download(self, track_id: str, output_path: Path, stem: str | None = None) -> Path
name: str
```

- **How it's called.** The convert stage (stage 11) calls `select_for_video` with the Remotion scenes: `mood`, `start_time`, `end_time`, `narrative_position` and so on.
- **`music_file`.** It can be a path relative to `remotion/public` (e.g. `music/track.mp3`) or an absolute path. Absolute paths and files outside `remotion/public` are copied into `remotion/public/music/`.
- **Fallback.** If you return `None` or raise, the pipeline uses the local library instead: energy-matched selection, then a random mood match, then the bundled mood tracks.
- **`search` and `download`.** The pipeline doesn't call these, but they are part of the interface. Raise `NotImplementedError` if you don't need them.
- Epidemic track adaptation (exact duration and stems) only applies to files named `epidemic_*`.

## `sfx`: `SFXProvider`

```python
def search(self, keyword: str, duration_max: float = 5.0, **kwargs) -> list[dict]  # [{"id", "title", "duration", "tags"}]
def download(self, sfx_id: str, output_path: Path) -> Path
name: str
# optional (defaults use search + download)
def sfx_for_scene(self, scene: dict, dest_dir: Path) -> str | None      # dest_dir = remotion/public/sfx
def ambient_for_scene(self, scene: dict, dest_dir: Path) -> str | None  # dest_dir = remotion/public/ambience
```

- **When it's called.** `ambient_for_scene` is called for every scene. `sfx_for_scene` is only called for key scenes (reveals, climaxes, act transitions, breathing room). Silence-beat scenes get neither.
- **Return value.** Return a path relative to `remotion/public` (e.g. `"sfx/hit.mp3"`) or `None`.
- **Default behaviour.** The default `sfx_for_scene` searches for `"<mood> cinematic impact"` with `duration_max=5`. The default `ambient_for_scene` searches for `"<mood> ambience"` with `duration_max=30`. Both download the first result into `dest_dir` and cache it per keyword. Override them for smarter choices.

---

## Testing your provider

```python
from providers import registry
registry.clear_cache()
tts = registry.get_provider("tts")          # what the pipeline will get
path, words = tts.synthesize("Hello there.")
```

`tests/test_provider_wiring.py` has fake providers for all seven types (`tests/_fake_providers.py`) and runs each of them through the real pipeline functions. Use it as a template.
