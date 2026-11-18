"""
sweatshirt.py — Sweatshirt Pipeline (Upscale → Mockups → Printify)

How to use: 

Step 1:
python -m scripts.create.sweatshirt --clear-ledger

Step 2: 
python -m scripts.create.sweatshirt --skip-upscale

Step 3: 
python -m scripts.create.marketing_sweatshirt --clear-ledger 

Step 4: 
python -m scripts.create.marketing_sweatshirt 





==================================================================
The sweatshirt equivalent of execute.py:

  1. Upscale every new design in assets/designs/ → 4x → assets/designs_final/
  2. For every sweatshirt color template × every design, render a mockup.
     Output is grouped per design:
       output/sweatshirt/{design}/Black_Sweatshirt.png
       output/sweatshirt/{design}/Gray_Sweatshirt.png
       ... one PNG per color template.
  2b. Every BLACK render is also COPIED, flat, into output/final_thumbnail/:
       output/final_thumbnail/{design}_Black_Sweatshirt.png
     These are the ready-to-upload thumbnails for platforms that want one
     hero image per product. It is a copy, not a move — the per-design
     folder keeps its Black_Sweatshirt.png (marketing_sweatshirt.py sends
     that one to Etsy as the rank-1 main image). Re-running overwrites
     them, so output/final_thumbnail/ always holds the newest renders.
  3. Upload each assets/designs_final/ image to Printify and create the product:
       • Unisex Heavy Blend™ Crewneck Sweatshirt — Gildan 18000
       • Fulfilled by Printful, DTF decoration (front)
       • Colors: Sport Grey, Black, Maroon, Sand, Navy, Dark Chocolate
       • Prices: S–XL ₱2,030 · 2XL ₱2,180 · 3XL ₱2,330 · 4XL ₱2,480 · 5XL ₱2,630
     By default products are CREATED in Printify but NOT published to Etsy —
     finalize each in Printify and click "Publish" yourself.
     (Skips designs already in caching/published_sweatshirt.json; needs
     PRINTIFY_API_TOKEN.)

Which thumbnails count as "sweatshirt"?
  Every image inside assets/thumbnails/Sweatshirts/ (one file per Printify
  color, e.g. Black_Sweatshirt.png, Navy_Sweatshirt.png). The t-shirt
  pipeline (execute.py) only scans the top level of assets/thumbnails/, so
  the two pipelines never overlap.

USAGE:
  (every run that renders mockups also refreshes output/final_thumbnail/ — see step 2b)
  python -m scripts.create.sweatshirt               # Upscale → mockups → Printify upload (full pipeline)
  python -m scripts.create.sweatshirt --skip-upscale  # Skip upscaling; use assets/designs_final/ as-is
  python -m scripts.create.sweatshirt --no-publish  # Skip the Printify step entirely (mockups only)
  python -m scripts.create.sweatshirt --publish     # Also auto-publish to Etsy's Drafts tab
  python -m scripts.create.sweatshirt --dry-run     # Printify step: preview only, create nothing
  python -m scripts.create.sweatshirt --setup       # Only run the corner picker (no processing)
  python -m scripts.create.sweatshirt --reupscale   # Force re-upscale (overwrite assets/designs_final/)
  python -m scripts.create.sweatshirt --reset       # Clear saved sweatshirt corners
  python -m scripts.create.sweatshirt --update-listings  # Regenerate AI title/description/tags for
                                     #   every already-created sweatshirt and republish
  python -m scripts.create.sweatshirt --reconcile   # Forget ledger entries deleted on Printify
  python -m scripts.create.sweatshirt --clear-ledger  # Wipe the sweatshirt ledger (re-create ALL next run)
  python -m scripts.create.sweatshirt --forget "name.png" [more...]  # Forget specific design(s)
  python -m scripts.create.sweatshirt --help        # Show this help

CORNERS (one pick for ALL colors):
  All color templates share the same print-area composition, so only ONE
  corner config is needed. It is stored in config.json under
  "sweatshirt_print_area" and scaled automatically to each template's
  pixel size. The legacy corners you picked on the original black
  sweatshirt are migrated automatically — no re-picking needed.
  Only if no config exists at all does the picker open once (on the
  first template): click TL → TR → BR → BL, then ENTER.
"""

from pathlib import Path
import shutil
import sys

from PIL import Image

