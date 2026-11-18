"""
reprice.py — Push a profile's price ladder onto every product already on Printify
=================================================================================
Editing PRICE_BY_SIZE in printify_service.py only affects products created
AFTER the edit. The 56 sweatshirts and 32 tees already sitting in Printify keep
whatever price they were born with. This walks the shop and brings them into
line with the ladder their profile now declares.

Printify does NOT push the new price to Etsy on its own — only a re-publish
does, and a re-publish also syncs `visible: False` (HIDE_IN_STORE), which
switches the live Etsy listing OFF. Use --etsy instead: it writes the same
ladder straight into each linked Etsy listing's inventory and never touches
Printify's publish path (so shipping profiles and listing state are left alone).

USAGE
  python -m scripts.printify.reprice                       # dry run over the sweatshirt ladder
  python -m scripts.printify.reprice --apply               # commit it (Printify only)
  python -m scripts.printify.reprice --etsy                # dry run: which Etsy prices are stale
  python -m scripts.printify.reprice --apply --etsy        # also write the ladder to Etsy
  python -m scripts.printify.reprice --garment tee         # the t-shirt ladder instead
  python -m scripts.printify.reprice --apply --limit 3     # smoke-test on three products

WHY THESE NUMBERS (sweatshirt, as of 2026-08-20, 61.83 PHP/USD)
  P2,190 nets ~P500 to EVERY destination, Australia included. P2,085 would net
  exactly P500 in the US but only P408 to Australia, because Etsy's 11% applies
  to the shipping charge and Printful's AU postage is $21.99 against the US
  $8.49. Full derivation lives in the comment above SWEATSHIRT_PROFILE.

  Fixing shipping (fix_shipping.py) is worth ~P460 per sale. This ladder is
  worth ~P142. Run that one FIRST — repricing on top of P8.49 postage just
  makes an underwater product more expensive.
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from services.etsy_service import EtsyClient
from services.printify_service import (
    PrintifyClient, SWEATSHIRT_PROFILE, TEE_PROFILE, _norm,
)


def resolve_shop_id(client: PrintifyClient) -> int:
    """PRINTIFY_SHOP_ID is optional and blank in this .env, so fall back to the
    account's first (only) shop, the same way the rest of the repo does."""
    env = os.environ.get("PRINTIFY_SHOP_ID", "").strip()
    if env:
        return int(env)
    return int(client.get_shops()[0]["id"])


GARMENTS = {
    "sweatshirt": (SWEATSHIRT_PROFILE, 49, 410),
    "tee":        (TEE_PROFILE,        12,  29),
}


def all_products(client: PrintifyClient, shop_id: int) -> list[dict]:
    out, page = [], 1
    while True:
        batch = client.get(f"/shops/{shop_id}/products.json?limit=50&page={page}").get("data", [])
        out += batch
        if len(batch) < 50:
            return out
        page += 1


def sync_etsy_prices(etsy: EtsyClient, listing_id: int, ladder: dict, write: bool) -> int:
    """Set every Etsy variation's price from the ladder. Returns how many differed.

    Etsy's inventory PUT replaces the whole matrix, so this is read-modify-write:
    only the price of a variation whose size matches the ladder is changed.
    """
    inv = etsy.get_listing_inventory(listing_id)
    changes = 0
    for prod in inv.get("products", []):
        key = None
        for pv in prod.get("property_values", []):
            for val in pv.get("values", []):
                key = next((k for k in ladder if _norm(k) == _norm(val)), None)
                if key:
                    break
            if key:
                break
        if key is None:
            continue
        for off in prod.get("offerings", []):
            # Sizes a colour doesn't come in (Maroon past 2XL) sit disabled at P0.
            if not off.get("is_enabled", True):
                continue
            p = off["price"]
            if round(p["amount"] / p["divisor"], 2) != float(ladder[key]):
                changes += 1
            off["price"] = float(ladder[key])
    if write and changes:
        etsy.update_listing_inventory(listing_id, inv)
    return changes


