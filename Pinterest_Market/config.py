"""
Pinterest_Market/config.py — paths and knobs shared by prepare.py and post.py.

Stdlib only: post.py runs on GitHub Actions with a minimal install, so nothing
imported here may pull in OpenCV, Gemini or the Etsy client.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent            # .../Pinterest_Market

DESIGNS_DIR  = ROOT / "Designs_To_Use"            # transparent design PNGs
MOCKUP_DIR   = ROOT / "Mockup"                    # your own shirt photos
READY_DIR    = ROOT / "ready"                     # rendered pin images (committed)
QUEUE_FILE   = ROOT / "queue.json"                # posting queue (committed)
CORNERS_FILE = ROOT / "mockup_corners.json"       # print area per mockup photo
LISTING_MAP  = ROOT / "listing_map.json"          # manual design → Etsy listing id

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# How many pins one run posts. The workflow runs 3×/day, so 1 here = 3 pins/day.
POSTS_PER_RUN = 1

# Rendered pin size. Pinterest favours tall images; the mockup's own aspect
# ratio is kept, only the width is capped. JPEG keeps the repo small (~300 KB/pin).
PIN_WIDTH    = 1000
JPEG_QUALITY = 88

# Where a pin links when its design has no exact, active Etsy listing.
SHOP_URL = "https://www.etsy.com/shop/Nullish?etsrc=sdt"

# Every pin in this queue is a tee on your mockup photo.
GARMENT = "tee"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_queue() -> dict:
    q = load_json(QUEUE_FILE, {"items": []})
    q.setdefault("items", [])
    return q


def image_files(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS)
