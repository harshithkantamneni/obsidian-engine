import threading
import base64
import re
import json
import time
import shutil
import os
from pathlib import Path
from datetime import datetime

from core.paths import ASSETS_DIR, OUTPUT_DIR
from core.log import get_logger
from core.shutdown import _shutdown_event

logger = get_logger(__name__)


# ── Era-Specific Visual Constraints ──────────────────────────────────────────
# Prevents anachronistic imagery (e.g., Mughal domes for Mauryan pillared halls).
# Each era has positive anchors (what TO show) and negative prompts (what to EXCLUDE).

ERA_CONSTRAINTS = {
    "mauryan": {
        "keywords": ["maurya", "chandragupta", "chanakya", "kautilya", "ashoka", "magadha", "pataliputra", "arthashastra", "nanda"],
        "years": (-400, -180),
        "positive": "Mauryan pillared hall, Ashokan lion capital, bare-shouldered Brahmanic dress, sandstone architecture, simple stone columns, open courtyards",
        "negative": "no domes, no minarets, no Mughal architecture, no Islamic arches, no ornate patterns, no turbans, no medieval armor",
    },
    "roman": {
        "keywords": ["roman", "caesar", "augustus", "nero", "senate", "legion", "gladiator", "colosseum"],
        "years": (-500, 476),
        "positive": "Roman toga, marble columns, laurel wreath, Roman forum, triumphal arch, legionary armor, terracotta rooftops",
        "negative": "no medieval castles, no Gothic architecture, no plate armor, no Renaissance clothing",
    },
    "medieval_european": {
        "keywords": ["medieval", "crusade", "knight", "castle", "feudal", "monastery", "plague", "viking"],
        "years": (476, 1453),
        "positive": "Gothic stone cathedral, chainmail armor, timber frame buildings, wattle and daub, thatched roofs, stone keep",
        "negative": "no Roman toga, no Renaissance architecture, no modern buildings, no ancient ruins",
    },
    "ancient_egyptian": {
        "keywords": ["pharaoh", "egypt", "pyramid", "nile", "hieroglyph", "cleopatra", "tutankhamun", "ramses"],
        "years": (-3000, -30),
        "positive": "Egyptian linen garments, papyrus columns, sandstone temple, hieroglyphic walls, desert landscape, reed boats",
        "negative": "no Greek columns, no Roman architecture, no medieval elements, no Islamic arches",
    },
    "mughal": {
        "keywords": ["mughal", "akbar", "shah jahan", "taj mahal", "delhi sultanate", "jahanara", "aurangzeb", "dara shikoh", "mughal india"],
        "years": (1526, 1857),
        "positive": "Mughal miniature painting, Indo-Persian art style, Mughal court architecture, jali screens, marble inlay, jeweled turbans, ornate domes",
        "negative": "no ancient ruins, no bare stone pillars, no Brahmanic dress, no Roman elements, no European landscape, no American portrait, no Western oil painting, no Thomas Cole",
    },
    "ottoman": {
        "keywords": ["ottoman", "sultan", "constantinople", "topkapi", "suleiman", "byzantine", "istanbul"],
        "years": (1299, 1922),
        "positive": "Ottoman court dress, Iznik tile patterns, Byzantine-influenced domes, calligraphy, horseshoe arches, minarets, hammam interiors",
        "negative": "no European court dress, no Renaissance architecture, no Western armor, no modern buildings",
    },
    "vijayanagara": {
        "keywords": ["vijayanagara", "hampi", "krishnadevaraya", "tungabhadra", "deccan", "nayaka"],
        "years": (1336, 1646),
        "positive": "Dravidian temple architecture, gopuram towers, stone chariot, carved basalt columns, royal elephant processions, South Indian silk garments",
        "negative": "no Mughal domes, no Islamic arches, no North Indian court dress, no European elements",
    },
    "ancient_greek": {
        "keywords": ["greek", "athens", "sparta", "alexander", "philosopher", "olympics", "pericles", "socrates", "plato"],
        "years": (-800, -146),
        "positive": "Greek chiton and himation, Doric/Ionic columns, agora marketplace, olive groves, white marble temple, amphitheater",
        "negative": "no Roman elements, no medieval castles, no Renaissance art, no modern buildings",
    },
    "colonial": {
        "keywords": ["colonial", "empire", "slavery", "plantation", "east india company", "conquest"],
        "years": (1500, 1900),
        "positive": "colonial-era clothing, wooden sailing ships, plantation architecture, muskets, tricorn hats, quill and inkwell",
        "negative": "no ancient architecture, no medieval elements, no modern technology",
    },
}