# Ensure UTF-8 output so emoji / box-drawing chars don't crash on Windows
# (cp1252) consoles or when stdout is piped/redirected.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from services.upscale_service import upscale as upscale_designs, find_binary
from services.mockup_service import pick_corners_interactive, generate_mockups
from services.printify_service import (
    SWEATSHIRT_PROFILE,
    run_printify_upload,
    reconcile_ledger,
    clear_ledger,
    forget_designs,
    update_listings,
)
from scripts.create.execute import (
    gather_files,
    load_config,
    save_config,
    THUMBNAILS_DIR,
    DESIGNS_DIR,
    FINAL_DESIGNS_DIR,
    IMG_EXTS,
)

# Sweatshirt renders land in their own folder so they're easy to review
# in isolation from the t-shirt mockups in output/.
OUTPUT_DIR = Path("output/sweatshirt")

# Every BLACK render is also copied here — flat, one file per design — because
# several platforms want a single ready-to-upload thumbnail image per product
# and black is the agreed "hero" color. Copies, never moves: the per-design
# folder keeps its Black_Sweatshirt.png (marketing_sweatshirt.py uploads that
# one to Etsy as rank-1 main image).
FINAL_THUMBNAIL_DIR = Path("output/final_thumbnail")

# Which color counts as "the" thumbnail — matches the template stem in
# assets/thumbnails/Sweatshirts/Black_Sweatshirt.png.
THUMBNAIL_STEM = "Black_Sweatshirt"

# One template per Printify sweatshirt color lives here
# (e.g. Black_Sweatshirt.png, Navy_Sweatshirt.png, ...).
SWEATSHIRT_THUMBS_DIR = THUMBNAILS_DIR / "Sweatshirts"

# All color templates share one print-area config (same composition,
# only the color differs). Stored once and scaled to each template size.
SHARED_CORNER_KEY = "sweatshirt_print_area"
# Old per-thumbnail config entry from the single-black-sweatshirt era;
# migrated into SHARED_CORNER_KEY on first run.
LEGACY_CORNER_KEY = "Black_Sweatshirt.jpg"


def scale_corners(corners: list, ref_size: tuple, target_size: tuple) -> list:
    """Scale corner points picked at ref_size (w, h) to target_size (w, h)."""
    rw, rh = ref_size
    tw, th = target_size
    if (rw, rh) == (tw, th):
        return [list(pt) for pt in corners]
    return [[int(round(x * tw / rw)), int(round(y * th / rh))] for x, y in corners]


def ensure_shared_corners(config: dict, thumbnails: list[Path]) -> dict | None:
    """
    Return {"corners": [...], "ref_size": [w, h]} for the shared sweatshirt
    print area, creating it if needed:
      1. Already in config under SHARED_CORNER_KEY → use it.
      2. Legacy Black_Sweatshirt.jpg corners in config → migrate them
         (they were picked on the same black template art).
      3. Otherwise → open the picker once on the first template.
    Returns None only if the picker was needed and cancelled.
    """
    shared = config.get(SHARED_CORNER_KEY)
    if shared and shared.get("corners") and shared.get("ref_size"):
        return shared

    legacy = config.get("thumbnails", {}).get(LEGACY_CORNER_KEY, {}).get("corners")
    if legacy:
        # The legacy pick was made on the original black template. Use the
        # matching new template's size as reference (same art, same size);
        # fall back to the first template.
        ref_thumb = next(
            (t for t in thumbnails if t.stem.lower() == "black_sweatshirt"),
            thumbnails[0],
        )
        with Image.open(ref_thumb) as im:
            ref_size = list(im.size)
        shared = {"corners": legacy, "ref_size": ref_size}
        config[SHARED_CORNER_KEY] = shared
        save_config(config)
        print(f"  ♻️  Migrated legacy '{LEGACY_CORNER_KEY}' corners → shared "
              f"sweatshirt print area (ref {ref_size[0]}x{ref_size[1]})")
        return shared

    first = thumbnails[0]
    print(f"\n  ⚙️  No sweatshirt print-area config — pick corners once on "
          f"'{first.name}' (shared by ALL colors)...")
    corners = pick_corners_interactive(first)
    if corners is None:
        return None
    with Image.open(first) as im:
        ref_size = list(im.size)
    shared = {"corners": corners, "ref_size": ref_size}
    config[SHARED_CORNER_KEY] = shared
    save_config(config)
    return shared