def main() -> int:
    ap = argparse.ArgumentParser(description="Align Printify product prices with the profile ladder.")
    ap.add_argument("--garment", choices=sorted(GARMENTS), default="sweatshirt")
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run)")
    ap.add_argument("--limit", type=int, help="stop after N products")
    ap.add_argument("--publish", action="store_true",
                    help="after repricing, re-sync each linked product to Etsy "
                         "(images=False, so custom mockups survive)")
    ap.add_argument("--etsy", action="store_true",
                    help="write the ladder straight to each linked Etsy listing "
                         "(no re-publish, so hidden products stay active)")
    args = ap.parse_args()

    token = os.environ.get("PRINTIFY_API_TOKEN", "").strip()
    if not token:
        print("PRINTIFY_API_TOKEN missing from .env.")
        return 1

    profile, blueprint, provider = GARMENTS[args.garment]
    ladder = profile.price_by_size

    print("=" * 74)
    print(f"  Reprice {args.garment}  ·  {'APPLY' if args.apply else 'DRY RUN'}")
    print("=" * 74)
    print(f"\n  Target ladder: " + "  ".join(f"{k}=P{v:,}" for k, v in ladder.items()))

    client = PrintifyClient(token)
    etsy = EtsyClient() if args.etsy else None
    shop_id = resolve_shop_id(client)
    products = [p for p in all_products(client, shop_id)
                if p.get("blueprint_id") == blueprint and p.get("print_provider_id") == provider]
    print(f"  {len(products)} matching product(s) on Printify\n")

    touched = unchanged = failed = published = etsy_stale = 0
    for prod in products:
        if args.limit is not None and touched >= args.limit:
            break

        variants, changes = [], 0
        for v in prod["variants"]:
            if not v.get("is_enabled"):
                continue
            # Variant titles are "<a> / <b>" but the order is NOT consistent
            # across blueprints: the Gildan sweatshirt reads "S / Dark Chocolate"
            # while the Bella+Canvas tee reads "Black / S". Match every segment
            # against the ladder instead of trusting a position.
            key = None
            for part in v["title"].split(" / "):
                key = next((k for k in ladder if _norm(k) == _norm(part)), None)
                if key:
                    break
            if key is None:
                continue
            want = ladder[key] * 100          # display number x 100 = cents
            if v["price"] != want:
                changes += 1
            variants.append({"id": v["id"], "price": want, "is_enabled": True})

        # A product whose price is already right still needs the publish when
        # --publish is set: reprice.py may have run earlier without it, leaving
        # Printify correct and Etsy stale.
        if not changes:
            unchanged += 1
            if not (args.etsy or (args.apply and args.publish)):
                continue

        if changes:
            print(f"  {prod['id']}  {changes:>2} variants  {prod['title'][:46]}")
            touched += 1
        if args.apply and changes:
            try:
                client.update_product(shop_id, prod["id"], {"variants": variants})
            except Exception as exc:
                failed += 1
                print(f"      FAILED: {type(exc).__name__}: {str(exc)[:150]}")
                continue

        listing_id = (prod.get("external") or {}).get("id")
        if args.etsy and listing_id:
            try:
                n = sync_etsy_prices(etsy, int(listing_id), ladder, write=args.apply)
                if n:
                    etsy_stale += 1
                    print(f"      Etsy {listing_id}: {n} variation price(s) "
                          f"{'updated' if args.apply else 'stale'}")
            except Exception as exc:
                failed += 1
                print(f"      ETSY FAILED {listing_id}: {type(exc).__name__}: {str(exc)[:150]}")

        # Printify stores the new price immediately but only pushes it to Etsy
        # on a publish. Without this the Etsy listing keeps the old number.
        # republish_product passes images=False so the custom mockups uploaded
        # by marketing_sweatshirt.py are not overwritten by Printify's own.
        if args.apply and args.publish and (prod.get("external") or {}).get("id"):
            try:
                client.republish_product(shop_id, prod["id"])
                published += 1
            except Exception as exc:
                failed += 1
                print(f"      PUBLISH FAILED: {type(exc).__name__}: {str(exc)[:150]}")

    print("\n" + "=" * 74)
    print(f"  products repriced   : {touched}")
    print(f"  already correct     : {unchanged}")
    if args.publish:
        print(f"  re-synced to Etsy   : {published}")
    if args.etsy:
        print(f"  Etsy listings {'updated' if args.apply else 'stale'} : {etsy_stale}")
    if failed:
        print(f"  failed              : {failed}")
    if not args.apply:
        print("\n  Dry run — nothing written. Re-run with --apply to commit.")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
