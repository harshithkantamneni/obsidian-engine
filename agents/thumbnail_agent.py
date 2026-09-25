"""
Thumbnail Agent — Generate and score high-quality YouTube thumbnails.

Generates distinct thumbnail concepts via the LLM, renders them with the
configured image provider (providers.images — fal.ai Flux Pro by default),
adds bold text overlay with Pillow, scores each with Claude Haiku vision (when
the LLM provider is Anthropic; otherwise variants get a neutral score), and
returns the best one.
"""

from __future__ import annotations
import os
import sys
import json
import re
import base64
import time
from pathlib import Path

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from core.agent_wrapper import call_agent
from clients.claude_client import HAIKU

# ── Pillow setup ──────────────────────────────────────────────────────────────
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "Pillow"], check=True)
    from PIL import Image, ImageDraw, ImageFont

# ── Anthropic client for vision scoring (shared client for cost tracking) ─────
from clients.claude_client import client as _vision_client, track_usage  # noqa: E402

BASE_DIR   = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "outputs" / "media"
THUMB_DIR  = OUTPUT_DIR / "thumbnails"


# ── Step 1: Generate thumbnail concepts ──────────────────────────────────────

def _generate_concepts(title: str, angle: str, hook: str, topic: str = "") -> list[dict]:
    """Use Claude Sonnet to design 3 distinct thumbnail concepts."""

    # Inject content quality thumbnail intelligence
    thumbnail_intel = ""
    try:
        from intel.channel_insights import get_content_quality_recommendation
        cq_rec = get_content_quality_recommendation("thumbnail")
        if cq_rec:
            thumbnail_intel = f"\n\nTHUMBNAIL PERFORMANCE DATA:\n{cq_rec}"
    except Exception:
        pass

    system = """You are a YouTube thumbnail design expert specializing in dark history
and documentary content. You understand what makes viewers click: bold contrast,
emotional faces, dramatic scenes, and minimal but impactful text.

Design 5 DISTINCT thumbnail concepts. Each must differ in composition, color
scheme, and emotional approach. Think like MrBeast's designer — every pixel
must earn attention at 160x90px (mobile thumbnail size).

Vary the COMPOSITION TYPE across concepts:
1. FACE CLOSE-UP: intense expression fills 60% of frame, dramatic lighting
2. WIDE ESTABLISHING: vast scene with dramatic scale (tiny figure vs huge architecture)
3. TEXT-HEAVY: bold 2-3 word text dominates, minimal background
4. ARTIFACT/OBJECT: weapon, crown, document, or key object in dramatic spotlight
5. SPLIT/CONTRAST: before/after, life/death, power/ruin — two halves that tell a story
""" + thumbnail_intel + """
Return JSON with this exact structure:
{
  "concepts": [
    {
      "text_overlay": "2-3 BOLD words for the thumbnail (ALL CAPS)",
      "color_scheme": "warm|cool|dark_contrast|golden|blood_red",
      "composition": "description of layout and focal point",
      "emotional_tone": "what emotion this triggers in the viewer",
      "image_prompt": "detailed image generation prompt (NO text in the image, cinematic, high contrast, 16:9)"
    }
  ]
}"""

    user = f"""Design 5 thumbnail concepts for this YouTube video:

TITLE: {title}
ANGLE: {angle}
HOOK (first line of script): {hook}

Requirements:
- Text overlay: 2-3 words MAX, must be readable at thumbnail size
- Image must work WITHOUT the text too (text is added separately)
- Each concept should target a different emotional response
- Optimize for dark history / documentary niche
- NO text, watermarks, or words in the image generation prompt
- Include dramatic lighting, high contrast, cinematic composition
- Faces/figures should have intense expressions when applicable"""

    result = call_agent("thumbnail_agent", system_prompt=system, user_prompt=user, max_tokens=2000, topic=topic)
    concepts = result.get("concepts", [])

    if not concepts:
        raise ValueError("Claude returned no thumbnail concepts")

    return concepts[:5]


