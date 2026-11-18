"""
add_video.py — Put each slideshow MP4 on the Etsy sweatshirt listing it belongs to
===================================================================================
Videos come from output/video/sweatshirt/<design>.mp4 (made by
scripts.create.slideshow). The matching listing is found LIVE — never from the
local publish ledgers, which get cleaned up:

  1. PRINTIFY  — every sweatshirt product (blueprint 49) stores the design file
                 it prints (e.g. "agress.png") and the Etsy listing it is
                 published to. Folder name = design file name → listing id.
  2. PICTURES  — the listing's photos on Etsy are compared with the mockups in
                 output/sweatshirt/<design>/. The design must be the closest
                 match of ALL folders, and a near-exact one. A listing whose
                 photos point at another design (or none) is skipped.
  3. ETSY      — the listing must be active and must not already have a video.

Only listings that pass all three are uploaded. Everything else is reported.
Printify is read, never written, so nothing here can switch a listing off.

Each upload is recorded in caching/added_video.json; a listing in there is never
given a video again — re-running is safe.

USAGE
  python -m scripts.etsy.add_video                     # dry run — the match table
  python -m scripts.etsy.add_video --apply --only agress   # test one listing
  python -m scripts.etsy.add_video --apply             # every matched listing
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from PIL import Image

from services.etsy_service import EtsyClient, EtsyError
from services.printify_service import PrintifyClient
from scripts.printify.reprice import all_products, resolve_shop_id

VIDEO_DIR = Path("output/video/sweatshirt")
MOCKUP_DIR = Path("output/sweatshirt")
CACHE_DIR = Path("caching")
LEDGER = CACHE_DIR / "added_video.json"
LOG_FILE = CACHE_DIR / "video_log.txt"

SWEATSHIRT_BLUEPRINT = 49         # Printify: Gildan 18000 crewneck
MATCH_MAX = 8                     # picture distance for "same mockup" (exact = 0, other designs ≥ 20)
PAUSE = 1.0                       # seconds between uploads — no bursts


def log(line: str):
    print(line)
    CACHE_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")


# ── Picture check ──────────────────────────────────────────────────────────
def fingerprint(im: Image.Image) -> list[bool]:
    """16x16 brightness pattern of the chest print. Every mockup shares the same
    garment and background, so only the centre (the design) tells them apart."""
    im = im.convert("L")
    w, h = im.size
    small = im.crop((w * .3, h * .25, w * .7, h * .7)).resize((16, 16), Image.BILINEAR)
    px = small.tobytes()
    mean = sum(px) / len(px)
    return [p > mean for p in px]


def distance(a: list[bool], b: list[bool]) -> int:
    return sum(x != y for x, y in zip(a, b))


def local_fingerprints() -> dict[str, list[list[bool]]]:
    out = {}
    for folder in sorted(p for p in MOCKUP_DIR.iterdir() if p.is_dir()):
        images = sorted(folder.glob("*.png")) + sorted(folder.glob("*.jpg"))
        if images:
            out[folder.name] = [fingerprint(Image.open(p)) for p in images]
    return out


def closest_design(etsy: EtsyClient, listing_id: int, local: dict) -> tuple[str, int]:
    """Which local design folder the listing's Etsy photos look like, and how close."""
    remote = []
    for img in etsy.get_listing_images(listing_id):
        resp = requests.get(img["url_570xN"], timeout=30)
        resp.raise_for_status()
        remote.append(fingerprint(Image.open(io.BytesIO(resp.content))))
    if not remote:
        return "", 999
    scores = {name: min(distance(a, b) for a in remote for b in fps)
              for name, fps in local.items()}
    best = min(scores, key=scores.get)
    return best, scores[best]


# ── Printify: design file name → Etsy listing ids ──────────────────────────
def printify_listings() -> dict[str, list[int]]:
    client = PrintifyClient(os.environ["PRINTIFY_API_TOKEN"])
    by_design: dict[str, list[int]] = {}
    for p in all_products(client, resolve_shop_id(client)):
        if p.get("blueprint_id") != SWEATSHIRT_BLUEPRINT:
            continue
        listing_id = (p.get("external") or {}).get("id")
        if not listing_id:
            continue                                  # never published to Etsy
        names = {Path(i["name"]).stem
                 for area in p.get("print_areas", [])
                 for ph in area.get("placeholders", [])
                 for i in ph.get("images", [])}
        for name in names:
            by_design.setdefault(name, []).append(int(listing_id))
    return by_design


def main():
    ap = argparse.ArgumentParser(description="Upload slideshow videos to their Etsy sweatshirt listings.")
    ap.add_argument("--apply", action="store_true", help="actually upload (default: dry run)")
    ap.add_argument("--only", nargs="*", help="restrict to these design names, e.g. agress")
    ap.add_argument("--limit", type=int, default=0, help="stop after N uploads")
    args = ap.parse_args()

    videos = sorted(VIDEO_DIR.glob("*.mp4"))
    if args.only:
        videos = [v for v in videos if v.stem in set(args.only)]
    if not videos:
        sys.exit(f"No videos to upload in {VIDEO_DIR}")
    ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {}

    print("Reading Printify sweatshirts…")
    by_design = printify_listings()
    print("Fingerprinting local mockups…")
    local = local_fingerprints()
    etsy = EtsyClient()
    shop_id = etsy.get_shop_id()

    print(f"\n{len(videos)} videos. {'UPLOADING' if args.apply else 'DRY RUN — nothing is uploaded'}.\n")
    ready, skipped, done = [], [], []
    for video in videos:
        design = video.stem
        candidates = by_design.get(design, [])
        if not candidates:
            skipped.append((design, "-", "no published Printify sweatshirt uses this design"))
            continue
        for lid in candidates:
            if str(lid) in ledger:
                skipped.append((design, lid, "already given a video (ledger)"))
                continue
            listing = etsy.get_listing(lid)
            title = html.unescape(listing.get("title", ""))[:60]
            if listing.get("state") != "active":
                skipped.append((design, lid, f"listing is {listing.get('state')}"))
                continue
            best, score = closest_design(etsy, lid, local)
            if best != design or score > MATCH_MAX:
                skipped.append((design, lid, f"photos look like '{best}' (distance {score}), not this design"))
                continue
            if etsy.get_listing_videos(lid):
                skipped.append((design, lid, "listing already has a video"))
                continue
            ready.append((design, lid, title, video))

    print(f"{'DESIGN':34} {'LISTING':11} LIVE ETSY TITLE")
    for design, lid, title, _ in ready:
        print(f"✅ {design:32} {lid:<11} {title}")
    for design, lid, why in skipped:
        print(f"⏭️  {design:32} {str(lid):11} skipped — {why}")

    if not args.apply:
        print(f"\nWould upload: {len(ready)}   Skipped: {len(skipped)}")
        print("Run with --apply --only agress to test one first.")
        return

    for design, lid, title, video in ready:
        if args.limit and len(done) >= args.limit:
            break
        try:
            v = etsy.upload_listing_video(shop_id, lid, video)
        except EtsyError as e:
            log(f"  FAIL {lid}  {design}  {e}")
            continue
        ledger[str(lid)] = {"design": design, "video_id": v.get("video_id"),
                            "uploaded_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"}
        LEDGER.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        log(f"  added video {design}.mp4 → {lid}  — {title}")
        done.append(lid)
        time.sleep(PAUSE)

    print(f"\nUploaded: {len(done)} (Etsy takes a few minutes to process each video)")
    for lid in done:
        print(f"  https://www.etsy.com/listing/{lid}")


if __name__ == "__main__":
    main()
