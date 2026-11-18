"""
add_guide.py — Append an info image to every live T-shirt or sweatshirt listing
================================================================================
Uploads an image from assets/listing_extras/ (default Shirt_Guide.png, the size
chart) as the LAST image of each matching active listing on Etsy, so the
thumbnail and mockups keep their order. The image name picks the garment:
Sweatshirt_* images go to sweatshirts, everything else to t-shirts.

Etsy-only: Printify is never touched, so nothing here can switch a listing off.

T-shirts are picked by title ("T-Shirt" / "Tshirt" / "Tee", but not
sweatshirt / hoodie / crewneck) AND by Etsy category 482 (T-shirts).
Sweatshirts by title ("Sweatshirt" / "Crewneck", but not hoodie / hooded) AND
category 2202 — the Printify Gildan 18000 crewnecks. The three older
non-Printify sweatshirts sit in category 469 and are left alone on purpose:
the sweatshirt images describe a Gildan 18000 crewneck they are not. A listing
where the two disagree is skipped and reported for a human to look at. A listing already at
Etsy's 20-image cap is skipped and reported, never trimmed.

Each upload is recorded in caching/added_<image>.json (one ledger per image),
and a listing in that ledger is never given that image again — so re-running is safe.

USAGE
  python -m scripts.etsy.add_guide                    # dry run — what would change
  python -m scripts.etsy.add_guide --apply --limit 3  # test on 3 listings
  python -m scripts.etsy.add_guide --apply            # every t-shirt
  python -m scripts.etsy.add_guide --apply --only 4513682953
  python -m scripts.etsy.add_guide --apply --image Shirt_Print.png
  python -m scripts.etsy.add_guide --apply --image Sweatshirt_Guide.jpg
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from services.etsy_service import EtsyClient, EtsyError

IMAGE_DIR = Path("assets/listing_extras")
CACHE_DIR = Path("caching")
LOG_FILE = CACHE_DIR / "guide_log.txt"

ETSY_IMAGE_MAX = 20               # live listings here already carry 12-14
TSHIRT_TAXONOMY = 482             # Etsy: Clothing > Tops & Tees > T-shirts
SWEATSHIRT_TAXONOMY = 2202        # Etsy: Sweatshirts (every Printify Gildan 18000)
PAUSE = 1.0                       # seconds between listings — no bursts

TEE = re.compile(r"\bt-?shirts?\b|\btees?\b", re.I)
NOT_TEE = re.compile(r"sweatshirt|hoodie|crewneck", re.I)


SWEAT = re.compile(r"sweatshirt|crewneck", re.I)
NOT_SWEAT = re.compile(r"hood", re.I)


def is_tee(title: str) -> bool:
    return bool(TEE.search(title)) and not NOT_TEE.search(title)


def is_sweatshirt(title: str) -> bool:
    return bool(SWEAT.search(title)) and not NOT_SWEAT.search(title)


# garment -> (title check, Etsy category)
GARMENTS = {
    "t-shirts":    (is_tee, TSHIRT_TAXONOMY),
    "sweatshirts": (is_sweatshirt, SWEATSHIRT_TAXONOMY),
}


def log(line: str):
    print(line)
    CACHE_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually upload")
    ap.add_argument("--limit", type=int, default=0, help="stop after N uploads")
    ap.add_argument("--image", default="Shirt_Guide.png", help="file in assets/listing_extras/")
    ap.add_argument("--only", type=int, nargs="*", help="restrict to these listing ids")
    args = ap.parse_args()

    image = IMAGE_DIR / args.image
    if not image.exists():
        sys.exit(f"Missing {image}")
    LEDGER = CACHE_DIR / f"added_{image.stem}.json"
    ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {}

    garment = "sweatshirts" if image.name.lower().startswith("sweatshirt") else "t-shirts"
    title_ok, taxonomy = GARMENTS[garment]

    etsy = EtsyClient()
    shop_id = etsy.get_shop_id()
    listings = etsy.get_active_listings(shop_id)
    # Title AND Etsy's category must both agree; disagreements are reported.
    tees = []
    for l in listings:
        by_title = title_ok(html.unescape(l.get("title", "")))
        by_cat = l.get("taxonomy_id") == taxonomy
        if by_title and by_cat:
            tees.append(l)
        elif by_title or by_cat:
            log(f"  CHECK {l['listing_id']}  title/category disagree, skipped — "
                f"{html.unescape(l['title'])[:70]}")
    if args.only:
        tees = [l for l in tees if l["listing_id"] in set(args.only)]
    print(f"{len(listings)} active listings, {len(tees)} {garment}. Adding {image.name}.\n")

    done = []
    for l in tees:
        if args.limit and len(done) >= args.limit:
            break
        lid = l["listing_id"]
        title = html.unescape(l["title"])[:70]
        if str(lid) in ledger:
            print(f"  skip {lid}  already has {image.name} — {title}")
            continue
        images = etsy.get_listing_images(lid)
        if len(images) >= ETSY_IMAGE_MAX:
            log(f"  FULL {lid}  {len(images)}/{ETSY_IMAGE_MAX} images, skipped — {title}")
            continue
        rank = len(images) + 1
        if not args.apply:
            print(f"  would add {lid} at image #{rank}  — {title}")
            done.append(lid)
            continue
        try:
            img = etsy.upload_listing_image(shop_id, lid, image, rank)
        except EtsyError as e:
            log(f"  FAIL {lid}  {e}")
            continue
        ledger[str(lid)] = img.get("listing_image_id")
        LEDGER.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        log(f"  added {lid} at image #{rank}  — {title}")
        done.append(lid)
        time.sleep(PAUSE)

    print(f"\n{'Added' if args.apply else 'Would add'}: {len(done)}")
    for lid in done:
        print(f"  https://www.etsy.com/listing/{lid}")


if __name__ == "__main__":
    main()