# ── Step 2: Generate thumbnail images via the image provider ─────────────────

def _generate_image(prompt: str, index: int) -> str | None:
    """Generate a single thumbnail image with the configured image provider. Returns path or None."""

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    img_path = THUMB_DIR / f"thumb_variant_{index:02d}.jpg"

    # Enhance prompt for thumbnail-specific rendering
    thumb_style = (
        "ultra high contrast, bold cinematic composition optimized for YouTube thumbnail, "
        "dramatic lighting, rich saturated colors, sharp focus on subject, "
        "dark atmospheric background, oil painting style, no text, no watermarks, "
        "16:9 aspect ratio, 4K detail"
    )
    full_prompt = f"{prompt}, {thumb_style}"

    try:
        from providers.registry import get_provider
        from pipeline.images import _place_image
        out = get_provider("images").generate(full_prompt, style="flux", width=1280, height=720)
        _place_image(out, img_path)
        print(f"  [Thumbnail] Variant {index + 1} generated ({img_path.stat().st_size // 1024}KB)")
        return str(img_path)
    except Exception as e:
        print(f"  [Thumbnail] Variant {index + 1} failed: {e}")
        return None


def _generate_all_images(concepts: list[dict]) -> list[dict]:
    """Generate images for all concepts. Skips failures gracefully."""

    results = []
    for i, concept in enumerate(concepts):
        prompt = concept.get("image_prompt", "")
        if not prompt:
            print(f"  [Thumbnail] Variant {i + 1}: no prompt, skipping")
            continue

        img_path = _generate_image(prompt, i)
        if img_path:
            results.append({**concept, "image_path": img_path, "index": i})
        time.sleep(0.3)  # rate limit courtesy

    return results


# ── Step 3: Add text overlay ─────────────────────────────────────────────────

