"""
execute.py — T-Shirt Design Replacer (Batch Mode + Auto-Upscale)
==================================================================
USAGE:
  python execute.py              # Upscale → mockups → Printify upload (full pipeline)
  python execute.py --reset      # Clear saved corners and re-pick for all
  python execute.py --setup      # Only run corner setup (no processing)
  python execute.py --upscale-only   # Run upscaling step only (no mockups)
  python execute.py --skip-upscale   # Skip upscaling; use Final Designs/ as-is
  python execute.py --reupscale  # Force re-upscale (overwrite Final Designs/)
  python execute.py --no-publish # Skip the Printify upload step (mockups only)
  python execute.py --dry-run    # Printify step: preview only, create nothing
  python execute.py --update-listings  # Regenerate AI title/description/tags for
                                 #   every already-published product and republish
  python execute.py --reconcile  # Forget ledger entries deleted on Printify (re-create them)
  python execute.py --clear-ledger # Wipe the whole ledger (re-create ALL designs next run)
  python execute.py --forget "name.png" [more...]  # Forget specific design(s) by file name
  python execute.py --help       # Show this help

PRINTIFY SETUP (one-time, for Step 3):
  1. pip install -r requirements.txt
  2. Copy .env.example -> .env and paste your PRINTIFY_API_TOKEN
     (Printify Dashboard -> My Profile -> Connections -> Generate).
  If no token is set, Step 3 is skipped automatically (mockups still run).

FOLDER STRUCTURE:
  thumbnails/      → Put your t-shirt thumbnail images here (.png/.jpg)
  designs/         → Drop RAW (not-yet-upscaled) designs here
  Final Designs/   → Auto-populated with 4x upscaled designs (mockup source)
  output/          → Processed mockups are saved here (auto-created)
  bin/realesrgan/  → Place the Real-ESRGAN-ncnn-vulkan binary + models here
  config.json      → Auto-generated; stores corner coordinates per thumbnail

PIPELINE:
  1. Upscale every new design in designs/ → 4x → Final Designs/
     (uses 'realesr-general-x4v3' model — same as Upscayl Lite)
  2. For every thumbnail × every design in Final Designs/, render a mockup.
  3. Upload each Final Designs/ image to Printify, create & publish the product.
     (skips designs already in published.json; needs PRINTIFY_API_TOKEN)

UPSCALER SETUP (one-time):
  Download Real-ESRGAN-ncnn-vulkan release for your OS:
    https://github.com/xinntao/Real-ESRGAN/releases
  Extract into ./bin/realesrgan/ so the structure is:
    bin/realesrgan/realesrgan-ncnn-vulkan(.exe)
    bin/realesrgan/models/realesr-general-x4v3.bin
    bin/realesrgan/models/realesr-general-x4v3.param

FIRST RUN:
  On first run for each thumbnail, an interactive window opens.
  Click the 4 corners of the print area on the shirt (in order):
    1. Top-Left  →  2. Top-Right  →  3. Bottom-Right  →  4. Bottom-Left
  Corner picker supports: Ctrl+Z/Backspace/U = undo, R = reset, ENTER = confirm.
"""

from pathlib import Path
import json
import sys

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
    run_printify_upload,
    reconcile_ledger,
    clear_ledger,
    forget_designs,
    update_listings,
)

# ─── Folder Configuration ──────────────────────────────────────────────────────
THUMBNAILS_DIR    = Path("thumbnails")
DESIGNS_DIR       = Path("designs")           # raw, not-yet-upscaled designs
FINAL_DESIGNS_DIR = Path("Final Designs")     # 4x upscaled designs (mockup source)
OUTPUT_DIR        = Path("output")
CONFIG_FILE       = Path("config.json")

# ─── Supported Formats ─────────────────────────────────────────────────────────
IMG_EXTS = ("*.png", "*.jpg", "*.jpeg", "*.webp")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def gather_files(directory: Path) -> list[Path]:
    files = []
    for ext in IMG_EXTS:
        files.extend(directory.glob(ext))
        files.extend(directory.glob(ext.upper()))
    return sorted(set(files))


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"thumbnails": {}}