def _detect_era(scenes: list, topic: str = "") -> dict | None:
    """Detect the historical era from scene data and topic name.
    Returns the matching ERA_CONSTRAINTS entry or None.
    """
    text = topic.lower()
    for scene in scenes[:5]:
        text += " " + (scene.get("narration", "") or "").lower()
        text += " " + (scene.get("year", "") or "").lower()
        text += " " + (scene.get("location", "") or "").lower()

    for era_name, constraints in ERA_CONSTRAINTS.items():
        if any(kw in text for kw in constraints["keywords"]):
            return constraints

    # Try year-based detection
    for scene in scenes:
        year_str = scene.get("year", "")
        if year_str:
            try:
                year_num = int("".join(c for c in year_str if c.isdigit() or c == "-"))
                if "bce" in year_str.lower() or "bc" in year_str.lower():
                    year_num = -abs(year_num)
                for era_name, constraints in ERA_CONSTRAINTS.items():
                    y_start, y_end = constraints["years"]
                    if y_start <= year_num <= y_end:
                        return constraints
            except (ValueError, TypeError):
                continue

    return None


def _apply_color_harmonization(scenes: list, palette: list[str]):
    """Apply subtle color grade to all generated images using the visual bible palette.

    Computes the average color temperature of the palette and applies a gentle
    tint shift to each image to create visual cohesion across the video.
    Only processes images that exist on disk.
    """
    try:
        from PIL import Image, ImageEnhance
    except ImportError:
        logger.info("[Images] Pillow not available — skipping color harmonization")
        return

    # Parse palette hex colors to RGB
    palette_rgb = []
    for hex_color in palette[:4]:
        hex_color = hex_color.lstrip("#")
        if len(hex_color) == 6:
            palette_rgb.append(tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4)))
    if not palette_rgb:
        return

    # Compute average palette color (the "target temperature")
    avg_r = sum(c[0] for c in palette_rgb) / len(palette_rgb)
    avg_g = sum(c[1] for c in palette_rgb) / len(palette_rgb)
    avg_b = sum(c[2] for c in palette_rgb) / len(palette_rgb)

    # Read optimizer-tuned blend parameters
    try:
        from core.param_overrides import get_override
        blend_factor = get_override("color.blend_factor", 0.05)
        contrast_boost = get_override("color.contrast_boost", 1.03)
    except Exception:
        blend_factor = 0.05
        contrast_boost = 1.03

    harmonized = 0
    for scene in scenes:
        img_path = scene.get("ai_image")
        if not img_path or not Path(img_path).exists():
            continue
        try:
            img = Image.open(img_path).convert("RGB")
            # Create a solid color overlay at the palette average
            overlay = Image.new("RGB", img.size, (int(avg_r), int(avg_g), int(avg_b)))
            # Blend very subtly (5%) to tint toward palette
            harmonized_img = Image.blend(img, overlay, blend_factor)
            # Contrast boost to counteract the flattening from blend (optimizer-tuned)
            enhancer = ImageEnhance.Contrast(harmonized_img)
            harmonized_img = enhancer.enhance(contrast_boost)
            harmonized_img.save(img_path, "JPEG", quality=92)
            harmonized += 1
        except Exception:
            pass  # Non-fatal — skip individual image failures

    if harmonized > 0:
        logger.info(f"[Images] Color harmonization: {harmonized} images tinted toward palette")


