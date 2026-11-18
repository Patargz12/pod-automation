"""
tshirt_automation.py — One-shot pipeline for T-SHIRTS: local → Printify → Etsy → Pinterest
==========================================================================================
The t-shirt twin of sweatshirt_automation.py. Drop a design in "assets/designs/",
run ONE command, and it ends up as a live Etsy t-shirt listing with our own
mockups plus a Pinterest pin linking to it.

SCOPE: only the images in assets/designs/ are processed. The rest of
assets/designs_final/ (every design made before) is never touched.

    python -m scripts.create.tshirt_automation

WHAT IT DOES (per design, failures isolated):

  1. RENDER    — tee mockups for every color into output/tshirt/<design>/,
                 plus output/final_thumbnail/<design>_Black_Shirt.png (tshirt.py).
  2. PRINTIFY  — Gemini writes the title/description/tags, the product is created
                 (Bella+Canvas 3001 · Monster Digital) AND published.
  3. WAIT      — poll Printify until the Etsy listing id appears.
  4. MOCKUPS   — swap Printify's images for ours, rank 1 = Black_Shirt.png.
     SHIPPING  — Printify's sync rewrites Etsy shipping as USD-as-PHP (US
                 "P4.95"); put the peso rates back before going live, and
                 check again at the very end (fix_shipping.repair).
  5. TAGS      — make Etsy's tags match Printify's.
  6. ACTIVATE  — take the listing LIVE.  💸 $0.20 Etsy listing fee per draft.
  7. WAIT      — until Etsy reports 'active'; renewal set to Automatic.
  8. EXTRAS    — append the info images (Shirt_Guide size chart, Shirt_Print,
                 Shirt_Chart color chart) after our mockups, then build a
                 slideshow video from those mockups and upload it.
  9. PINTEREST — pin the black tee render, linking to the live listing.
 10. LEDGER    — caching/pinned_tshirt.json, so re-runs skip finished designs.

Everything else (placeholder guard, sandbox vs production Pinterest, fee
preview) behaves exactly like sweatshirt_automation.py — see its --help.

Ledgers (separate from the sweatshirt ones, so the same design can be both):
  caching/published.json        Printify tee products (shared with execute.py)
  caching/marketed_tshirt.json  mockup swap done
  caching/pinned_tshirt.json    live + pinned

USAGE:
  python -m scripts.create.tshirt_automation                    # the whole thing
  python -m scripts.create.tshirt_automation --dry-run          # trace everything, change nothing
  python -m scripts.create.tshirt_automation --only "name.png"  # touch ONLY this design
  python -m scripts.create.tshirt_automation --skip-pinterest   # stop after the listing is live
  python -m scripts.create.tshirt_automation --skip-video       # no slideshow video (stage 8)
  python -m scripts.create.tshirt_automation --skip-info-images # no size/print/color charts (stage 8)
  python -m scripts.create.tshirt_automation --extras-only      # ONLY stage 8, on every live listing
                                                                # in the ledger that is missing them
                                                                # (add --only "name.png" / --dry-run)
  python -m scripts.create.tshirt_automation --no-upscale       # the file in assets/designs/ is already high-res
  python -m scripts.create.tshirt_automation --forget "name.png" [more...]   # redo stages 4-8
  python -m scripts.create.tshirt_automation --clear-ledger     # wipe the pinned ledger
  python -m scripts.create.tshirt_automation --allow-placeholder
  python -m scripts.create.tshirt_automation --all              # also finish every unfinished design in the ledger
  python -m scripts.create.tshirt_automation --help
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

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

from scripts.create import tshirt
from scripts.create import marketing_sweatshirt
from scripts.create.execute import DESIGNS_DIR, FINAL_DESIGNS_DIR
from scripts.etsy.listing_extras import add_extras, backfill
from services.ai_content import generate_pin_content
from services.etsy_service import EtsyClient, EtsyError, EtsyAuthError
from services.pinterest_service import (
    PinterestClient,
    PinterestError,
    PinterestAuthError,
    is_sandbox,
)
from services.printify_service import (
    CACHE_DIR,
    PrintifyClient,
    PrintifyError,
    TEE_PROFILE,
    _load_json,
    _save_json,
)
# Garment-neutral stages, shared with the sweatshirt one-shot.
from scripts.create.sweatshirt_automation import (
    BOARD_DESCRIPTIONS,
    _board_name,
    _completed_names,
    _current_env,
    _designs_in_scope,
    _record_pinned,
    activate_and_wait,
    align_tags,
    ensure_publishing,
    preflight,
    preview_fees,
    repair_shipping,
    wait_for_etsy_listings,
)

PINNED_LEDGER   = CACHE_DIR / "pinned_tshirt.json"
MARKETED_LEDGER = CACHE_DIR / "marketed_tshirt.json"
THUMBNAIL_NAME  = f"{tshirt.THUMBNAIL_STEM}.png"          # Black_Shirt.png


# ── Ledger helpers ────────────────────────────────────────────────────────────

def _load_pinned() -> dict:
    return _load_json(PINNED_LEDGER, {"products": []})


def clear_pinned() -> int:
    n = len(_load_pinned().get("products", []))
    _save_json(PINNED_LEDGER, {"products": []})
    print(f"🧹  Cleared {n} entr{'y' if n == 1 else 'ies'} from {PINNED_LEDGER.name}.")
    return n


def forget_pinned(names: list[str]) -> int:
    ledger = _load_pinned()
    before = len(ledger["products"])
    ledger["products"] = [p for p in ledger["products"] if p["design"] not in set(names)]
    removed = before - len(ledger["products"])
    _save_json(PINNED_LEDGER, ledger)
    print(f"🔄  Forgot {removed} design(s) from {PINNED_LEDGER.name}."
          if removed else
          f"ℹ️  None of {names} found in {PINNED_LEDGER.name} — nothing forgotten.")
    # The mockup-swap ledger gates stage 4 — forget there too.
    marketing_sweatshirt.forget_marketed(names, ledger_path=MARKETED_LEDGER)
    return removed


# ── Stage 8 — Pinterest ───────────────────────────────────────────────────────

def _pin_image(design_name: str) -> Path:
    path = tshirt.FINAL_THUMBNAIL_DIR / f"{Path(design_name).stem}_{tshirt.THUMBNAIL_STEM}.png"
    if not path.is_file():
        raise PinterestError(f"No pin image: {path} is missing. "
                             f"Run `python -m scripts.create.tshirt --no-publish` to re-render it.")
    return path


def create_pin_for(pinterest: PinterestClient, design_name: str, etsy_url: str,
                   allow_placeholder: bool = False) -> dict | None:
    design = FINAL_DESIGNS_DIR / design_name
    copy_source = design if design.is_file() else _pin_image(design_name)
    title, description, alt_text, board_key, ai_used = generate_pin_content(
        copy_source,
        fallback_title=f"Funny {Path(design_name).stem.replace('_', ' ').title()} T-Shirt",
        fallback_description="A funny unisex t-shirt. Tap to see it on Etsy.",
        garment="tee",
    )
    board_name = _board_name(board_key)
    print(f"     🤖 Pin copy {'(AI)' if ai_used else '(PLACEHOLDER)'} → board '{board_name}'")
    print(f"        {title}")

    if not ai_used and not allow_placeholder:
        print(f"     🛑 Placeholder pin copy — NOT pinning this design "
              f"(fix Gemini quota or use --allow-placeholder).")
        return None

    board_id = pinterest.ensure_board(board_name, BOARD_DESCRIPTIONS.get(board_key, ""))
    image_path = _pin_image(design_name)
    pin = pinterest.create_pin(board_id=board_id, title=title, description=description,
                               link=etsy_url, alt_text=alt_text, image_path=image_path)
    print(f"     📌 Pin {pin.get('id')} created from {image_path.name}"
          f"{'  (sandbox — visible only to you)' if is_sandbox() else ''}")
    return {"pin_id": pin.get("id"), "board": board_name,
            "board_key": board_key, "pin_ai_used": ai_used}


# ── Orchestration ─────────────────────────────────────────────────────────────

def run(dry_run: bool = False, skip_upscale: bool = False,
        skip_pinterest: bool = False, allow_placeholder: bool = False,
        only: list[str] | None = None, process_all: bool = False,
        skip_video: bool = False, skip_info_images: bool = False):

    print("\n" + "═" * 60)
    print("  🚀  T-SHIRT AUTOMATION — Printify → Etsy → Pinterest")
    print("═" * 60)
    if dry_run:
        print("  🔍 DRY RUN — nothing is created, changed, pinned or charged.")

    # ── Scope: ONLY what is in assets/designs/ right now ─────────────────────
    # assets/designs_final/ keeps every design ever made, so it is never the
    # scope — otherwise a cleared ledger re-creates all of them as duplicates.
    scope = _designs_in_scope(skip_upscale, only)
    if not scope and not process_all:
        print(f"\n  ℹ️  Nothing to do — put the design(s) to process in {DESIGNS_DIR}/.\n")
        return
    if scope:
        print(f"\n  🎯 In scope ({DESIGNS_DIR}/): {', '.join(scope)}")

    etsy, pinterest = preflight(skip_pinterest)

    # ── Stages 1 & 2: render + create/publish on Printify ────────────────────
    if scope:
        tshirt.run(skip_upscale=skip_upscale, publish=not dry_run, dry_run=dry_run,
                   only=scope)

    if dry_run:
        print("\n  🔍 Dry run stops here — the remaining stages all need a real "
              "Printify product.\n")
        return

    published = _load_json(TEE_PROFILE.ledger_file, {"products": []}).get("products", [])
    if not published:
        print(f"\n  ℹ️  {TEE_PROFILE.ledger_file.name} is empty — nothing to do.\n")
        return

    # Only the designs currently in assets/designs/ (unless --all).
    in_scope = set(scope)
    already = _completed_names(_load_pinned(), skip_pinterest)
    if process_all:
        print(f"\n  🌐 --all: ignoring folder scope, using the whole ledger.")
        todo = [e for e in published if e["design"] not in already]
    else:
        todo = [e for e in published
                if e["design"] in in_scope and e["design"] not in already]

    if only:
        wanted = {n.lower() for n in only}
        todo = [e for e in todo if e["design"].lower() in wanted]
        missing = wanted - {e["design"].lower() for e in todo}
        if missing:
            print(f"  ⚠️  --only named design(s) not pending: {', '.join(sorted(missing))}")

    if not todo:
        print(f"\n  ✅ Nothing to do — every design in scope is already completed "
              f"for {_current_env().upper()} (in {PINNED_LEDGER.name}).\n")
        return

    print(f"\n  📋 {len(todo)} design(s) to process this run:")
    for e in todo:
        print(f"        • {e['design']}")

    printify = PrintifyClient(os.environ["PRINTIFY_API_TOKEN"].strip())

    # ── Stage 3: wait for the Etsy listings ──────────────────────────────────
    ensure_publishing(printify, todo)
    listing_ids = wait_for_etsy_listings(printify, todo)
    if not listing_ids:
        print("\n  ⏱️  No listings synced in time. Re-run in a few minutes.\n")
        return

    # ── Stage 4: swap Printify's mockups for ours ────────────────────────────
    print("\n" + "═" * 60)
    print("  🖼️   Stage 4 — swapping in our t-shirt mockups")
    print("═" * 60)
    marketing_sweatshirt.run(
        dry_run=False,
        only={e["design"] for e in todo},
        profile=TEE_PROFILE,
        output_dir=tshirt.OUTPUT_DIR,
        ledger_path=MARKETED_LEDGER,
        thumbnail_name=THUMBNAIL_NAME,
    )

    # Printify has synced by now, which is when it overwrites shipping with USD
    # figures. Fix it BEFORE anything goes live.
    repair_shipping("before going live")

    preview_fees(etsy, listing_ids)

    # ── Stages 5-8, per design ───────────────────────────────────────────────
    pinned_ledger = _load_pinned()
    live, held, failed, charged = [], [], [], []

    for entry in todo:
        name = entry["design"]
        listing_id = listing_ids.get(name)
        if not listing_id:
            continue

        print(f"\n  ──────── {name} ────────")

        if not entry.get("ai_used", True) and not allow_placeholder:
            print(f"     🛑 Gemini fell back to placeholder copy — leaving it in DRAFTS.")
            held.append(name)
            continue

        try:
            listing = etsy.get_listing(listing_id)
            shop_id = int(listing["shop_id"])

            align_tags(etsy, printify, shop_id, listing_id, entry)

            etsy_url, was_charged = activate_and_wait(etsy, shop_id, listing_id)
            if was_charged:
                charged.append(name)
            if not etsy_url:
                held.append(name)
                continue

            add_extras(etsy, shop_id, listing_id, "tshirt", name,
                       images=not skip_info_images, video=not skip_video)

            record = {
                "design": name,
                "product_id": entry["product_id"],
                "etsy_listing_id": listing_id,
                "etsy_url": etsy_url,
                "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

            if skip_pinterest or pinterest is None:
                print("     ⏭️  Pinterest skipped.")
            else:
                pin = create_pin_for(pinterest, name, etsy_url,
                                     allow_placeholder=allow_placeholder)
                if pin is None:
                    held.append(name)
                    continue
                record.update(pin)
                record["pinterest_env"] = _current_env()

            _record_pinned(pinned_ledger, record)
            _save_json(PINNED_LEDGER, pinned_ledger)
            live.append(name)

        except EtsyAuthError as e:
            print(f"\n❌  Etsy auth problem: {e}\n")
            sys.exit(1)
        except PinterestAuthError as e:
            print(f"\n❌  Pinterest auth problem: {e}\n")
            sys.exit(1)
        except (EtsyError, PrintifyError, PinterestError) as e:
            print(f"     ❌ FAILED: {e}")
            failed.append(name)

    # A late Printify sync can overwrite the rates again — check once more.
    repair_shipping("final check")

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print(f"  🎉 Done!  {len(live)} live, {len(held)} held, {len(failed)} failed")
    print("═" * 60)
    for p in pinned_ledger["products"]:
        if p["design"] in live:
            print(f"     • {p['design']}  →  {p['etsy_url']}")
            if p.get("pin_id"):
                print(f"       📌 pin {p['pin_id']} on '{p['board']}'"
                      f"{'  (sandbox)' if p.get('pinterest_env') == 'sandbox' else ''}")
    if live:
        print(f"\n  💸 Etsy listing fees charged this run: ${0.20 * len(charged):.2f}"
              f"  ({len(charged)} × $0.20)")
    if held:
        print(f"\n  ⏸️  Held in DRAFTS (not charged, retried next run): {', '.join(held)}")
    if failed:
        print(f"\n  ❌ Failed (retried next run): {', '.join(failed)}")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    if "--clear-ledger" in args:
        clear_pinned()
        sys.exit(0)

    if "--forget" in args:
        names = [a for a in args[args.index("--forget") + 1:] if not a.startswith("--")]
        if not names:
            print('⚠️  --forget needs a design file name, e.g. --forget "ctrl_alt.png"')
        else:
            forget_pinned(names)
        sys.exit(0)

    only = None
    if "--only" in args:
        only = [a for a in args[args.index("--only") + 1:] if not a.startswith("--")]
        if not only:
            print('⚠️  --only needs a design file name, e.g. --only "ctrl_alt.png"')
            sys.exit(1)

    if "--extras-only" in args:
        records = _load_pinned().get("products", [])
        if only:
            records = [r for r in records if r["design"].lower() in {n.lower() for n in only}]
        backfill("tshirt", records, dry_run="--dry-run" in args,
                 images="--skip-info-images" not in args, video="--skip-video" not in args)
        sys.exit(0)

    run(
        dry_run="--dry-run" in args,
        skip_upscale="--no-upscale" in args or "--skip-upscale" in args,
        skip_pinterest="--skip-pinterest" in args,
        allow_placeholder="--allow-placeholder" in args,
        only=only,
        process_all="--all" in args,
        skip_video="--skip-video" in args,
        skip_info_images="--skip-info-images" in args,
    )