def save_config(config: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    print(f"   💾 Config saved → {CONFIG_FILE}")


# ──────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────────────────────────────────────

def ensure_dirs():
    for d in [THUMBNAILS_DIR, DESIGNS_DIR, FINAL_DESIGNS_DIR, OUTPUT_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def run(
    setup_only: bool = False,
    skip_upscale: bool = False,
    force_reupscale: bool = False,
    upscale_only: bool = False,
    no_publish: bool = False,
    dry_run: bool = False,
):
    ensure_dirs()

    raw_before      = gather_files(DESIGNS_DIR)
    upscaled_before = gather_files(FINAL_DESIGNS_DIR)
    thumbnails      = gather_files(THUMBNAILS_DIR)
    binary          = find_binary()

    print("\n" + "═" * 56)
    print("  🎽  T-Shirt Mockup Pipeline")
    print("═" * 56)
    print(f"\n  📊 Pre-flight scan:")
    print(f"     thumbnails/      → {len(thumbnails)} file(s)")
    print(f"     designs/         → {len(raw_before)} file(s)  (raw input)")
    print(f"     Final Designs/   → {len(upscaled_before)} file(s)  (upscaled, mockup source)")
    print(f"     Real-ESRGAN bin  → {binary if binary else '❌ NOT FOUND'}")

    if not raw_before and not upscaled_before:
        print(f"\n❌  Both 'designs/' and 'Final Designs/' are empty.")
        print(f"\n    Workflow:")
        print(f"      1. Drop your raw designs (.png/.jpg/.jpeg/.webp) into 'designs/'")
        print(f"      2. Run this script again")
        print(f"      3. They'll be auto-upscaled 4x → 'Final Designs/' → mockups → 'output/'\n")
        sys.exit(1)

    if not thumbnails:
        print(f"\n❌  No thumbnails found in '{THUMBNAILS_DIR}/'")
        print(f"    Add your t-shirt template image(s) and re-run.\n")
        sys.exit(1)

    # ── Step 1: Upscale ───────────────────────────────────────────────────────
    if not skip_upscale:
        upscale_designs(
            designs_dir=DESIGNS_DIR,
            output_dir=FINAL_DESIGNS_DIR,
            img_exts=IMG_EXTS,
            force=force_reupscale,
        )
    else:
        print(f"\n  ⏭️   Upscale step skipped (--skip-upscale)")

    if upscale_only:
        print(f"\n  ℹ️  --upscale-only mode: stopping before mockup step.\n")
        return

    # ── Step 2: Mockup ────────────────────────────────────────────────────────
    config  = load_config()
    designs = gather_files(FINAL_DESIGNS_DIR)

    if not designs:
        print(f"\n❌  'Final Designs/' is empty — cannot generate mockups.\n")
        if raw_before and not skip_upscale:
            print(f"    You have {len(raw_before)} file(s) in 'designs/' but the upscale")
            print(f"    step produced no output. Most likely causes:\n")
            if binary is None:
                print(f"      • ⚠️  Real-ESRGAN binary is NOT installed.")
                print(f"         Download: https://github.com/xinntao/Real-ESRGAN/releases")
                print(f"         Extract the release into:  ./bin/realesrgan/\n")
            else:
                print(f"      • Real-ESRGAN ran but errored (scroll up for the ❌ line)")
                print(f"      • The 'models/' folder may be missing next to the binary\n")
        elif skip_upscale:
            print(f"    You used --skip-upscale, but 'Final Designs/' has no files.")
            print(f"    Remove --skip-upscale to let the pipeline upscale 'designs/'.\n")
        sys.exit(1)

    # ── Ensure every thumbnail has saved corners ──────────────────────────────
    skipped_thumbs: set[str] = set()

    for thumb_path in thumbnails:
        key     = thumb_path.name
        corners = config.get("thumbnails", {}).get(key, {}).get("corners")
        if corners is None:
            print(f"\n  ⚙️  No saved corners for '{key}' — launching picker...")
            corners = pick_corners_interactive(thumb_path)
            if corners is None:
                print(f"  ⚠️  '{key}' skipped (no corners set)")
                skipped_thumbs.add(key)
                continue
            config.setdefault("thumbnails", {})[key] = {"corners": corners}
            save_config(config)

    if setup_only:
        print(f"\n  ℹ️  --setup mode: corners are ready. Exiting without processing.\n")
        return

    # ── Delegate batch rendering to the mockup service ───────────────────────
    corners_map = {
        name: data["corners"]
        for name, data in config.get("thumbnails", {}).items()
        if name not in skipped_thumbs
    }

    processed, skipped = generate_mockups(
        thumbnails=[t for t in thumbnails if t.name not in skipped_thumbs],
        designs=designs,
        corners_map=corners_map,
        output_dir=OUTPUT_DIR,
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═' * 56}")
    print(f"  🎉 Done!  {processed} mockup(s) created", end="")
    if skipped:
        print(f",  {skipped} skipped/failed", end="")
    print(f"\n  📁 Output folder: {OUTPUT_DIR}/")

    if processed > 0:
        print(f"\n  💡 TIP: Corners are cached in {CONFIG_FILE}.")
        print(f"         Run with --reset to re-pick corners.\n")
    else:
        print(f"\n  💡 TIP: Use --setup to only run the corner picker.\n")

    # ── Step 3: Printify upload ────────────────────────────────────────────────
    if no_publish:
        print(f"  ⏭️   Printify step skipped (--no-publish)\n")
        return

    run_printify_upload(designs, dry_run=dry_run)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    if "--reset" in args:
        config = load_config()
        config["thumbnails"] = {}
        save_config(config)
        print("🔄  Corner configs cleared. Re-pick on next run.")
        if len(args) == 1:
            sys.exit(0)

    # ── Ledger maintenance (published.json) ───────────────────────────────────
    if "--update-listings" in args:
        # Refresh title/description/tags (Gemini) on already-published products.
        update_listings(FINAL_DESIGNS_DIR)
        sys.exit(0)

    if "--reconcile" in args:
        # Drop records of products you've deleted on Printify so they re-create.
        reconcile_ledger()
        sys.exit(0)

    if "--clear-ledger" in args:
        clear_ledger()
        sys.exit(0)

    if "--forget" in args:
        # Usage: python execute.py --forget "break.png" "retrying_my_prompt.png"
        names = [a for a in args[args.index("--forget") + 1:] if not a.startswith("--")]
        if not names:
            print("⚠️  --forget needs at least one design file name, e.g.:")
            print('    python execute.py --forget "break.png"')
        else:
            forget_designs(names)
        sys.exit(0)

    setup_only      = "--setup" in args
    skip_upscale    = "--skip-upscale" in args
    force_reupscale = "--reupscale" in args
    upscale_only    = "--upscale-only" in args
    no_publish      = "--no-publish" in args
    dry_run         = "--dry-run" in args
    run(
        setup_only=setup_only,
        skip_upscale=skip_upscale,
        force_reupscale=force_reupscale,
        upscale_only=upscale_only,
        no_publish=no_publish,
        dry_run=dry_run,
    )

