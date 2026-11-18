"""
tshirt.py — T-Shirt Pipeline (Upscale → Mockups → Printify)
==================================================================
The t-shirt twin of sweatshirt.py, used by tshirt_automation.py:

  1. Upscale every new design in assets/designs/ → 4x → assets/designs_final/
  2. For every t-shirt color template × every design, render a mockup:
       output/tshirt/{design}/Black_Shirt.png
       output/tshirt/{design}/Gray_Shirt.png  ... one PNG per color template
  2b. Copy every BLACK render, flat, into output/final_thumbnail/:
       output/final_thumbnail/{design}_Black_Shirt.png   (the Pinterest pin image)
  3. Create the product on Printify with TEE_PROFILE
     (Bella+Canvas 3001 · Monster Digital · Black, Dark Grey, Maroon, Navy, Brown).

Templates: every image in assets/thumbnails/Shirts/ (one per color).

CORNERS (one pick for ALL colors):
  Stored once in config.json under "tshirt_print_area" and scaled to each
  template. The corners already saved for Black_Shirt.png are migrated
  automatically — the picker only opens if neither exists.

USAGE:
  python -m scripts.create.tshirt                 # Upscale → mockups → Printify create (NOT published)
  python -m scripts.create.tshirt --skip-upscale  # Use assets/designs_final/ as-is
  python -m scripts.create.tshirt --no-publish    # Mockups only, skip Printify
  python -m scripts.create.tshirt --publish       # Also publish to Etsy's Drafts tab
  python -m scripts.create.tshirt --dry-run       # Printify step: preview only
  python -m scripts.create.tshirt --setup         # Only run the corner picker
  python -m scripts.create.tshirt --reset         # Clear the shared t-shirt corners
  python -m scripts.create.tshirt --clear-ledger  # Wipe ALL t-shirt ledgers (published, marketed,
                                   #   pinned) so every design is redone next run
  python -m scripts.create.tshirt --help
"""

from pathlib import Path
import shutil
import sys

