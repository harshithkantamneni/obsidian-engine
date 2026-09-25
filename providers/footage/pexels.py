"""
Pexels stock footage provider — default implementation.

Free API with generous limits. Needs PEXELS_API_KEY; without it search()
returns [] and the footage hunter falls back to Wikimedia stills.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from providers.base import FootageProvider


class PexelsProvider(FootageProvider):
    """Pexels stock footage provider."""

    API_BASE = "https://api.pexels.com/videos"

    def __init__(self, min_width: int = 1280):
        self._api_key = os.getenv("PEXELS_API_KEY")
        self._min_width = min_width

    def _pick_file(self, files: list[dict]) -> dict | None:
        """Prefer the first HD rendition at least min_width wide (what the
        footage hunter always used); otherwise the widest file if it is wide
        enough."""
        hd = [f for f in files if f.get("quality") == "hd" and f.get("width", 0) >= self._min_width]
        if hd:
            return hd[0]
        best = max(files, key=lambda f: f.get("width", 0) or 0, default=None)
        if best and (best.get("width", 0) or 0) >= self._min_width:
            return best
        return None

    def search(
        self,
        query: str,
        orientation: str = "landscape",
        min_duration: int = 5,
        max_results: int = 5,
    ) -> list[dict]:
        import requests
        api_key = self._api_key or os.getenv("PEXELS_API_KEY")
        if not api_key:
            return []

        r = requests.get(
            f"{self.API_BASE}/search",
            headers={"Authorization": api_key},
            params={
                "query": query,
                "orientation": orientation,
                "per_page": max_results,
            },
            timeout=10,
        )
        if r.status_code != 200:
            return []

        results = []
        for video in json.loads(r.text, strict=False).get("videos", []):
            duration = video.get("duration", 0) or 0
            if duration < min_duration:
                continue
            best = self._pick_file(video.get("video_files", []) or [])
            if not best:
                continue
            user = (video.get("user") or {}).get("name", "Pexels")
            results.append({
                "url": best.get("link", ""),
                "duration": duration or 10,
                "width": best.get("width", 1920),
                "height": best.get("height", 1080),
                "preview_url": video.get("image", ""),
                "credit": f"Video by {user} on Pexels",
            })

        return results[:max_results]

    def download(self, url: str, output_path: Path) -> Path:
        from pipeline.helpers import download_file
        download_file(url, str(output_path))
        return output_path

    @property
    def name(self) -> str:
        return "Pexels"