def _load_bold_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try to load a bold font, fall back to default."""

    # Common bold font paths by platform
    candidates = [
        "/System/Library/Fonts/Supplemental/Impact.ttf",          # macOS
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",      # macOS
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",  # Linux
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",              # Arch Linux
        "C:\\Windows\\Fonts\\impact.ttf",                          # Windows
        "C:\\Windows\\Fonts\\arialbd.ttf",                         # Windows
    ]

    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

    # Pillow 10.1+ default with size
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _add_text_overlay(image_path: str, text: str, color_scheme: str) -> str:
    """Add bold text with drop shadow + outline to a thumbnail image."""

    img = Image.open(image_path).convert("RGBA")

    # Create text overlay layer
    txt_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(txt_layer)

    # Scale font to image — aim for text that's ~60% of image width
    font_size = 72
    font = _load_bold_font(font_size)

    # Measure and adjust font size
    text_upper = text.upper().strip()
    for test_size in range(90, 40, -4):
        test_font = _load_bold_font(test_size)
        bbox = draw.textbbox((0, 0), text_upper, font=test_font)
        text_w = bbox[2] - bbox[0]
        if text_w <= img.width * 0.85:
            font = test_font
            font_size = test_size
            break

    bbox = draw.textbbox((0, 0), text_upper, font=font)
    text_w = bbox[2] - bbox[0]

    # Position: upper-left with padding, or center for short text
    padding = 40
    if text_w < img.width * 0.5:
        # Center for short text
        x = (img.width - text_w) // 2
        y = padding + 20
    else:
        # Upper-left
        x = padding
        y = padding + 20

    # Color scheme mapping for text
    text_colors = {
        "warm":          (255, 240, 200),
        "cool":          (200, 220, 255),
        "dark_contrast": (255, 255, 255),
        "golden":        (255, 215, 80),
        "blood_red":     (220, 40, 40),
    }
    text_color = text_colors.get(color_scheme, (255, 255, 255))

    # Draw shadow (offset by 4px)
    shadow_offset = max(3, font_size // 18)
    shadow_color = (0, 0, 0, 200)
    for dx in range(-shadow_offset, shadow_offset + 1):
        for dy in range(-shadow_offset, shadow_offset + 1):
            if dx * dx + dy * dy <= shadow_offset * shadow_offset:
                draw.text((x + dx, y + dy), text_upper, font=font, fill=shadow_color)

    # Draw outline (2px black border)
    outline_width = max(2, font_size // 30)
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if abs(dx) == outline_width or abs(dy) == outline_width:
                draw.text((x + dx, y + dy), text_upper, font=font, fill=(0, 0, 0, 255))

    # Draw main text
    draw.text((x, y), text_upper, font=font, fill=text_color + (255,))

    # Composite and save
    result = Image.alpha_composite(img, txt_layer).convert("RGB")
    out_path = image_path.replace(".jpg", "_final.jpg")
    result.save(out_path, "JPEG", quality=95)
    print(f"  [Thumbnail] Text overlay added: {Path(out_path).name}")
    return out_path


# ── Step 4: Score thumbnails with Claude Haiku vision ────────────────────────

def _score_thumbnail(image_path: str, concept: dict) -> dict:
    """Score a thumbnail on clickability, readability, and emotional impact."""

    neutral = {"clickability": 5, "readability": 5, "emotional_impact": 5, "total": 15,
               "brief_note": "scoring unavailable"}
    try:
        from clients.claude_client import anthropic_vision_available
        if not anthropic_vision_available():
            return neutral
    except Exception:
        return neutral

    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    try:
        response = _vision_client.messages.create(
            model=HAIKU,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "You are a YouTube thumbnail expert. Rate this thumbnail for a dark history documentary channel.\n\n"
                            "Score each criterion 1-10:\n"
                            "1. CLICKABILITY: Would you click this at thumbnail size? (contrast, intrigue, visual hook)\n"
                            "2. READABILITY: Can you read the text clearly at 160x90px? (font size, contrast, clarity)\n"
                            "3. EMOTIONAL IMPACT: Does it evoke curiosity, fear, or fascination? (expression, mood, drama)\n\n"
                            "Reply ONLY in this JSON format:\n"
                            '{"clickability": N, "readability": N, "emotional_impact": N, "total": N, "brief_note": "one sentence"}'
                        ),
                    },
                ],
            }],
        )

        try:
            track_usage(HAIKU, response.usage)
        except Exception:
            pass
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        clean = re.sub(r"^```(?:json)?\s*", "", raw)
        clean = re.sub(r"\s*```$", "", clean).strip()
        scores = json.loads(clean, strict=False)

        # Ensure total is computed
        if "total" not in scores:
            scores["total"] = (
                scores.get("clickability", 0)
                + scores.get("readability", 0)
                + scores.get("emotional_impact", 0)
            )

        print(f"  [Thumbnail] Score: {scores['total']}/30 — {scores.get('brief_note', '')}")
        return scores

    except Exception as e:
        print(f"  [Thumbnail] Scoring failed: {e}")
        # Return neutral score so this variant can still be considered
        return {"clickability": 5, "readability": 5, "emotional_impact": 5, "total": 15, "brief_note": "scoring failed"}


# ── Main orchestrator ────────────────────────────────────────────────────────

def run(seo_data: dict, script_data: dict, angle_data: dict) -> dict | None:
    """
    Generate and score thumbnail variants. Returns best thumbnail path.

    Args:
        seo_data: SEO agent output (must contain recommended_title)
        script_data: Script writer output (must contain script or narration)
        angle_data: Originality agent output (must contain angle or unique_angle)

    Returns:
        {"thumbnail_path": str, "score": int, "concept": str} or None on total failure
    """

    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    title = seo_data.get("recommended_title", "") or seo_data.get("title", "Untitled")
    angle = angle_data.get("angle", "") or angle_data.get("unique_angle", "") or ""

    # Extract hook (first 1-2 sentences of script)
    script = script_data.get("full_script", "") or script_data.get("narration", "")
    if not isinstance(script, str):
        script = script_data.get("script", "")
        if not isinstance(script, str):
            script = ""
    hook_sentences = script.split(".")[:2]
    hook = ". ".join(s.strip() for s in hook_sentences if s.strip())[:300]

    if not title:
        print("[Thumbnail] No title available — cannot generate thumbnail")
        return None

    # ── Step 1: Generate concepts ────────────────────────────────────────────
    print(f"[Thumbnail] Generating 5 concepts for: {title[:60]}...")
    try:
        concepts = _generate_concepts(title, angle, hook)
        print(f"[Thumbnail] {len(concepts)} concepts designed")
    except Exception as e:
        print(f"[Thumbnail] Concept generation failed: {e}")
        return None

    # ── Step 2: Generate images ──────────────────────────────────────────────
    from providers.registry import get_provider_name
    if get_provider_name("images") == "fal":
        from providers.images.fal import FalProvider
        if not FalProvider.has_credentials():
            print("[Thumbnail] WARNING: FAL_API_KEY not set — cannot generate images")
            return None

    print(f"[Thumbnail] Generating images with image provider '{get_provider_name('images')}'...")
    variants = _generate_all_images(concepts)

    if not variants:
        print("[Thumbnail] All image generations failed")
        return None

    print(f"[Thumbnail] {len(variants)} images generated successfully")

    # ── Step 3: Add text overlays ────────────────────────────────────────────
    print("[Thumbnail] Adding text overlays...")
    finalized = []
    for variant in variants:
        try:
            text = variant.get("text_overlay", "")
            if not text:
                # Fallback: extract 2-3 key words from title
                words = title.upper().split()
                text = " ".join(words[:3])

            color_scheme = variant.get("color_scheme", "dark_contrast")
            final_path = _add_text_overlay(variant["image_path"], text, color_scheme)
            finalized.append({
                **variant,
                "final_path": final_path,
                "text_used": text,
            })
        except Exception as e:
            print(f"  [Thumbnail] Text overlay failed for variant {variant.get('index', '?')}: {e}")
            # Still include the raw image as a fallback
            finalized.append({
                **variant,
                "final_path": variant["image_path"],
                "text_used": "",
            })

    if not finalized:
        print("[Thumbnail] All text overlays failed")
        return None

    # ── Step 4: Score thumbnails ─────────────────────────────────────────────
    print("[Thumbnail] Scoring thumbnails with Claude Haiku vision...")
    best = None
    best_score = -1

    for variant in finalized:
        scores = _score_thumbnail(variant["final_path"], variant)
        total = scores.get("total", 0)

        if total > best_score:
            best_score = total
            best = {
                "thumbnail_path": variant["final_path"],
                "score": total,
                "concept": variant.get("text_overlay", ""),
                "scores_detail": scores,
                "composition": variant.get("composition", ""),
            }

    if not best:
        print("[Thumbnail] Scoring failed for all variants")
        return None

    # Copy best thumbnail to the standard location
    final_thumb = OUTPUT_DIR / "thumbnail.jpg"
    try:
        from shutil import copy2
        copy2(best["thumbnail_path"], final_thumb)
        best["thumbnail_path"] = str(final_thumb)
        print(f"[Thumbnail] Best thumbnail: {final_thumb.name} (score: {best_score}/30)")
    except Exception as e:
        print(f"[Thumbnail] Copy failed (using original path): {e}")

    return best


# ── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import glob as _glob

    state_files = sorted(_glob.glob(str(BASE_DIR / "outputs" / "*_state.json")))
    if not state_files:
        print("No state file found. Run run_pipeline.py first.")
        sys.exit(1)

    with open(state_files[-1]) as f:
        state = json.load(f)

    seo_data    = state.get("stage_6", {})
    script_data = state.get("stage_4", {})
    angle_data  = state.get("stage_2", {})

    result = run(seo_data, script_data, angle_data)
    if result:
        print(f"\nBest thumbnail: {result['thumbnail_path']}")
        print(f"Score: {result['score']}/30")
        print(f"Concept: {result['concept']}")
    else:
        print("\nThumbnail generation failed — caller should fall back to simple thumbnail.")
