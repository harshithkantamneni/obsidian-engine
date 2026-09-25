"""
Example upload provider: copy the finished video into a folder.

Handy for a synced folder (Dropbox, Google Drive, a NAS mount) or as a
template for S3/R2/Vimeo uploads: replace the copy with your API call and
return the id/url it gives you.

obsidian.yaml:
    providers:
      upload:
        name: examples.providers.folder_upload.CopyToFolder
        options:
          folder: ~/Videos/obsidian
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from providers.base import UploadProvider


class CopyToFolder(UploadProvider):
    def __init__(self, folder: str = "./outputs/published"):
        self.folder = Path(folder).expanduser()

    def upload(self, video_path, title, description, tags, thumbnail_path=None):
        self.folder.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "video"
        video_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}"

        dest = self.folder / f"{video_id}.mp4"
        shutil.copy2(video_path, dest)
        if thumbnail_path and Path(thumbnail_path).exists():
            shutil.copy2(thumbnail_path, dest.with_suffix(".jpg"))
        dest.with_suffix(".json").write_text(json.dumps({
            "title": title, "description": description, "tags": tags,
        }, indent=2))

        # video_id must be non-empty — the pipeline treats "" as a failed upload
        return {"video_id": video_id, "url": dest.resolve().as_uri(), "status": "copied"}

    @property
    def name(self):
        return "Copy to folder"