from PIL import Image

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from services.upscale_service import upscale as upscale_designs, find_binary
from services.mockup_service import pick_corners_interactive, generate_mockups
from services.printify_service import (
    TEE_PROFILE,
    run_printify_upload,
    clear_ledger,
    _load_json,
    _save_json,
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
from scripts.create.sweatshirt import scale_corners

OUTPUT_DIR          = Path("output/tshirt")
FINAL_THUMBNAIL_DIR = Path("output/final_thumbnail")
THUMBNAIL_STEM      = "Black_Shirt"              # the hero color
TSHIRT_THUMBS_DIR   = THUMBNAILS_DIR / "Shirts"
SHARED_CORNER_KEY   = "tshirt_print_area"
LEGACY_CORNER_KEY   = "Black_Shirt.png"          # picked by execute.py


def ensure_shared_corners(config: dict, thumbnails: list[Path]) -> dict | None:
    """{"corners", "ref_size"} for the tee print area: saved → migrated from
    Black_Shirt.png → picked once. None only if the picker was cancelled."""
    shared = config.get(SHARED_CORNER_KEY)
    if shared and shared.get("corners") and shared.get("ref_size"):
        return shared

    legacy = config.get("thumbnails", {}).get(LEGACY_CORNER_KEY, {}).get("corners")
    if legacy:
        ref_thumb = next((t for t in thumbnails if t.stem.lower() == "black_shirt"),
                         thumbnails[0])
        with Image.open(ref_thumb) as im:
            ref_size = list(im.size)
        shared = {"corners": legacy, "ref_size": ref_size}
        config[SHARED_CORNER_KEY] = shared
        save_config(config)
        print(f"  ♻️  Migrated '{LEGACY_CORNER_KEY}' corners → shared t-shirt "
              f"print area (ref {ref_size[0]}x{ref_size[1]})")
        return shared

    first = thumbnails[0]
    print(f"\n  ⚙️  No t-shirt print-area config — pick corners once on "
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


def collect_final_thumbnails(designs: list[Path]) -> int:
    """Copy output/tshirt/<design>/Black_Shirt.png → output/final_thumbnail/<design>_Black_Shirt.png."""
    FINAL_THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
    copied = 0
    for design_path in designs:
        src = OUTPUT_DIR / design_path.stem / f"{THUMBNAIL_STEM}.png"
        if src.exists():
            shutil.copy2(src, FINAL_THUMBNAIL_DIR / f"{design_path.stem}_{THUMBNAIL_STEM}.png")
            copied += 1
        else:
            print(f"     ⚠️  No {THUMBNAIL_STEM}.png render for {design_path.stem}")
    print(f"\n  🖤 Black thumbnails → {FINAL_THUMBNAIL_DIR}/  ({copied} copied)")
    return copied


def run(setup_only: bool = False, skip_upscale: bool = False,
        force_reupscale: bool = False, no_publish: bool = False,
        publish: bool = False, dry_run: bool = False,
        only: list[str] | None = None):
    for d in [TSHIRT_THUMBS_DIR, DESIGNS_DIR, FINAL_DESIGNS_DIR, OUTPUT_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    raw_before      = gather_files(DESIGNS_DIR)
    upscaled_before = gather_files(FINAL_DESIGNS_DIR)
    thumbnails      = gather_files(TSHIRT_THUMBS_DIR)
    binary          = find_binary()

    print("\n" + "═" * 56)
    print("  👕  T-Shirt Pipeline  (Bella+Canvas 3001 · Monster Digital)")
    print("═" * 56)
    print(f"\n  📊 Pre-flight scan:")
    print(f"     {TSHIRT_THUMBS_DIR}/ → {len(thumbnails)} color template(s)")
    print(f"     assets/designs/         → {len(raw_before)} file(s)  (raw input)")
    print(f"     assets/designs_final/   → {len(upscaled_before)} file(s)  (upscaled, mockup source)")
    print(f"     Real-ESRGAN bin  → {binary if binary else '❌ NOT FOUND'}")

    if not thumbnails:
        print(f"\n❌  No t-shirt templates found in '{TSHIRT_THUMBS_DIR}/'.\n")
        sys.exit(1)
    if not raw_before and not upscaled_before:
        print(f"\n❌  Both 'assets/designs/' and 'assets/designs_final/' are empty.\n")
        sys.exit(1)

    # ── Step 1: Upscale ───────────────────────────────────────────────────────
    if not skip_upscale:
        upscale_designs(designs_dir=DESIGNS_DIR, output_dir=FINAL_DESIGNS_DIR,
                        img_exts=IMG_EXTS, force=force_reupscale)
    else:
        print(f"\n  ⏭️   Upscale step skipped (--skip-upscale)")

    designs = gather_files(FINAL_DESIGNS_DIR)
    if not designs:
        print(f"\n❌  'assets/designs_final/' is empty — cannot generate mockups.\n")
        sys.exit(1)
    if only is not None:   # an empty list means "nothing", never "everything"
        wanted  = {n.lower() for n in only}
        designs = [d for d in designs if d.name.lower() in wanted]
        print(f"\n  🎯 --only: limited to {len(designs)} design(s): "
              f"{', '.join(d.name for d in designs) or 'none found'}")
        if not designs:
            sys.exit(1)

    config = load_config()
    shared = ensure_shared_corners(config, thumbnails)
    if shared is None:
        print(f"\n❌  Corner picking cancelled — cannot generate mockups.\n")
        sys.exit(1)
    if setup_only:
        print(f"\n  ℹ️  --setup mode: corners are ready. Exiting without processing.\n")
        return

    # ── Step 2: Mockups ───────────────────────────────────────────────────────
    ref_size    = tuple(shared["ref_size"])
    corners_map = {}
    for thumb_path in thumbnails:
        with Image.open(thumb_path) as im:
            corners_map[thumb_path.name] = scale_corners(shared["corners"], ref_size, im.size)

    processed, skipped = generate_mockups(
        thumbnails=thumbnails,
        designs=designs,
        corners_map=corners_map,
        output_dir=OUTPUT_DIR,
        group_by_design=True,
    )
    thumbs_copied = collect_final_thumbnails(designs)

    print(f"\n{'═' * 56}")
    print(f"  🎉 Done!  {processed} t-shirt mockup(s) created"
          + (f",  {skipped} skipped/failed" if skipped else ""))
    print(f"  📁 Output layout: {OUTPUT_DIR}/<design>/<Color>_Shirt.png")
    print(f"  🖤 Thumbnails:    {FINAL_THUMBNAIL_DIR}/<design>_{THUMBNAIL_STEM}.png"
          f"  ({thumbs_copied} file(s))")

    # ── Step 3: Printify ──────────────────────────────────────────────────────
    if no_publish:
        print(f"\n  ⏭️   Printify step skipped (--no-publish)\n")
        return
    run_printify_upload(designs, dry_run=dry_run, publish=publish, profile=TEE_PROFILE)


def clear_all_ledgers():
    """Wipe every t-shirt ledger so the next run redoes ALL designs from scratch:
    published.json (Printify create), marketed_tshirt.json (mockup swap) and
    pinned_tshirt.json (live + pinned). Clearing only the first would leave
    tshirt_automation.py thinking the design is already finished."""
    from scripts.create import tshirt_automation   # imported here: tshirt_automation imports this module

    clear_ledger(profile=TEE_PROFILE)
    n = len(_load_json(tshirt_automation.MARKETED_LEDGER, {"products": []})["products"])
    _save_json(tshirt_automation.MARKETED_LEDGER, {"products": []})
    print(f"🧹 Cleared {n} entr{'y' if n == 1 else 'ies'} from "
          f"{tshirt_automation.MARKETED_LEDGER.name}.")
    tshirt_automation.clear_pinned()


def reset_tshirt_corners():
    config = load_config()
    if config.pop(SHARED_CORNER_KEY, None) is not None:
        save_config(config)
        print(f"🔄  Cleared {SHARED_CORNER_KEY}. Re-pick (or re-migrate) on next run.")
    else:
        print("ℹ️  No t-shirt corner config found — nothing to clear.")


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    if "--reset" in args:
        reset_tshirt_corners()
        sys.exit(0)

    if "--clear-ledger" in args:
        clear_all_ledgers()
        sys.exit(0)

    run(
        setup_only="--setup" in args,
        skip_upscale="--skip-upscale" in args,
        force_reupscale="--reupscale" in args,
        no_publish="--no-publish" in args,
        publish="--publish" in args,
        dry_run="--dry-run" in args,
    )
