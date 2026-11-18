"""
listing_extras.py — Info images + video for ONE known Etsy listing
===================================================================
The one-shot pipelines (tshirt_automation / sweatshirt_automation) already know
the exact listing id a design landed on, so they don't need video_pipeline's
photo search. This module gives them the two finishing touches every older
listing already has:

  1. INFO IMAGES — appended as the LAST images, after our mockups:
       tee:        Shirt_Guide.png (size) · Shirt_Print.png (DTG) · Shirt_Chart.png (color)
       sweatshirt: Sweatshirt_Guide.jpg (size) · Sweatshirt_Printed.png (DTF) · Sweatshirt_Chart.png (color)
  2. VIDEO       — rebuilt fresh from output/<garment>/<design>/ (the mockups
                   that just went on the listing) and uploaded.

Both write the SAME ledgers add_guide.py and video_pipeline.py use
(caching/added_<image>.json, caching/added_video.json), so nothing is ever added
twice whichever script gets there first. Etsy-only: Printify is never touched.

Not run on its own — use `--extras-only` on either automation to backfill
listings that are already live.
"""

from __future__ import annotations

import io
import json
import time
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image

from services.etsy_service import EtsyClient, EtsyError
from scripts.etsy.add_guide import IMAGE_DIR, ETSY_IMAGE_MAX, CACHE_DIR, log
from scripts.etsy.add_video import LEDGER as VIDEO_LEDGER, MATCH_MAX, fingerprint, distance

# In upload order — the size guide sits right after the mockups.
INFO_IMAGES = {
    "tshirt":     ["Shirt_Guide.png", "Shirt_Print.png", "Shirt_Chart.png"],
    "sweatshirt": ["Sweatshirt_Guide.jpg", "Sweatshirt_Printed.png", "Sweatshirt_Chart.png"],
}
PAUSE = 1.0


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def add_info_images(etsy: EtsyClient, shop_id: int, listing_id: int,
                    garment: str, dry_run: bool = False) -> int:
    """Append the garment's info images that the listing doesn't have yet. Returns # added."""
    added = planned = 0          # planned: dry-run uploads, so the ranks read right
    for name in INFO_IMAGES[garment]:
        image = IMAGE_DIR / name
        ledger_path = CACHE_DIR / f"added_{image.stem}.json"
        ledger = _load(ledger_path)
        if str(listing_id) in ledger:
            continue
        if not image.exists():
            print(f"     ⚠️  {image} is missing — skipped.")
            continue
        count = len(etsy.get_listing_images(listing_id)) + planned
        if count >= ETSY_IMAGE_MAX:
            print(f"     ⚠️  Listing already has {count}/{ETSY_IMAGE_MAX} images — {name} skipped.")
            break
        if dry_run:
            print(f"     🔍 Would add {name} at image #{count + 1}")
            planned += 1
            continue
        img = etsy.upload_listing_image(shop_id, listing_id, image, count + 1)
        ledger[str(listing_id)] = img.get("listing_image_id")
        _save(ledger_path, ledger)
        log(f"  added {listing_id} at image #{count + 1}  — {name}")
        added += 1
        time.sleep(PAUSE)
    if not dry_run:
        print(f"     📐 Info images: {added} added{'' if added else ' (already on the listing)'}.")
    return added


def _photos_match(etsy: EtsyClient, listing_id: int, folder: Path) -> tuple[bool, str]:
    """Does the listing's main photo look like the mockups in `folder`?

    Design file names get reused for different artwork, so a backfill must not
    trust the name alone. One download, same fingerprint as video_pipeline.
    """
    images = sorted(etsy.get_listing_images(listing_id), key=lambda i: i.get("rank", 0))
    if not images:
        return False, "listing has no photos"
    resp = requests.get(images[0]["url_570xN"], timeout=30)
    resp.raise_for_status()
    remote = fingerprint(Image.open(io.BytesIO(resp.content)))
    local = [fingerprint(Image.open(p)) for p in sorted(folder.glob("*.png"))]
    if not local:
        return False, f"no mockups in {folder}"
    best = min(distance(remote, fp) for fp in local)
    return best <= MATCH_MAX, f"photo distance {best}"