def collect_final_thumbnails(designs: list[Path]) -> tuple[int, int]:
    """
    Copy every design's black-sweatshirt render into FINAL_THUMBNAIL_DIR.

      output/sweatshirt/<design>/Black_Sweatshirt.png
        → output/final_thumbnail/<design>_Black_Sweatshirt.png

    Flat and named after the design so the files stay unique and obvious when
    uploaded elsewhere. Existing files are overwritten, so re-running always
    leaves the freshest render there.

    Returns (copied, missing).
    """
    FINAL_THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
    copied = 0
    missing: list[str] = []

    for design_path in designs:
        src = OUTPUT_DIR / design_path.stem / f"{THUMBNAIL_STEM}.png"
        if not src.exists():
            missing.append(design_path.stem)
            continue
        dst = FINAL_THUMBNAIL_DIR / f"{design_path.stem}_{THUMBNAIL_STEM}.png"
        shutil.copy2(src, dst)
        copied += 1

    print(f"\n  🖤 Black thumbnails → {FINAL_THUMBNAIL_DIR}/  "
          f"({copied} copied)")
    if missing:
        print(f"     ⚠️  No {THUMBNAIL_STEM}.png render for: {', '.join(missing)}")
        print(f"        (is assets/thumbnails/Sweatshirts/{THUMBNAIL_STEM}.png present?)")

    return copied, len(missing)


def reset_sweatshirt_corners():
    """Clear the shared sweatshirt print-area config (tee corners kept)."""
    config = load_config()
    removed = []
    if config.pop(SHARED_CORNER_KEY, None) is not None:
        removed.append(SHARED_CORNER_KEY)
    thumbs = config.get("thumbnails", {})
    legacy = [name for name in thumbs if "sweatshirt" in name.lower()]
    for name in legacy:
        del thumbs[name]
    removed.extend(legacy)
    save_config(config)
    if removed:
        print(f"🔄  Cleared: {', '.join(removed)}. Re-pick on next run.")
    else:
        print("ℹ️  No sweatshirt corner configs found — nothing to clear.")


