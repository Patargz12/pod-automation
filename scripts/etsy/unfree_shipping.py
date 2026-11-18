"""
unfree_shipping.py — Take the 18 listings off the P0.00 "Free Shipping" rate card
=================================================================================
THE PROBLEM

18 active listings point at shipping profile 295057919524, "Printful: Free
Shipping (#PF-FRGFREE)". Every one of its 11 destinations is P0.00 / P0.00, so
Etsy prints "Free Shipping" on those listings and we collect nothing toward
postage — which Printful still bills us for in full.

Nothing else marks them as free. Etsy's OTHER free-shipping feature (the US
"Free Shipping Guarantee" on orders over $35) is a shop-level setting the v3 API
does not expose at all. It is not what is happening here: this is purely a rate
card full of zeros, and rate cards are writable.

WHAT THIS CHANGES

One field — `shipping_profile_id` — on each listing, using the same one-field
form-encoded updateListing that fix_profile.py uses. It does NOT:
  · change any price, title, image, tag, or the listing state
  · edit any rate on any profile (the free profile is left exactly as it is)
  · call Printify or Printful, or re-publish or re-sync anything
  · affect the listing URL, views or favourites

WHERE EACH LISTING GOES

  17 tees (taxonomy 482)      -> 295077566750  "Printful: Tshirts (#PF-FRG1)"
                                Printful's own tee rate card, and where the
                                shop's other Printful tee already sits.
                                US P278.87 · AU P422.12 · EU P281.22
  1 sweatshirt (taxonomy 469) -> 312495877008  "Standard: Printful, Hoodie,
                                Sweatshirt" — the card the other 58
                                sweatshirt/hoodie listings already use.
                                US P525.00 · AU P1360.00

Routing is by Etsy taxonomy_id, not by title, so a listing whose title does not
say "sweatshirt" still lands on the right card. Anything with an unrecognised
taxonomy is reported and skipped, never guessed at.

PRICES ARE NOT TOUCHED

These listings are P1934-P2500, the same band as the listings already on the
paid cards. Buyers simply start paying postage on top instead of us absorbing
it, so the shipping money is a straight margin gain.

THE RE-SYNC CAVEAT

These 18 were created by Printful's Etsy integration, not by Printify — none of
them appear in the Printify shop, so a Printify re-publish cannot touch them.
Printful's own integration can still reassert its profile if a product is
re-synced from the Printful dashboard. Re-run --verify after any such sync.

USAGE
  python -m scripts.etsy.unfree_shipping             # dry run — prints the plan, writes nothing
  python -m scripts.etsy.unfree_shipping --apply     # move them, then confirm from live data
  python -m scripts.etsy.unfree_shipping --verify    # re-read from Etsy, report what is where
  python -m scripts.etsy.unfree_shipping --restore caching/unfree_backup_YYYYMMDD_HHMMSS.json

Runs append to caching/profile_log.txt.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from services.etsy_service import EtsyClient, EtsyError

CACHE_DIR = Path("caching")
LOG_FILE = CACHE_DIR / "profile_log.txt"

# The rate card every one of these listings is currently on: all zeros.
FREE_PROFILE = 295057919524

# taxonomy_id -> destination profile. 482 = T-shirts, 469 = Sweatshirts/Hoodies.
ROUTES = {
    482: (295077566750, "Printful: Tshirts (#PF-FRG1)"),
    469: (312495877008, "Standard: Printful, Hoodie, Sweatshirt"),
}


def log(line: str):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  unfree_shipping  {line}\n")


def active_listings(client: EtsyClient, shop_id: int) -> list[dict]:
    """Every active listing in the shop (Etsy pages these 100 at a time)."""
    out, offset = [], 0
    while True:
        page = client._request(
            "GET", f"/application/shops/{shop_id}/listings/active",
            params={"limit": 100, "offset": offset})
        rows = page.get("results") or []
        out += rows
        offset += 100
        if len(rows) < 100:
            break
    return out


def build_plan(listings: list[dict]) -> tuple[list[dict], list[dict]]:
    """(moves, skipped) for everything still sitting on the free profile."""
    moves, skipped = [], []
    for l in listings:
        if l.get("shipping_profile_id") != FREE_PROFILE:
            continue
        row = {"listing_id": l["listing_id"],
               "title": (l.get("title") or "")[:60],
               "taxonomy_id": l.get("taxonomy_id"),
               "from": FREE_PROFILE}
        route = ROUTES.get(l.get("taxonomy_id"))
        if route is None:
            skipped.append(row)
            continue
        row["to"], row["to_title"] = route
        moves.append(row)
    return moves, skipped


def print_plan(moves: list[dict], skipped: list[dict]):
    if not moves and not skipped:
        print("Nothing on the free-shipping profile. Nothing to do.")
        return
    for pid, title in sorted({(m["to"], m["to_title"]) for m in moves}):
        rows = [m for m in moves if m["to"] == pid]
        print(f"\n-> {pid}  {title}   ({len(rows)} listings)")
        for m in rows:
            print(f"     {m['listing_id']}  tax={m['taxonomy_id']}  {m['title']}")
    for s in skipped:
        print(f"\n!! SKIPPED {s['listing_id']} — unmapped taxonomy "
              f"{s['taxonomy_id']}: {s['title']}")
    print(f"\n{len(moves)} listing(s) to move, {len(skipped)} skipped.")


def apply_moves(client: EtsyClient, shop_id: int, moves: list[dict]) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    backup = CACHE_DIR / f"unfree_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
    backup.write_text(json.dumps(moves, indent=2), encoding="utf-8")
    print(f"Backup written: {backup}\n")

    ok, failed = 0, []
    for m in moves:
        try:
            client.set_shipping_profile(shop_id, m["listing_id"], m["to"])
            ok += 1
            print(f"  OK   {m['listing_id']} -> {m['to']}  {m['title']}")
            log(f"{m['listing_id']} {m['from']} -> {m['to']}")
        except EtsyError as e:
            failed.append(m["listing_id"])
            print(f"  FAIL {m['listing_id']}: {str(e)[:160]}")
    print(f"\n{ok} moved, {len(failed)} failed.")
    if failed:
        print("Re-run to retry the failures — already-moved listings are skipped.")
    return backup


def verify(client: EtsyClient, shop_id: int) -> list[dict]:
    listings = active_listings(client, shop_id)
    counts: dict = {}
    for l in listings:
        pid = l.get("shipping_profile_id")
        counts[pid] = counts.get(pid, 0) + 1
    print("Active listings per shipping profile (live from Etsy):")
    for pid, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        mark = "   <- FREE SHIPPING" if pid == FREE_PROFILE else ""
        print(f"  {pid}: {n}{mark}")
    left = [l for l in listings if l.get("shipping_profile_id") == FREE_PROFILE]
    if left:
        print(f"\nStill on the free profile ({len(left)}):")
        for l in left:
            print(f"  {l['listing_id']}  {l['title'][:60]}")
    else:
        print("\nNothing left on the free-shipping profile.")
    return left


def restore(client: EtsyClient, shop_id: int, path: Path):
    rows = json.loads(path.read_text(encoding="utf-8"))
    for r in rows:
        client.set_shipping_profile(shop_id, r["listing_id"], r["from"])
        print(f"  back {r['listing_id']} -> {r['from']}")
        log(f"RESTORE {r['listing_id']} -> {r['from']}")
    print(f"\n{len(rows)} listing(s) restored to profile {FREE_PROFILE}.")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually write the changes")
    ap.add_argument("--verify", action="store_true", help="re-read from Etsy and report")
    ap.add_argument("--restore", metavar="BACKUP.json", help="put the listings back")
    args = ap.parse_args()

    client = EtsyClient()
    shop_id = client.get_shop_id()

    if args.restore:
        restore(client, shop_id, Path(args.restore))
        return
    if args.verify:
        verify(client, shop_id)
        return

    moves, skipped = build_plan(active_listings(client, shop_id))
    print_plan(moves, skipped)
    if not moves:
        return
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to do it.")
        return
    print()
    apply_moves(client, shop_id, moves)
    print()
    verify(client, shop_id)


if __name__ == "__main__":
    main()