# Back-compat re-export: the fal retry helper now lives in the fal provider.
from providers.images.fal import _fal_subscribe_with_retry  # noqa: E402,F401


def _get_image_provider():
    from providers.registry import get_provider
    return get_provider("images")


def _image_cost_service(provider) -> str:
    """Cost-tracker service key for the active image provider."""
    try:
        from providers.registry import get_provider_name
        name = get_provider_name("images")
    except Exception:
        name = getattr(provider, "name", "images")
    return "fal_ai" if name == "fal" else name


def _log_image_cost(provider) -> None:
    try:
        from core.cost_tracker import get_active_run_id, log_cost
        _rid = get_active_run_id()
        if _rid:
            log_cost(_rid, "images", _image_cost_service(provider), 1, "images")
    except Exception:
        pass


def _place_image(src, dest) -> None:
    """Copy a provider's output image to dest (and drop provider temp files)."""
    import tempfile
    src = Path(src)
    shutil.copyfile(src, dest)
    try:
        if Path(tempfile.gettempdir()).resolve() in src.resolve().parents:
            src.unlink(missing_ok=True)
    except Exception:
        pass


def _score_image(image_path) -> int | None:
    """Score image quality 1-10 using Claude Haiku vision.

    Returns None when scoring is unavailable (LLM provider isn't the built-in
    Anthropic one, no ANTHROPIC_API_KEY, or the call failed) — callers then
    accept the image instead of regenerating it.
    """
    try:
        from clients.claude_client import anthropic_vision_available
        if not anthropic_vision_available():
            return None
    except Exception:
        return None
    try:
        from clients.claude_client import client as _vc, track_usage
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        resp = _vc.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=50,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": "Rate 1-10 for quality as documentary illustration. Reply ONLY the number."}
            ]}],
        )
        try:
            track_usage("claude-haiku-4-5-20251001", resp.usage)
        except Exception:
            pass
        match = re.search(r'\d+', resp.content[0].text)
        return int(match.group()) if match else None
    except Exception:
        return None


def _ensure_min_resolution(image_path, min_width: int = 1920, min_height: int = 1080) -> bool:
    """Upscale image to min dimensions using Lanczos if undersized. Returns True if upscaled."""
    try:
        from PIL import Image as _PILImage
        img = _PILImage.open(image_path)
        w, h = img.size
        if w >= min_width and h >= min_height:
            img.close()
            return False
        scale = max(min_width / w, min_height / h)
        new_w = int(w * scale)
        new_h = int(h * scale)
        img = img.resize((new_w, new_h), _PILImage.LANCZOS)
        img.save(image_path, quality=95)
        img.close()
        return True
    except Exception:
        return False


def _sharpen_for_video(image_path) -> bool:
    """Apply output sharpening for H.264 video delivery.

    Standard practice: H.264 + vignette + film grain soften detail.
    A subtle unsharp mask compensates without visible artifacts.
    Radius and percent are tunable via the optimizer.
    """
    try:
        from PIL import Image as _PILImage
        from PIL import ImageFilter
        try:
            from core.param_overrides import get_override
            _radius = get_override("image.sharpen_radius", 2.0)
            _percent = int(get_override("image.sharpen_percent", 120.0))
        except Exception:
            _radius = 2.0
            _percent = 120
        img = _PILImage.open(image_path)
        img = img.filter(ImageFilter.UnsharpMask(radius=_radius, percent=_percent, threshold=3))
        img.save(image_path, quality=95)
        img.close()
        return True
    except Exception:
        return False


