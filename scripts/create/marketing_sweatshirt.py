"""

Step 1: 
python -m scripts.create.marketing_sweatshirt --clear-ledger 

Step 2: 
python -m scripts.create.marketing_sweatshirt 


HOW TO USE :

python -m scripts.create.marketing_sweatshirt --clear-ledger # Wipe the WHOLE marketed ledger

=========================================================================
Phase 2 of the sweatshirt pipeline. Run this AFTER you have finalized the
positioning in Printify and clicked "Publish" there (the product then sits in
Etsy's DRAFTS tab with Printify's default mockups). For every such draft this
script:

  1. Matches the Etsy listing to its design — deterministically, no guessing:
       caching/published_sweatshirt.json → Printify product → external.id
     external.id IS the Etsy listing id once Printify has published it.
  2. Switches the listing's renewal option from "Manual" (Printify's default)
     to "Automatic", so it re-lists itself every four months.
  3. Deletes Printify's default mockup images from the draft.
  4. Uploads our renders from output/sweatshirt/<design>/ instead:
       • Black_Sweatshirt.png FIRST with rank 1  → the listing's MAIN THUMBNAIL
       • every other color after it (max 10 images — Etsy's limit)
  5. LEAVES THE LISTING IN DRAFTS. Publishing on Etsy stays a manual click, so
     you can eyeball each listing first. (Nothing is ever auto-published.)
  6. Records the design in caching/marketed_sweatshirt.json so re-runs skip it.

ONE-TIME SETUP (before the very first run):
  1. https://www.etsy.com/developers/your-apps → your app → add Callback URL:
       http://localhost:8080/callback
  2. Put the app's Keystring and Shared Secret in .env as ETSY_KEYSTRING and
     ETSY_SHARED_SECRET (both shown on the app page; Etsy requires BOTH in
     every API request since Feb 9, 2026).
  3. python -m scripts.auth.etsy_auth   → one browser click, saves caching/.etsy_tokens.json.
     Tokens auto-refresh forever after (re-run only if unused ~90 days).

USAGE:
  python -m scripts.create.marketing_sweatshirt             # Swap mockups on every new Etsy draft
  python -m scripts.create.marketing_sweatshirt --dry-run   # Preview everything, change nothing
  python -m scripts.create.marketing_sweatshirt --forget "name.png" [more...]
                                             # Re-process design(s) next run
  python -m scripts.create.marketing_sweatshirt --reconcile # Drop ledger entries whose Etsy
                                             #   listing no longer exists
  python -m scripts.create.marketing_sweatshirt --clear-ledger # Wipe the WHOLE marketed ledger
                                             #   (every design re-checked next run)
  python -m scripts.create.marketing_sweatshirt --help      # Show this help




GOTCHA — order matters: if you ever re-publish a product from Printify AFTER
this script ran (e.g. sweatshirt.py --update-listings), Printify pushes its
default mockups back over ours. Re-run with --forget "<design>.png" to fix.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Ensure UTF-8 output (emoji) on Windows cp1252 consoles / piped stdout.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from services.etsy_service import EtsyClient, EtsyError, EtsyAuthError
from services.printify_service import (
    CACHE_DIR,
    PrintifyClient,
    PrintifyError,
    SWEATSHIRT_PROFILE,
    _load_json,
    _save_json,
)

OUTPUT_DIR      = Path("output/sweatshirt")     # phase-1 mockup renders
MARKETED_LEDGER = CACHE_DIR / "marketed_sweatshirt.json"
THUMBNAIL_NAME  = "Black_Sweatshirt.png"        # always the main Etsy thumbnail
IMG_EXTS        = {".png", ".jpg", ".jpeg"}
MAX_ETSY_IMAGES = 10                            # Etsy hard limit per listing
UPLOAD_DELAY_S  = 0.5                           # politeness between uploads
RERANK_SETTLE_S = 2.0                           # let Etsy re-rank after deletes


# ══════════════════════════════════════════════════════════════════════════════
#  Ledger helpers (caching/marketed_sweatshirt.json — mirrors published_*.json)
# ══════════════════════════════════════════════════════════════════════════════

def _load_marketed(ledger_path: Path = MARKETED_LEDGER) -> dict:
    return _load_json(ledger_path, {"products": []})


def _marketed_names(ledger: dict) -> set[str]:
    return {p["design"] for p in ledger.get("products", [])}


def clear_marketed() -> int:
    """Wipe the entire marketed ledger — every design will be re-checked against
    Etsy (and re-marketed if its draft still needs our mockups) on the next run."""
    n = len(_load_marketed().get("products", []))
    _save_json(MARKETED_LEDGER, {"products": []})
    print(f"🧹  Cleared {n} entr{'y' if n == 1 else 'ies'} from {MARKETED_LEDGER.name}. "
          f"Every design will be re-checked against Etsy on the next run.")
    return n


def forget_marketed(names: list[str], ledger_path: Path = MARKETED_LEDGER) -> int:
    ledger = _load_marketed(ledger_path)
    before = len(ledger["products"])
    ledger["products"] = [p for p in ledger["products"] if p["design"] not in set(names)]
    removed = before - len(ledger["products"])
    _save_json(ledger_path, ledger)
    if removed:
        print(f"🔄  Forgot {removed} design(s) — they will be re-processed next run.")
    else:
        print(f"ℹ️  None of {names} found in {ledger_path.name} — nothing forgotten.")
    return removed


def reconcile_marketed() -> int:
    """Drop marketed-ledger entries whose Etsy listing no longer exists."""
    ledger = _load_marketed()
    if not ledger["products"]:
        print(f"ℹ️  {MARKETED_LEDGER.name} is empty — nothing to reconcile.")
        return 0
    try:
        etsy = EtsyClient()
    except EtsyAuthError as e:
        print(f"❌  Etsy auth problem: {e}")
        sys.exit(1)

    keep, dropped = [], []
    for p in ledger["products"]:
        if etsy.listing_exists(int(p["etsy_listing_id"])):
            keep.append(p)
        else:
            dropped.append(p["design"])
    ledger["products"] = keep
    _save_json(MARKETED_LEDGER, ledger)
    if dropped:
        print(f"🧹  Dropped {len(dropped)} stale entr{'y' if len(dropped) == 1 else 'ies'}: "
              f"{', '.join(dropped)}")
    else:
        print("✅  All marketed listings still exist on Etsy — ledger unchanged.")
    return len(dropped)


# ══════════════════════════════════════════════════════════════════════════════
#  Matching helpers
# ══════════════════════════════════════════════════════════════════════════════

def _mockup_files(design_name: str, output_dir: Path = OUTPUT_DIR,
                  thumbnail_name: str = THUMBNAIL_NAME) -> tuple[Path | None, list[Path]]:
    """(thumbnail, ordered files) for output/sweatshirt/<design stem>/.

    Thumbnail (Black_Sweatshirt.png) comes FIRST; the rest alphabetical.
    Returns (None, []) if the folder or the black mockup is missing.
    """
    folder = output_dir / Path(design_name).stem
    if not folder.is_dir():
        return None, []
    files = sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXTS),
        key=lambda p: p.name.lower(),
    )
    thumb = next((p for p in files if p.name.lower() == thumbnail_name.lower()), None)
    if thumb is None:
        return None, files
    ordered = [thumb] + [p for p in files if p != thumb]
    return thumb, ordered[:MAX_ETSY_IMAGES]


def _etsy_listing_id(product: dict) -> int | None:
    """The Etsy listing id Printify stored after YOU clicked Publish there."""
    ext = product.get("external") or {}
    if isinstance(ext, list):                    # older API shape
        ext = ext[0] if ext else {}
    lid = str(ext.get("id") or "").strip()
    return int(lid) if lid.isdigit() else None


# ══════════════════════════════════════════════════════════════════════════════
#  Pipeline
# ══════════════════════════════════════════════════════════════════════════════

def run(dry_run: bool = False, only: set[str] | None = None,
        profile=SWEATSHIRT_PROFILE, output_dir: Path = OUTPUT_DIR,
        ledger_path: Path = MARKETED_LEDGER, thumbnail_name: str = THUMBNAIL_NAME):
    """Swap Printify's mockups for ours on the Etsy drafts.

    `only` restricts the run to those design file names. sweatshirt_automation
    passes the designs currently in assets/designs_final/, because a run triggered by
    one new design must not reach into unrelated older products.

    The keyword defaults are the sweatshirt setup; tshirt_automation passes the
    tee profile, output/tshirt/, its own ledger and Black_Shirt.png.
    """

    print("\n" + "═" * 56)
    print("  🛍️   Etsy Marketing — Sweatshirt mockup swap")
    print("═" * 56)
    if dry_run:
        print("  🔍 DRY RUN — nothing will be changed on Etsy.")

    published = _load_json(profile.ledger_file, {"products": []}).get("products", [])
    if not published:
        print(f"\nℹ️  {profile.ledger_file.name} is empty — run `python -m scripts.create.sweatshirt` "
              f"first, publish from Printify, then come back.\n")
        return

    if only is not None:
        ignored = len(published) - len([e for e in published if e["design"] in only])
        published = [e for e in published if e["design"] in only]
        if ignored:
            print(f"  🎯 Scoped to {len(published)} design(s); "
                  f"{ignored} other product(s) untouched.")

    marketed_ledger = _load_marketed(ledger_path)
    already = _marketed_names(marketed_ledger)

    token = os.environ.get("PRINTIFY_API_TOKEN", "").strip()
    if not token:
        print("\n❌  PRINTIFY_API_TOKEN is not set in .env.\n")
        sys.exit(1)
    printify = PrintifyClient(token)

    try:
        etsy = EtsyClient()
    except EtsyAuthError as e:
        print(f"\n❌  Etsy auth problem: {e}\n")
        sys.exit(1)

    done, skipped, failed = [], [], []

    for entry in published:
        name = entry["design"]
        print(f"\n  ──────── {name} ────────")

        if name in already:
            print(f"     ⏭️   Already marketed (in {ledger_path.name}) — skipping. "
                  f"(--forget \"{name}\" to redo)")
            skipped.append(name)
            continue

        # Local mockups first — cheapest check, no API calls wasted.
        thumb, files = _mockup_files(name, output_dir, thumbnail_name)
        if thumb is None:
            folder = output_dir / Path(name).stem
            print(f"     ⚠️  No usable mockups: '{folder}/' "
                  f"{'is missing' if not folder.is_dir() else f'has no {thumbnail_name}'} "
                  f"— run `python -m scripts.create.sweatshirt` to render them. Skipping.")
            skipped.append(name)
            continue

        try:
            # published_sweatshirt.json → Printify product → Etsy listing id
            product = printify.get_product(entry["shop_id"], entry["product_id"])
            listing_id = _etsy_listing_id(product)
            if listing_id is None:
                print(f"     ⏭️   Not published from Printify yet (no Etsy listing id) — "
                      f"click Publish in Printify first. Skipping.")
                skipped.append(name)
                continue

            listing = etsy.get_listing(listing_id)
            state = listing.get("state", "?")
            if state != "draft":
                print(f"     ⚠️  Etsy listing {listing_id} is '{state}', not 'draft' — "
                      f"skipping (this script only touches drafts).")
                skipped.append(name)
                continue
            shop_id = int(listing["shop_id"])

            # Renewal options: Printify-created drafts land on "Manual". Flip
            # them to "Automatic" so they re-list themselves after four months.
            if listing.get("should_auto_renew") is True:
                print(f"     ♻️  Renewal already Automatic.")
            elif dry_run:
                print(f"     🔍 Would switch renewal Manual → Automatic.")
            else:
                etsy.set_auto_renew(shop_id, listing_id, True)
                print(f"     ♻️  Renewal switched to Automatic.")

            # Sort by rank — Etsy doesn't guarantee response order matches rank,
            # and picking the wrong image as "placeholder" (see below) causes a
            # rank collision ("ListingImages ... in an inconsistent order" 400).
            old_images = sorted(etsy.get_listing_images(listing_id),
                                 key=lambda i: i.get("rank", 0))
            print(f"     🔗 Etsy draft {listing_id}: {len(old_images)} Printify image(s) "
                  f"→ replacing with {len(files)} mockup(s), thumbnail: {thumb.name}")

            if dry_run:
                print(f"     🔍 Would upload (in this order): "
                      f"{', '.join(p.name for p in files)}")
                skipped.append(name)
                continue

            # Two Etsy rules shape the order here:
            #   • max 10 images per listing  → can't just upload ours first
            #   • a listing with images must always keep ≥1 → can't delete all
            # So: delete all old images EXCEPT one placeholder, upload ours,
            # then delete the placeholder last.
            #
            # The placeholder MUST be the LOWEST-ranked image, not the highest.
            # Etsy keeps ranks contiguous: delete ranks 1..5 of 6 and it has to
            # renumber the survivor 6→1, and that renumbering is asynchronous.
            # Uploading into that half-settled state is what throws
            # "The ListingImages for this Listing are in an inconsistent order."
            # Deleting the HIGH ranks instead leaves rank 1 exactly where it is,
            # so there is nothing to renumber and no window to race.
            placeholder = old_images[0] if old_images else None
            for img in old_images[1:]:
                etsy.delete_listing_image(shop_id, listing_id, img["listing_image_id"])
            if len(old_images) > 1:
                print(f"     🗑️  Deleted {len(old_images) - 1} old image(s) "
                      f"(keeping rank 1 until ours are up — Etsy forbids 0 images)")
                time.sleep(RERANK_SETTLE_S)   # let Etsy's image list settle

            # Room left while the placeholder still occupies a slot.
            room  = MAX_ETSY_IMAGES - (1 if placeholder else 0)
            total = len(files)
            for rank, path in enumerate(files[:room], start=1):
                etsy.upload_listing_image(shop_id, listing_id, path, rank=rank)
                print(f"     ⬆️  [{rank}/{total}] {path.name}"
                      f"{'   ← main thumbnail' if rank == 1 else ''}")
                time.sleep(UPLOAD_DELAY_S)

            if placeholder is not None:
                # Uploading with overwrite=true may have already replaced the
                # placeholder (the rank-1 upload takes over its slot), and
                # deleting an id that's no longer on the listing makes Etsy
                # throw a confusing "does not belong to the same shop" 400.
                # So re-check what is ACTUALLY still there and remove only that.
                old_ids  = {img["listing_image_id"] for img in old_images}
                leftover = [img["listing_image_id"]
                            for img in etsy.get_listing_images(listing_id)
                            if img["listing_image_id"] in old_ids]
                for iid in leftover:
                    etsy.delete_listing_image(shop_id, listing_id, iid)
                print(f"     🗑️  Old image cleanup: "
                      f"{len(leftover) or 'none'} still present"
                      f"{' — deleted' if leftover else ' (already replaced by our uploads)'}")

            # Only needed if we had 10 mockups AND a placeholder held a slot.
            for rank, path in enumerate(files[room:], start=room + 1):
                etsy.upload_listing_image(shop_id, listing_id, path, rank=rank)
                print(f"     ⬆️  [{rank}/{total}] {path.name}")
                time.sleep(UPLOAD_DELAY_S)

            marketed_ledger["products"].append({
                "design": name,
                "product_id": entry["product_id"],
                "etsy_listing_id": listing_id,
                "images": [p.name for p in files],
                "marketed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            _save_json(ledger_path, marketed_ledger)
            print(f"     ✓ Draft updated — review it in Etsy and click Publish there.")
            done.append(name)

        except EtsyAuthError as e:
            print(f"\n❌  Etsy auth problem: {e}\n")
            sys.exit(1)
        except (EtsyError, PrintifyError) as e:
            print(f"     ❌ FAILED: {e}")
            failed.append(name)
            continue

    print(f"\n{'═' * 56}")
    print(f"  🎉 Done!  {len(done)} draft(s) updated, {len(skipped)} skipped, "
          f"{len(failed)} failed")
    if done:
        print(f"     ✓ Updated: {', '.join(done)}")
        print(f"     👉 Etsy → Shop Manager → Listings → Drafts: check the images,")
        print(f"        then click Publish on each listing yourself.")
    if failed:
        print(f"     ❌ Failed (will retry next run): {', '.join(failed)}")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    if "--forget" in args:
        names = [a for a in args[args.index("--forget") + 1:] if not a.startswith("--")]
        if not names:
            print("⚠️  --forget needs at least one design file name, e.g.:")
            print('    python -m scripts.create.marketing_sweatshirt --forget "ctrl_alt.png"')
        else:
            forget_marketed(names)
        sys.exit(0)

    if "--reconcile" in args:
        reconcile_marketed()
        sys.exit(0)

    if "--clear-ledger" in args:
        clear_marketed()
        sys.exit(0)

    run(dry_run="--dry-run" in args)
