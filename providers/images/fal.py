"""
fal.ai image generation provider — default implementation.

Models (chosen with the ``style`` argument, which the pipeline sets from the
IMAGE_MODEL env var; default "flux"):
  - "flux":    fal-ai/flux-pro/v1.1-ultra (aspect ratio derived from width/height)
  - "recraft": fal-ai/recraft/v3/text-to-image (digital_illustration)
Reference images (character consistency): fal-ai/flux-pro/kontext.

Needs FAL_API_KEY (or FAL_KEY) in the environment; it is checked when an image
is generated, not when the provider is created.
"""

from __future__ import annotations

import base64
import os
import random
import tempfile
import time
from math import gcd
from pathlib import Path

from core.log import get_logger
from providers.base import ImageProvider

logger = get_logger(__name__)

FLUX_ULTRA = "fal-ai/flux-pro/v1.1-ultra"
RECRAFT_V3 = "fal-ai/recraft/v3/text-to-image"
FLUX_KONTEXT = "fal-ai/flux-pro/kontext"

# Aspect ratios accepted by flux-pro ultra / kontext
_FLUX_ASPECTS = {"21:9", "16:9", "4:3", "3:2", "1:1", "2:3", "3:4", "9:16", "9:21"}


def _fal_subscribe_with_retry(model: str, arguments: dict, label: str = "fal.ai",
                              max_attempts: int = 5, backoff_base: int = 2):
    """Call fal_client.subscribe with exponential-backoff retries.

    Retries on connection errors, timeouts, and rate limits (up to max_attempts).
    """
    import fal_client

    for attempt in range(1, max_attempts + 1):
        try:
            return fal_client.subscribe(model, arguments=arguments)
        except Exception as e:
            err_str = str(e).lower()
            is_retryable = any(kw in err_str for kw in [
                "timeout", "timed out", "connection", "rate limit", "429",
                "502", "503", "504", "overloaded", "temporarily",
            ])
            if attempt == max_attempts or not is_retryable:
                logger.error(f"  [{label}] Failed after {attempt} attempt(s): {e}")
                raise
            _rng = random.Random()
            wait = (backoff_base ** attempt) + _rng.uniform(0, backoff_base ** attempt * 0.5)
            logger.warning(f"  [{label}] Attempt {attempt}/{max_attempts} failed ({type(e).__name__}: {e}), retrying in {wait:.1f}s...")
            time.sleep(wait)


def _aspect_ratio(width: int, height: int) -> str:
    g = gcd(int(width), int(height)) or 1
    ratio = f"{int(width) // g}:{int(height) // g}"
    if ratio in _FLUX_ASPECTS:
        return ratio
    # Closest supported ratio
    target = width / height
    return min(_FLUX_ASPECTS, key=lambda r: abs(int(r.split(":")[0]) / int(r.split(":")[1]) - target))


class FalProvider(ImageProvider):
    """fal.ai image generation provider."""

    supports_reference_images = True

    def __init__(self, default_model: str | None = None):
        # "flux" or "recraft"; the pipeline passes style= explicitly
        self._default_model = (default_model or os.getenv("IMAGE_MODEL", "flux")).lower()

    @staticmethod
    def has_credentials() -> bool:
        return bool(os.getenv("FAL_API_KEY") or os.getenv("FAL_KEY"))

    def _ensure_keys(self):
        """Map FAL_API_KEY → FAL_KEY (what fal_client reads). Raises if neither is set."""
        key = os.getenv("FAL_API_KEY")
        if key:
            os.environ["FAL_KEY"] = key
        if not os.getenv("FAL_KEY"):
            raise RuntimeError("FAL_KEY or FAL_API_KEY not set — cannot generate images with fal.ai")
        try:
            import fal_client  # noqa: F401
        except ImportError:
            import subprocess
            import sys
            logger.info("[fal] Installing fal-client...")
            subprocess.run([sys.executable, "-m", "pip", "install", "fal-client"], check=True)

    def _download_first(self, result: dict, prompt: str) -> Path:
        from pipeline.helpers import download_file
        images = (result or {}).get("images") or []
        if not images:
            raise ValueError("fal.ai returned empty images list")
        url = images[0] if isinstance(images[0], str) else images[0].get("url", "")
        if not url:
            raise RuntimeError("fal.ai returned empty image URL")
        suffix = ".png" if ".png" in url.split("?")[0] else ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            path = Path(tmp.name)
        download_file(url, path)
        return path

    def generate(
        self,
        prompt: str,
        style: str | None = None,
        width: int = 1920,
        height: int = 1080,
        seed: int | None = None,
    ) -> Path:
        self._ensure_keys()
        model = (style or self._default_model or "flux").lower()

        if model == "recraft":
            # 16:9 requests go out at 2560x1440 (Recraft's sharpest 16:9 size)
            size = {"width": 2560, "height": 1440} if (width, height) == (1920, 1080) \
                else {"width": width, "height": height}
            endpoint = RECRAFT_V3
            args = {
                "prompt": prompt,
                "image_size": size,
                "num_images": 1,
                "style": "digital_illustration",
            }
        else:
            endpoint = FLUX_ULTRA
            args = {
                "prompt": prompt,
                "aspect_ratio": _aspect_ratio(width, height),
                "num_images": 1,
                "safety_tolerance": "2",
            }
        if seed is not None:
            args["seed"] = seed

        result = _fal_subscribe_with_retry(endpoint, args, label=f"fal {model}")
        return self._download_first(result, prompt)

    def generate_with_reference(
        self,
        prompt: str,
        reference_image: Path,
        width: int = 1920,
        height: int = 1080,
        seed: int | None = None,
    ) -> Path:
        self._ensure_keys()
        with open(reference_image, "rb") as f:
            ref_b64 = base64.b64encode(f.read()).decode()
        args = {
            "image_url": f"data:image/jpeg;base64,{ref_b64}",
            "prompt": prompt,
            "aspect_ratio": _aspect_ratio(width, height),
            "num_images": 1,
            "safety_tolerance": "2",
        }
        if seed is not None:
            args["seed"] = seed
        result = _fal_subscribe_with_retry(FLUX_KONTEXT, args, label="fal kontext")
        return self._download_first(result, prompt)

    def estimate_cost(self) -> float:
        # fal.ai: ~$0.01-0.05 per image depending on model
        return 0.05

    @property
    def name(self) -> str:
        return "fal.ai"
