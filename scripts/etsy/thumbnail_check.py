"""
thumbnail_check.py — Judge the listing thumbnails at the size buyers actually see
=================================================================================
A mockup gets evaluated on a monitor at 1000+ pixels. Etsy shows it in a search
grid at roughly 230 px, on a white page, surrounded by 40-odd competitors. Those
are different pictures, and a mockup can be genuinely well made at full size and
still be invisible at grid size.

This downloads the rank-1 image of every live listing, rebuilds it at grid size,
and measures the two things that decide whether anyone clicks:

  CONTRAST vs the GRID — Etsy's results page is white. A dark garment on a dark
    prop merges into one low-contrast block, and the eye slides past it. Measured
    as the mean luminance of the tile against Etsy's white background.

It also writes a contact sheet: every thumbnail at true size, laid out on white
the way the search page shows them. That picture is the actual argument.

USAGE
  python -m scripts.etsy.thumbnail_check                 # all 29
  python -m scripts.etsy.thumbnail_check --only 4513682953
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import requests
from PIL import Image

from scripts.etsy.retag import CATALOGUE, LISTING_TO_DESIGN
from services.etsy_service import EtsyClient, EtsyError

OUT = Path("caching") / "thumbs"

# Etsy renders search results at roughly this width on a desktop grid.
GRID_PX = 230

# Below this, a garment tile reads as a dark block on Etsy's white page.
DARK_TILE_LUMA = 90


def mean_luma(img: Image.Image) -> float:
    g = img.convert("L").resize((64, 64))
    px = list(g.getdata())
    return sum(px) / len(px)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int)
    args = ap.parse_args()

    client = EtsyClient()
    ids = [args.only] if args.only else sorted(LISTING_TO_DESIGN)
    OUT.mkdir(parents=True, exist_ok=True)

    rows, tiles = [], []
    for lid in ids:
        try:
            images = client.get_listing_images(lid)
        except EtsyError as exc:
            print(f"  ✗ {lid}: {exc}")
            continue
        if not images:
            print(f"  ✗ {lid}: no images")
            continue

        first = sorted(images, key=lambda i: i.get("rank", 99))[0]
        url = (first.get("url_fullxfull") or first.get("url_570xN")
               or first.get("url_680x540"))
        try:
            raw = requests.get(url, timeout=60).content
            full = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception as exc:                       # network or decode
            print(f"  ✗ {lid}: could not fetch image — {exc}")
            continue

        grid = full.resize((GRID_PX, GRID_PX), Image.LANCZOS)
        grid.save(OUT / f"{lid}_grid.png")

        key, garment = LISTING_TO_DESIGN[lid]
        rows.append((lid, key, garment, mean_luma(grid), len(images)))
        tiles.append((lid, grid))

    # ── contact sheet: what the search grid actually looks like ─────────────
    if tiles:
        cols = 6
        pad, label = 14, 16
        rows_n = (len(tiles) + cols - 1) // cols
        sheet = Image.new("RGB",
                          (cols * (GRID_PX + pad) + pad,
                           rows_n * (GRID_PX + pad + label) + pad),
                          "white")
        for i, (_lid, tile) in enumerate(tiles):
            x = pad + (i % cols) * (GRID_PX + pad)
            y = pad + (i // cols) * (GRID_PX + pad + label)
            sheet.paste(tile, (x, y))
        sheet.save(OUT / "search_grid.png")

    print()
    print("=" * 86)
    print(f"  Thumbnails at Etsy grid size ({GRID_PX}px)")
    print("=" * 86)
    print(f"  {'listing':<12}{'design':<20}{'garment':<12}{'luma':>7}"
          f"{'imgs':>6}  flags")
    print("-" * 86)
    for lid, key, garment, luma, n in sorted(rows, key=lambda r: r[3]):
        flags = []
        if luma < DARK_TILE_LUMA:
            flags.append("DARK TILE — merges into the grid")
        if n < 5:
            flags.append(f"only {n} images")
        print(f"  {lid:<12}{key:<20}{garment:<12}{luma:>7.0f}"
              f"{n:>6}  {', '.join(flags)}")
    print("-" * 86)
    if rows:
        lumas = sorted(r[3] for r in rows)
        print(f"  median tile luma   : {lumas[len(lumas)//2]:.0f}  "
              f"(Etsy's results page is 255)")
        print(f"  tiles below {DARK_TILE_LUMA} luma : "
              f"{sum(1 for r in rows if r[3] < DARK_TILE_LUMA)} / {len(rows)}")
    print("=" * 86)
    print(f"\n  contact sheet: {OUT / 'search_grid.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