def _generate_single_image(idx, scene, total_scenes, assets_dir, image_model,
                           mood_light, style_recraft, style_flux,
                           character_portraits=None, visual_bible=None,
                           era_constraints=None, image_provider=None):
    """Generate + quality-score one scene image. Thread-safe: unique file path per scene.

    Images come from the configured ImageProvider (providers.images).
    Returns (idx, updated_scene, success).
    """
    import copy
    _thread = threading.current_thread().name
    scene = copy.deepcopy(scene)
    img_path = assets_dir / f"scene_{idx:03d}_ai.jpg"

    # Adaptive quality thresholds based on scene importance
    position = scene.get("narrative_position", "")
    is_reveal = scene.get("is_reveal_moment", False)
    is_breathing = scene.get("is_breathing_room", False)

    try:
        from core.param_overrides import get_override
        _base_floor = int(get_override("target.image_quality_floor", 7.0))
    except Exception:
        _base_floor = 7

    if position == "hook" or is_reveal:
        IMAGE_QUALITY_THRESHOLD, IMAGE_MAX_RETRIES = max(_base_floor, 8), 5
    elif position in ("act3", "ending"):
        IMAGE_QUALITY_THRESHOLD, IMAGE_MAX_RETRIES = max(_base_floor, 8), 4
    elif is_breathing:
        IMAGE_QUALITY_THRESHOLD, IMAGE_MAX_RETRIES = max(_base_floor - 1, 4), 2
    else:
        IMAGE_QUALITY_THRESHOLD, IMAGE_MAX_RETRIES = _base_floor, 3

    try:
        narration = scene.get("narration", "")
        visual_desc = scene.get("visual_description", "")
        first = narration.split(".")[0].strip()[:200]
        mood = scene.get("mood", "dark")
        year = scene.get("year", "")
        location = scene.get("location", "")
        subject = visual_desc if visual_desc else first

        era_context = ""
        if year:
            era_context += f"set in {year}, "
        if location:
            era_context += f"in {location}, "

        # Inject character descriptions from visual bible (not just for fallback)
        vb = visual_bible or {}
        char_descs = vb.get("character_descriptions", {})
        characters = scene.get("characters_mentioned", [])
        char_desc = ""
        for c in characters[:2]:
            if c in char_descs:
                char_desc += f"{char_descs[c]}, "
                break
        if not char_desc and characters and not visual_desc:
            char_desc = f"depicting {', '.join(characters[:2])}, period-accurate clothing and appearance, "

        # Use visual_treatment from 07b instead of recalculating from narrative_position
        visual_treatment = scene.get("visual_treatment", "standard")
        composition_hint = ""
        if visual_treatment == "close_portrait":
            composition_hint = "intimate close-up portrait, face fills 60% of frame, "
        elif visual_treatment == "wide_establishing":
            composition_hint = "wide establishing shot, vast scale, figures small against grand architecture, "
        elif visual_treatment == "artifact_detail":
            composition_hint = "extreme close-up on artifact or document, raking side-light revealing texture, "
        elif visual_treatment == "map_overhead":
            composition_hint = "overhead map view, geographic perspective, "
        elif visual_treatment == "text_overlay_dark":
            composition_hint = "dark minimal frame with space for text overlay, "
        else:
            # Fallback to narrative_position for "standard" treatment
            position = scene.get("narrative_position", "")
            if position == "hook":
                composition_hint = "extreme close-up on a face frozen in a pivotal moment, "
            elif position == "act3" or scene.get("is_reveal_moment"):
                composition_hint = "wide dramatic reveal shot, figures dwarfed by grand architecture or landscape, "
            elif position == "ending":
                composition_hint = "solitary figure silhouetted against vast empty space, melancholic distance, "

        # Use visual bible art_style instead of hardcoded STYLE_FLUX/STYLE_RECRAFT
        bible_art_style = vb.get("art_style", "")
        if bible_art_style:
            cur_style = f"{bible_art_style}, no text, no watermarks, no modern elements"
        else:
            cur_style = style_recraft if image_model == "recraft" else style_flux

        # Inject color palette hints from visual bible
        palette_hint = ""
        palette = vb.get("color_palette", [])
        if palette:
            palette_hint = f"color palette: {', '.join(palette[:3])}, "

        # Inject recurring motif for tense/dark/dramatic scenes
        motif_hint = ""
        motifs = vb.get("recurring_motifs", [])
        if motifs and mood in ("tense", "dark", "dramatic", "cold"):
            motif_hint = f"subtle recurring motif: {motifs[0]}, "

        # Era-specific visual anchors and negative prompts
        era_positive = ""
        era_negative = ""
        if era_constraints:
            era_positive = f"{era_constraints['positive']}, "
            era_negative = f", {era_constraints['negative']}"

        prompt = (f"{subject}, {era_context}{char_desc}{composition_hint}"
                  f"{era_positive}{palette_hint}{motif_hint}"
                  f"{mood_light.get(mood, 'deep shadows')}, {cur_style}{era_negative}")

        logger.info(f"  [{_thread}] Scene {idx+1}/{total_scenes} ({mood})...")

        provider = image_provider or _get_image_provider()
        best_score = 0
        best_path = None
        scoring_available = True
        for attempt in range(IMAGE_MAX_RETRIES):
            if _shutdown_event.is_set():
                scene["ai_image"] = None
                return (idx, scene, False)

            attempt_path = assets_dir / f"scene_{idx:03d}_ai_attempt{attempt}.jpg" if attempt > 0 else img_path

            # Character portrait → reference-image generation (e.g. fal Kontext Pro)
            _used_reference = False
            characters = scene.get("characters_mentioned", [])
            if character_portraits and characters and getattr(provider, "supports_reference_images", False):
                for c in characters:
                    if c in character_portraits:
                        portrait_path = character_portraits[c]
                        if Path(portrait_path).exists():
                            try:
                                kontext_prompt = (
                                    f"Transform this into: {subject}, {era_context}{composition_hint}"
                                    f"{era_positive}{palette_hint}{mood_light.get(mood, 'deep shadows')}. "
                                    f"Keep the character's face, build, and clothing identical. {cur_style}{era_negative}"
                                )
                                out = provider.generate_with_reference(
                                    kontext_prompt, Path(portrait_path), width=1920, height=1080)
                                _place_image(out, attempt_path)
                                _used_reference = True
                            except Exception as kontext_err:
                                logger.warning(f"  [{_thread}] Reference-image generation failed, falling back: {kontext_err}")
                                _used_reference = False
                        break

            if not _used_reference:
                out = provider.generate(prompt, style=image_model, width=1920, height=1080)
                _place_image(out, attempt_path)
            logger.info(f"  [{_thread}] {attempt_path.name} ({attempt_path.stat().st_size//1024}KB)")

            # Log image cost (per image generated, including retries)
            _log_image_cost(provider)

            # Quality gate via Claude Haiku vision (None = scoring unavailable)
            _score = _score_image(attempt_path)
            if _score is None:
                scoring_available = False
                best_path = attempt_path
                logger.info(f"  [{_thread}] Quality scoring unavailable — accepting image")
                break

            logger.info(f"  [{_thread}] Quality: {_score}/10 {'OK' if _score >= IMAGE_QUALITY_THRESHOLD else 'below threshold'}")

            # Resolution check — undersized images penalize quality score to trigger retry
            try:
                from PIL import Image as _PILImage
                _img = _PILImage.open(attempt_path)
                _w, _h = _img.size
                _img.close()
                if _w < 1920 or _h < 1080:
                    logger.warning(f"  [{_thread}] Undersized: {_w}x{_h} — treating as quality failure")
                    _score = min(_score, IMAGE_QUALITY_THRESHOLD - 1)
            except Exception:
                pass

            if _score > best_score:
                best_score = _score
                best_path = attempt_path

            if _score >= IMAGE_QUALITY_THRESHOLD:
                break

            if attempt < IMAGE_MAX_RETRIES - 1:
                logger.info(f"  [{_thread}] Regenerating (attempt {attempt+2}/{IMAGE_MAX_RETRIES})...")
                time.sleep(1)

        # Use the best image we got (even if below threshold)
        if best_path and best_path != img_path:
            shutil.copy2(best_path, img_path)
            best_path.unlink(missing_ok=True)
        for att in assets_dir.glob(f"scene_{idx:03d}_ai_attempt*.jpg"):
            att.unlink(missing_ok=True)

        if scoring_available and best_score < IMAGE_QUALITY_THRESHOLD:
            logger.warning(f"  [{_thread}] Best quality {best_score}/10 — below {IMAGE_QUALITY_THRESHOLD} threshold but using anyway")

        # Safety net: upscale final image if still undersized after all retries
        if _ensure_min_resolution(img_path):
            logger.info(f"  [{_thread}] Upscaled to minimum resolution (Lanczos)")

        # Output sharpening for H.264 delivery (compensates for vignette + grain + compression)
        _sharpen_for_video(img_path)

        scene["ai_image"] = str(img_path)
        return (idx, scene, True)

    except Exception as e:
        logger.error(f"  [{_thread}] Image generation failed for scene {idx+1}: {e}")
        # Wikimedia fallback
        visual = scene.get("visual", {})
        wiki_url = visual.get("url", "") if isinstance(visual, dict) else ""
        if wiki_url:
            try:
                fallback_path = assets_dir / f"scene_{idx:03d}_fallback.jpg"
                from pipeline.helpers import download_file as _dl
                _dl(wiki_url, fallback_path)
                # Validate downloaded file is actually an image (not a video or HTML page)
                _is_valid_image = False
                try:
                    from PIL import Image as _PILCheck
                    _check_img = _PILCheck.open(str(fallback_path))
                    _check_img.verify()
                    _is_valid_image = True
                except Exception:
                    pass
                if not _is_valid_image:
                    fallback_path.unlink(missing_ok=True)
                    logger.warning(f"  [{_thread}] Wikimedia fallback is not a valid image — skipping")
                else:
                    if _ensure_min_resolution(fallback_path):
                        logger.info(f"  [{_thread}] Upscaled Wikimedia fallback to minimum resolution")
                    _sharpen_for_video(fallback_path)
                    scene["ai_image"] = str(fallback_path)
                    logger.info(f"  [{_thread}] Using Wikimedia fallback: {fallback_path.name}")
                    return (idx, scene, True)
            except Exception as fb_err:
                logger.error(f"  [{_thread}] Wikimedia fallback also failed: {fb_err}")
        scene["ai_image"] = None
        return (idx, scene, False)


