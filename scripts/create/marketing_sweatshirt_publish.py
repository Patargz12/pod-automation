"""
marketing_sweatshirt_publish.py — marketing_sweatshirt.py + auto-publish on Etsy.

Same as `python -m scripts.create.marketing_sweatshirt` (swap Printify's mockups for ours,
renewal → Automatic), then it ALSO clicks "Publish" on Etsy for you:

  1. Runs the normal mockup swap (marketing_sweatshirt.run).
  2. Every design in caching/marketed_sweatshirt.json whose Etsy listing is
     still a DRAFT gets its tags copied from Printify (Printify's own Etsy tag
     sync is unreliable), then is activated (taken live).  💸 $0.20 Etsy fee each.
     You get a 10s Ctrl+C window before anything is charged.
  3. Designs whose AI copy fell back to placeholder text stay in drafts
     (pass --allow-placeholder to publish them anyway).

Pinterest is NOT touched here.

USAGE:
  python -m scripts.create.marketing_sweatshirt_publish                      # Swap mockups + publish
  python -m scripts.create.marketing_sweatshirt_publish --dry-run            # Preview, change nothing
  python -m scripts.create.marketing_sweatshirt_publish --allow-placeholder  # Publish even placeholder copy
  python -m scripts.create.marketing_sweatshirt_publish --help
"""

from __future__ import annotations

import os
import sys

from scripts.create import marketing_sweatshirt
from scripts.create.sweatshirt_automation import activate_and_wait, align_tags, preview_fees
from services.etsy_service import EtsyClient, EtsyError, EtsyAuthError
from services.printify_service import (SWEATSHIRT_PROFILE, PrintifyClient,
                                       PrintifyError, _load_json)


def publish_marketed(dry_run: bool = False, allow_placeholder: bool = False,
                     profile=SWEATSHIRT_PROFILE,
                     ledger_path=marketing_sweatshirt.MARKETED_LEDGER):
    """Activate every marketed listing that is still a draft.

    Defaults are the sweatshirt ledgers; marketing_tshirt_publish.py passes the
    t-shirt ones.
    """
    print("\n" + "═" * 56)
    print("  📢  Etsy Publish — taking marketed drafts live")
    print("═" * 56)
    if dry_run:
        print("  🔍 DRY RUN — on a real run, every draft updated above would also")
        print("     be published here (💸 $0.20 each). Already-marketed drafts:")

    marketed = marketing_sweatshirt._load_marketed(ledger_path).get("products", [])
    if not marketed:
        print("\nℹ️  Nothing marketed yet — nothing to publish.\n")
        return

    # ai_used, shop_id and tags live in the profile's published ledger, not the
    # marketed ledger.
    published = {e["design"]: e
                 for e in _load_json(profile.ledger_file, {"products": []})
                                    .get("products", [])}
    ai_used = {n: e.get("ai_used", True) for n, e in published.items()}
    printify_shop = next(iter(published.values()), {}).get("shop_id")
    product_ids = {p["design"]: p["product_id"] for p in marketed}

    token = os.environ.get("PRINTIFY_API_TOKEN", "").strip()
    printify = PrintifyClient(token) if token else None
    if not printify:
        print("  ⚠️  PRINTIFY_API_TOKEN not set — tags will NOT be checked before publishing.")

    try:
        etsy = EtsyClient()
    except EtsyAuthError as e:
        print(f"\n❌  Etsy auth problem: {e}\n")
        sys.exit(1)

    # Only drafts are candidates — live listings are left alone.
    drafts: dict[str, int] = {}
    for p in marketed:
        try:
            if etsy.get_listing(int(p["etsy_listing_id"])).get("state") == "draft":
                drafts[p["design"]] = int(p["etsy_listing_id"])
        except EtsyError as e:
            print(f"  ⚠️  {p['design']}: could not read listing ({e}) — skipping.")

    if not drafts:
        print("\n  ✅ No drafts left — every marketed listing is already live.\n")
        return

    held = [n for n in drafts if not ai_used.get(n, True) and not allow_placeholder]
    for n in held:
        print(f"  🛑 {n}: placeholder AI copy — leaving in drafts "
              f"(--allow-placeholder to force).")
        drafts.pop(n)

    if not dry_run and drafts:
        preview_fees(etsy, drafts)

    live, failed = [], []
    for name, listing_id in drafts.items():
        print(f"\n  ──────── {name} ────────")
        try:
            shop_id = int(etsy.get_listing(listing_id)["shop_id"])
            if printify:
                # A design can be missing from the published ledger; the
                # marketed ledger still knows its Printify product.
                entry = published.get(name) or {"design": name, "tags": []}
                entry = {**entry, "product_id": entry.get("product_id") or product_ids[name],
                         "shop_id": entry.get("shop_id") or printify_shop}
                align_tags(etsy, printify, shop_id, listing_id, entry, dry_run=dry_run)
            url, _ = activate_and_wait(etsy, shop_id, listing_id, dry_run=dry_run)
            if url:
                live.append(name)
        except EtsyAuthError as e:
            print(f"\n❌  Etsy auth problem: {e}\n")
            sys.exit(1)
        except (EtsyError, PrintifyError) as e:
            print(f"     ❌ FAILED: {e}")
            failed.append(name)

    print(f"\n{'═' * 56}")
    print(f"  🎉 Done!  {len(live)} published, {len(held)} held, {len(failed)} failed")
    if failed:
        print(f"     ❌ Failed (re-run to retry): {', '.join(failed)}")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    dry = "--dry-run" in args
    marketing_sweatshirt.run(dry_run=dry)
    publish_marketed(dry_run=dry, allow_placeholder="--allow-placeholder" in args)