def add_video(etsy: EtsyClient, shop_id: int, listing_id: int, garment: str,
              design: str, dry_run: bool = False, verify_photos: bool = False) -> bool:
    """Build a fresh slideshow from output/<garment>/<design stem>/ and upload it.

    `verify_photos` is for backfills: the folder may have been re-rendered for
    another design since. A run that JUST uploaded these mockups can skip it.
    """
    # Imported here: video_pipeline pulls in the mockup renderer and Printify.
    from scripts.etsy.video_pipeline import make_video

    stem = Path(design).stem
    ledger = _load(VIDEO_LEDGER)
    if str(listing_id) in ledger:
        print("     🎬 Video: already added (ledger).")
        return False
    if etsy.get_listing_videos(listing_id):
        print("     🎬 Video: listing already has one.")
        return False
    folder = Path("output") / garment / stem
    if not folder.is_dir():
        print(f"     ⚠️  Video skipped: no mockups in {folder}/.")
        return False
    if verify_photos:
        ok, why = _photos_match(etsy, listing_id, folder)
        if not ok:
            print(f"     ⚠️  Video skipped: listing photos don't match {folder}/ ({why}).")
            return False
    if dry_run:
        print(f"     🔍 Would build + upload video from {folder}/")
        return False

    video = make_video(garment, stem)
    if video is None:
        print("     ⚠️  Video could not be built — skipped.")
        return False
    v = etsy.upload_listing_video(shop_id, listing_id, video)
    ledger[str(listing_id)] = {"design": stem, "garment": garment,
                               "video_id": v.get("video_id"),
                               "uploaded_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"}
    _save(VIDEO_LEDGER, ledger)
    log(f"  added video {garment}/{stem}.mp4 → {listing_id}")
    print(f"     🎬 Video uploaded (Etsy takes a few minutes to process it).")
    return True


def backfill(garment: str, records: list[dict], dry_run: bool = False,
             images: bool = True, video: bool = True) -> None:
    """`--extras-only`: finish listings an automation already took live.

    Every record is a row of caching/pinned_<garment>.json. Photos are verified
    before a video goes up, because the mockup folder may have been re-rendered
    for a different design with the same file name since.
    """
    print("\n" + "═" * 60)
    print(f"  🎬  Backfilling info images + video on live {garment} listings")
    print("═" * 60)
    if dry_run:
        print("  🔍 DRY RUN — nothing is uploaded.")
    if not records:
        print("\n  ℹ️  No live listings in the ledger — nothing to do.\n")
        return
    etsy = EtsyClient()
    shop_id = etsy.get_shop_id()
    for r in records:
        listing_id = int(r["etsy_listing_id"])
        print(f"\n  ──────── {r['design']}  ({listing_id}) ────────")
        state = etsy.get_listing(listing_id).get("state")
        if state != "active":
            print(f"     ⏭️  Listing is '{state}', not active — skipped.")
            continue
        add_extras(etsy, shop_id, listing_id, garment, r["design"], dry_run,
                   verify_photos=True, images=images, video=video)
    print()


def add_extras(etsy: EtsyClient, shop_id: int, listing_id: int, garment: str,
               design: str, dry_run: bool = False, verify_photos: bool = False,
               images: bool = True, video: bool = True) -> None:
    """Both finishing touches. Never raises an Etsy error: the listing is
    already live either way, and `--extras-only` retries whatever is missing."""
    if images:
        try:
            add_info_images(etsy, shop_id, listing_id, garment, dry_run)
        except EtsyError as e:
            print(f"     ⚠️  Info images failed ({e}) — re-run with --extras-only.")
    if video:
        try:
            add_video(etsy, shop_id, listing_id, garment, design, dry_run, verify_photos)
        except (EtsyError, requests.RequestException) as e:
            print(f"     ⚠️  Video failed ({e}) — re-run with --extras-only.")