def _generate_character_portraits(visual_bible, scenes, assets_dir, image_provider=None):
    """Generate reference portraits for top characters. Returns {name: path}.

    Only used when the image provider supports reference images (the built-in
    fal provider does, via Kontext Pro); otherwise portraits would be unused.
    """
    char_descs = visual_bible.get("character_descriptions", {})
    art_style = visual_bible.get("art_style", "")
    if not char_descs:
        return {}
    provider = image_provider or _get_image_provider()
    if not getattr(provider, "supports_reference_images", False):
        return {}

    # Count character appearances to prioritize
    char_counts = {}
    for scene in scenes:
        for c in scene.get("characters_mentioned", []):
            char_counts[c] = char_counts.get(c, 0) + 1

    top_chars = sorted(char_descs.keys(), key=lambda c: char_counts.get(c, 0), reverse=True)[:5]
    portraits = {}

    for char_name in top_chars:
        desc = char_descs[char_name]
        slug = re.sub(r'[^a-z0-9]+', '_', char_name.lower()).strip('_')
        portrait_path = assets_dir / f"character_ref_{slug}.jpg"

        try:
            portrait_prompt = (
                f"Portrait of {desc}, neutral studio background, "
                f"front-facing 3/4 view, {art_style}, no text, no watermarks"
            )
            out = provider.generate(portrait_prompt, style="flux", width=1024, height=1024)
            _place_image(out, portrait_path)
            _log_image_cost(provider)
            score = _score_image(portrait_path)
            if score is None or score >= 7:
                portraits[char_name] = str(portrait_path)
                logger.info(f"[Portraits] {char_name}: {score if score is not None else 'unscored'}/10")
            else:
                logger.warning(f"[Portraits] {char_name} scored {score}/10 — skipping")
                portrait_path.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"[Portraits] Failed for {char_name}: {e}")

    return portraits


