"""
slideshow.py — Turn a mockup folder into a looping MP4 slideshow for Etsy
==========================================================================
Takes every image in output/<garment>/<design>/ (sorted by name), slowly zooms
into each one for 1.5 seconds, then cuts to the next. Writes a silent,
1080x1080, H.264 MP4 to output/video/<garment>/<design>.mp4.

Etsy's listing video rules: 5–15 seconds, no sound, one video per listing.
  - Too few images (under 5s)  → the images repeat until it reaches 5s.
  - Too many images (over 15s) → each image gets less time so it fits 15s.

Local only: nothing is uploaded to Etsy or Printify.

USAGE
  python -m scripts.create.slideshow --all                     # every sweatshirt folder
  python -m scripts.create.slideshow --all --force             # remake ones that already exist
  python -m scripts.create.slideshow --all --garment tshirt    # every t-shirt folder
  python -m scripts.create.slideshow agress                    # one folder, sweatshirt (default)
  python -m scripts.create.slideshow agress --garment tshirt
  python -m scripts.create.slideshow agress --seconds 2 --zoom 1.2
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import imageio_ffmpeg
from PIL import Image

OUTPUT_DIR = Path("output")
VIDEO_DIR = OUTPUT_DIR / "video"

SIZE = 1080                       # Etsy shows square; 1080p is its recommended minimum
FPS = 30
ETSY_MIN_SEC = 5.0
ETSY_MAX_SEC = 15.0
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def ease(t: float) -> float:
    """Smooth start and stop so the zoom doesn't feel mechanical."""
    return t * t * (3 - 2 * t)


def plan_images(images: list[Path], seconds: float) -> tuple[list[Path], float]:
    """Repeat or squeeze the image list so the video lands inside Etsy's 5–15s window."""
    if len(images) * seconds > ETSY_MAX_SEC:
        seconds = ETSY_MAX_SEC / len(images)
    plan = list(images)
    while len(plan) * seconds < ETSY_MIN_SEC:
        plan += images
    return plan, seconds


def frames_for(image: Path, seconds: float, zoom: float):
    """Yield raw RGB frames zooming from 1.0x to `zoom` around the image centre."""
    # Shrink the 3000px source once; zooming then only crops from this copy.
    src = Image.open(image).convert("RGB")
    work = round(SIZE * zoom)
    src = src.resize((work, work), Image.LANCZOS)

    count = max(1, round(seconds * FPS))
    for i in range(count):
        scale = 1 + (zoom - 1) * ease(i / max(1, count - 1))
        side = work / scale
        left = (work - side) / 2
        box = (left, left, left + side, left + side)
        yield src.resize((SIZE, SIZE), Image.BICUBIC, box=box).tobytes()


def build(folder: Path, out: Path, seconds: float, zoom: float) -> bool:
    images = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    if not images:
        print(f"No images found in {folder} — skipped")
        return False

    plan, seconds = plan_images(images, seconds)
    print(f"{len(images)} images → {len(plan)} slides × {seconds:.2f}s "
          f"= {len(plan) * seconds:.1f}s video")

    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{SIZE}x{SIZE}", "-r", str(FPS),
        "-i", "-",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
        "-preset", "medium", "-movflags", "+faststart",
        str(out),
    ]
    ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for image in plan:
        print(f"  {image.name}")
        for frame in frames_for(image, seconds, zoom):
            ffmpeg.stdin.write(frame)
    ffmpeg.stdin.close()
    if ffmpeg.wait() != 0:
        print(f"ffmpeg failed to write {out}")
        return False

    print(f"Saved {out} ({out.stat().st_size / 1_000_000:.1f} MB)")
    return True


def build_all(garment: str, seconds: float, zoom: float, force: bool) -> None:
    """One video per design folder under output/<garment>/. Existing videos are kept unless --force."""
    root = OUTPUT_DIR / garment
    if not root.is_dir():
        sys.exit(f"Folder not found: {root}")

    made, skipped, failed = [], [], []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        out = VIDEO_DIR / garment / f"{folder.name}.mp4"
        if out.exists() and not force:
            print(f"\n[{folder.name}] video already exists — skipped (use --force to remake)")
            skipped.append(folder.name)
            continue
        print(f"\n[{folder.name}]")
        (made if build(folder, out, seconds, zoom) else failed).append(folder.name)

    print(f"\nDone: {len(made)} made, {len(skipped)} already existed, {len(failed)} failed/empty")
    for name in failed:
        print(f"  ✗ {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Make an Etsy slideshow MP4 from a mockup folder.")
    parser.add_argument("design", nargs="?", help="folder name under output/<garment>/, e.g. agress")
    parser.add_argument("--all", action="store_true", help="make a video for every design folder")
    parser.add_argument("--force", action="store_true", help="with --all: remake videos that already exist")
    parser.add_argument("--garment", default="sweatshirt", help="sweatshirt (default) or tshirt")
    parser.add_argument("--seconds", type=float, default=1.5, help="time per image (default 1.5)")
    parser.add_argument("--zoom", type=float, default=1.15, help="end zoom per image (default 1.15)")
    args = parser.parse_args()

    if args.all:
        build_all(args.garment, args.seconds, args.zoom, args.force)
        return
    if not args.design:
        parser.error("give a design folder name, or use --all")

    folder = OUTPUT_DIR / args.garment / args.design
    if not folder.is_dir():
        sys.exit(f"Folder not found: {folder}")

    if not build(folder, VIDEO_DIR / args.garment / f"{args.design}.mp4", args.seconds, args.zoom):
        sys.exit(1)


if __name__ == "__main__":
    main()