def run(
    setup_only: bool = False,
    skip_upscale: bool = False,
    force_reupscale: bool = False,
    no_publish: bool = False,
    publish: bool = False,
    dry_run: bool = False,
    only: list[str] | None = None,
):
    for d in [SWEATSHIRT_THUMBS_DIR, DESIGNS_DIR, FINAL_DESIGNS_DIR, OUTPUT_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    raw_before      = gather_files(DESIGNS_DIR)
    upscaled_before = gather_files(FINAL_DESIGNS_DIR)
    thumbnails      = gather_files(SWEATSHIRT_THUMBS_DIR)
    binary          = find_binary()

    print("\n" + "═" * 56)
    print("  🧥  Sweatshirt Pipeline  (Gildan 18000 · Printful · DTF)")
    print("═" * 56)
    print(f"\n  📊 Pre-flight scan:")
    print(f"     {SWEATSHIRT_THUMBS_DIR}/ → {len(thumbnails)} color template(s)")
    print(f"     assets/designs/         → {len(raw_before)} file(s)  (raw input)")
    print(f"     assets/designs_final/   → {len(upscaled_before)} file(s)  (upscaled, mockup source)")
    print(f"     Real-ESRGAN bin  → {binary if binary else '❌ NOT FOUND'}")

    if not thumbnails:
        print(f"\n❌  No sweatshirt templates found in '{SWEATSHIRT_THUMBS_DIR}/'.")
        print(f"    Add one image per color (e.g. Black_Sweatshirt.png,")
        print(f"    Navy_Sweatshirt.png) and re-run.\n")
        sys.exit(1)

    if not raw_before and not upscaled_before:
        print(f"\n❌  Both 'assets/designs/' and 'assets/designs_final/' are empty.")
        print(f"    Drop your raw designs (.png/.jpg/.jpeg/.webp) into 'assets/designs/' and re-run.\n")
        sys.exit(1)

    # ── Step 1: Upscale (same shared source folders as the tee pipeline) ─────
    if not skip_upscale:
        upscale_designs(
            designs_dir=DESIGNS_DIR,
            output_dir=FINAL_DESIGNS_DIR,
            img_exts=IMG_EXTS,
            force=force_reupscale,
        )
    else:
        print(f"\n  ⏭️   Upscale step skipped (--skip-upscale)")

    designs = gather_files(FINAL_DESIGNS_DIR)
    if not designs:
        print(f"\n❌  'assets/designs_final/' is empty — cannot generate mockups.")
        if binary is None:
            print(f"    ⚠️  Real-ESRGAN binary is NOT installed in ./bin/realesrgan/.")
            print(f"       Download: https://github.com/xinntao/Real-ESRGAN/releases\n")
        sys.exit(1)
    if only is not None:   # an empty list means "nothing", never "everything"
        wanted  = {n.lower() for n in only}
        designs = [d for d in designs if d.name.lower() in wanted]
        print(f"\n  🎯 --only: limited to {len(designs)} design(s): "
              f"{', '.join(d.name for d in designs) or 'none found'}")
        if not designs:
            sys.exit(1)

    # ── Ensure the ONE shared print-area config exists ────────────────────────
    config = load_config()
    shared = ensure_shared_corners(config, thumbnails)
    if shared is None:
        print(f"\n❌  Corner picking cancelled — cannot generate mockups.\n")
        sys.exit(1)

    if setup_only:
        print(f"\n  ℹ️  --setup mode: corners are ready. Exiting without processing.\n")
        return

    # ── Step 2: Mockups ───────────────────────────────────────────────────────
    # Scale the shared corners to each color template's pixel size.
    ref_size    = tuple(shared["ref_size"])
    corners_map = {}
    for thumb_path in thumbnails:
        with Image.open(thumb_path) as im:
            corners_map[thumb_path.name] = scale_corners(
                shared["corners"], ref_size, im.size
            )

    processed, skipped = generate_mockups(
        thumbnails=thumbnails,
        designs=designs,
        corners_map=corners_map,
        output_dir=OUTPUT_DIR,
        group_by_design=True,
    )

    # ── Step 2b: Collect the black renders as platform thumbnails ─────────────
    thumbs_copied, _ = collect_final_thumbnails(designs)

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{'═' * 56}")
    print(f"  🎉 Done!  {processed} sweatshirt mockup(s) created", end="")
    if skipped:
        print(f",  {skipped} skipped/failed", end="")
    print(f"\n  📁 Output layout: {OUTPUT_DIR}/<design>/<Color>_Sweatshirt.png")
    print(f"     (one folder per design, one mockup per color template)")
    print(f"  🖤 Thumbnails:    {FINAL_THUMBNAIL_DIR}/<design>_{THUMBNAIL_STEM}.png"
          f"  ({thumbs_copied} file(s))")

    # ── Step 3: Printify upload ───────────────────────────────────────────────
    if no_publish:
        print(f"\n  ⏭️   Printify step skipped (--no-publish)\n")
        return

    run_printify_upload(designs, dry_run=dry_run, publish=publish,
                        profile=SWEATSHIRT_PROFILE)


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    if "--reset" in args:
        reset_sweatshirt_corners()
        if len(args) == 1:
            sys.exit(0)

    # ── Ledger maintenance (caching/published_sweatshirt.json) ────────────────
    if "--update-listings" in args:
        update_listings(FINAL_DESIGNS_DIR, profile=SWEATSHIRT_PROFILE)
        sys.exit(0)

    if "--reconcile" in args:
        reconcile_ledger(profile=SWEATSHIRT_PROFILE)
        sys.exit(0)

    if "--clear-ledger" in args:
        clear_ledger(profile=SWEATSHIRT_PROFILE)
        sys.exit(0)

    if "--forget" in args:
        # Usage: python -m scripts.create.sweatshirt --forget "break.png" "retrying_my_prompt.png"
        names = [a for a in args[args.index("--forget") + 1:] if not a.startswith("--")]
        if not names:
            print("⚠️  --forget needs at least one design file name, e.g.:")
            print('    python -m scripts.create.sweatshirt --forget "break.png"')
        else:
            forget_designs(names, profile=SWEATSHIRT_PROFILE)
        sys.exit(0)

    run(
        setup_only="--setup" in args,
        skip_upscale="--skip-upscale" in args,
        force_reupscale="--reupscale" in args,
        no_publish="--no-publish" in args,
        publish="--publish" in args,
        dry_run="--dry-run" in args,
    )