def run_images(manifest):
    from providers.registry import get_provider_name
    provider_name = get_provider_name("images")
    if provider_name == "fal":
        from providers.images.fal import FalProvider
        if not FalProvider.has_credentials():
            logger.warning("[Images] WARNING: FAL_API_KEY not set — skipping image generation")
            return manifest
    image_provider = _get_image_provider()

    scenes = manifest.get("scenes", [])

    # Detect historical era for visual constraints
    era_constraints = _detect_era(scenes, manifest.get("topic", ""))
    if era_constraints:
        logger.info(f"[Images] Era detected — applying visual constraints: {era_constraints['positive'][:60]}...")

    # Generate character reference portraits if visual bible is available
    visual_bible = manifest.get("visual_bible", {})
    character_portraits = _generate_character_portraits(visual_bible, scenes, ASSETS_DIR, image_provider) if visual_bible else {}
    if character_portraits:
        logger.info(f"[Images] Character portraits generated: {len(character_portraits)}")

    IMAGE_MODEL = os.getenv("IMAGE_MODEL", "flux").lower()

    STYLE_FLUX = ("masterwork oil painting, museum-quality historical illustration, "
                  "dramatic cinematic composition inspired by Caravaggio and Rembrandt, "
                  "rich chiaroscuro with deep shadows and luminous highlights, "
                  "ultra-detailed faces and period-accurate costumes, "
                  "16:9 widescreen composition, textured canvas feel, "
                  "no text, no watermarks, no modern elements, no UI")
    STYLE_RECRAFT = ("historical documentary illustration, dramatic cinematic lighting, "
                     "rich chiaroscuro inspired by Caravaggio and Rembrandt, "
                     "ultra-detailed period-accurate costumes and architecture, "
                     "no text, no watermarks, no modern elements")
    MOOD_LIGHT = {
        "tense":    "sinister torchlight casting long shadows on stone walls, blood-red undertones, faces half-hidden in darkness",
        "dramatic": "golden candlelight flooding the scene, high contrast chiaroscuro like Caravaggio, dramatic spotlight on central figure",
        "dark":     "single guttering flame in vast darkness, deep impenetrable shadows, silhouettes against dim firelight",
        "cold":     "cold blue moonlight through stone arches, frost-pale skin tones, desolate winter atmosphere",
        "reverent": "warm amber cathedral light streaming through stained glass, sacred golden glow, solemn processional atmosphere",
        "wonder":   "vast open sky with golden hour light, awe-inspiring scale, luminous atmospheric perspective, warm sunlight on massive architecture",
        "warmth":   "soft warm firelight, intimate candlelit interior, gentle amber tones on human faces, tender domestic atmosphere",
        "absurdity":"bright incongruous lighting, slightly surreal color palette, vivid saturated tones that feel dreamlike and impossible",
    }

    if provider_name == "fal":
        logger.info(f"[Images] Using model: {IMAGE_MODEL} ({'Recraft v3' if IMAGE_MODEL == 'recraft' else 'Flux Pro Ultra'})")
    else:
        logger.info(f"[Images] Using image provider: {provider_name} ({image_provider.name})")
    generated = 0

    # Pre-filter cached scenes (synchronous)
    to_generate = []
    for idx, scene in enumerate(scenes):
        img_path = ASSETS_DIR / f"scene_{idx:03d}_ai.jpg"
        if img_path.exists():
            logger.info(f"[Images] Scene {idx+1}/{len(scenes)}: cached")
            scene["ai_image"] = str(img_path)
            generated += 1
        else:
            to_generate.append((idx, scene))

    # Parallel generation for uncached scenes (3 workers)
    if to_generate:
        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
        logger.info(f"[Images] Generating {len(to_generate)} scenes with 3 parallel workers...")
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="img_gen") as pool:
            futures = {}
            for idx, scene in to_generate:
                fut = pool.submit(
                    _generate_single_image, idx, scene, len(scenes),
                    ASSETS_DIR, IMAGE_MODEL, MOOD_LIGHT, STYLE_RECRAFT, STYLE_FLUX,
                    character_portraits, visual_bible, era_constraints, image_provider,
                )
                futures[fut] = idx

            for future in as_completed(futures):
                try:
                    result_idx, updated_scene, success = future.result(timeout=300)
                    scenes[result_idx] = updated_scene
                    if success:
                        generated += 1
                except TimeoutError:
                    tidx = futures[future]
                    logger.error(f"  [Images] Scene {tidx+1} timed out (300s)")
                    scenes[tidx]["ai_image"] = None
                except Exception as e:
                    tidx = futures[future]
                    logger.error(f"  [Images] Scene {tidx+1} unexpected error: {e}")
                    scenes[tidx]["ai_image"] = None

    manifest["scenes"] = scenes
    logger.info(f"[Images] ✓ Generated {generated}/{len(scenes)} images")

    # ── Color harmonization pass ─────────────────────────────────────────────
    # Apply visual bible palette as a subtle color grade to unify all images.
    # Uses PIL to shift color temperature toward the palette's dominant tone.
    if visual_bible and visual_bible.get("color_palette"):
        try:
            _apply_color_harmonization(scenes, visual_bible["color_palette"])
        except Exception as ch_err:
            logger.warning(f"[Images] Color harmonization skipped: {ch_err}")

    # Abort if more than 50% of scenes have no AI image
    total_scenes = len(scenes)
    scenes_with_images = sum(1 for s in scenes if s.get("ai_image"))
    if total_scenes > 0 and scenes_with_images / total_scenes < 0.5:
        raise Exception(
            f"[Images] ABORT: Only {scenes_with_images}/{total_scenes} scenes have images "
            f"({scenes_with_images/total_scenes*100:.0f}%). Pipeline would produce unwatchable video. "
            f"Check {image_provider.name} API key and quota, then retry with --from-stage 10"
        )

    # Write image attribution audit log
    audit_log = []
    for i, scene in enumerate(scenes):
        entry = {
            "scene_index": i,
            "has_image": bool(scene.get("ai_image")),
            "source": "none",
            "license": "none",
            "source_url": "",
        }
        ai_img = scene.get("ai_image")
        wiki_img = scene.get("wikimedia_url", "") or scene.get("footage_url", "")

        if ai_img and "ai.jpg" in str(ai_img):
            entry["source"] = f"{image_provider.name} (AI generated)"
            entry["license"] = "AI-generated, no copyright"
        elif wiki_img and "wikimedia" in str(wiki_img).lower():
            entry["source"] = "Wikimedia Commons"
            entry["license"] = "Public Domain / CC"
            entry["source_url"] = str(wiki_img)
        elif ai_img:
            entry["source"] = "unknown"
            entry["license"] = "verify manually"

        audit_log.append(entry)

    audit_path = OUTPUT_DIR / "image_audit_log.json"
    audit_data = {
        "generated_at": datetime.now().isoformat(),
        "total_scenes": len(scenes),
        "scenes_with_images": sum(1 for s in scenes if s.get("ai_image")),
        "sources": audit_log,
    }
    with open(audit_path, "w") as f:
        json.dump(audit_data, f, indent=2)
    logger.info(f"[Images] Audit log: {audit_path.name}")
    try:
        from core.utils import persist_json_to_supabase
        persist_json_to_supabase(audit_path, audit_data)
    except Exception:
        pass

    return manifest
