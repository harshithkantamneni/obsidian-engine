"""
YouTube upload provider — publishes via the YouTube Data API.

Wraps agents/11_youtube_uploader.upload_video (OAuth: client_secrets.json /
youtube_token.json, or GOOGLE_CLIENT_SECRETS_JSON / YOUTUBE_TOKEN_JSON env).
When this provider is selected, the uploader agent also runs the
YouTube-only follow-ups (sources comment, era playlist, endscreen/cards).

obsidian.yaml:
    providers:
      upload:
        name: youtube
        options:
          privacy: public      # public | unlisted | private
"""

from __future__ import annotations

from pathlib import Path

from providers.base import UploadProvider


def _load_uploader_agent():
    """Load agents/11_youtube_uploader.py (file name starts with a digit)."""
    from pipeline.loader import load_agent
    return load_agent(Path("11_youtube_uploader.py"))


class YouTubeUploadProvider(UploadProvider):
    """Upload to YouTube with the channel's OAuth credentials."""

    def __init__(self, privacy: str | None = None):
        # None = use the caller's privacy (the pipeline publishes "public")
        self.privacy = privacy

    def upload(
        self,
        video_path: Path,
        title: str,
        description: str,
        tags: list[str],
        thumbnail_path: Path | None = None,
        privacy: str | None = None,
    ) -> dict:
        agent = _load_uploader_agent()
        result = agent.upload_video(
            str(video_path), title, description, tags,
            str(thumbnail_path) if thumbnail_path else None,
            privacy or self.privacy or "public",
        )
        result = dict(result or {})
        result.setdefault("status", "uploaded")
        return result

    @property
    def name(self) -> str:
        return "YouTube"
