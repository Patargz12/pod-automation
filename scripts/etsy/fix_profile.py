"""
fix_profile.py — Move the orphaned tee onto the shipping profile its siblings use
=================================================================================
THE PROBLEM

The 7 Monster Digital tees should share one shipping profile. Six sit on
"Standard" (#308167021214), which prices 60 countries individually. One —
4525458859, "I Hallucinate With Confidence", 9 views — sits alone on
"Free Standard" (#308327773212), which names only US, AU and CA and drops
everything else into a single rest-of-world bucket at P619.

Standard prices 27 EU countries at P835. So an EU order on that one listing
collects P619 against postage that costs P835: P216 short, every time, one way.
No destination is overcharged, so there is nothing on the other side of it.

WHAT THIS CHANGES

One field — `shipping_profile_id` — on one listing. The same one-field
updateListing the section and title writes use. It does NOT:
  · delete or recreate the listing (URL, views, favourites, search history stay)
  · edit any rate on any profile
  · touch the images, title, tags, price or state
  · call Printify, or re-publish, or re-sync anything

US, AU, CA and rest-of-world are byte-identical between the two profiles, so the
only buyer-visible change is those 27 EU countries: P619 -> P835.

WHY THE PROFILE IS CALLED "FREE"

It used to be: US shipping on it was P0.00. fix_shipping.py rewrote that to
P495 on 20 Aug, because its repair rule treats a zero as a broken value and a
deliberate free-shipping zero is indistinguishable from one. The run logged it
("US P 0.00 -> P 495.00  <- was FREE"). The name is now meaningless.

THE PRINTIFY CAVEAT

Printify keeps its own copy of the profile id in the product's `external` block,
so re-publishing product 6a37a3dbdf3558139210f0bd would reassert the old
profile. That product is `visible: False` in Printify, and re-publishing a
hidden product switches the live Etsy listing OFF — so it must never be
re-published regardless. Re-run --verify after any Printify sync.

USAGE
  python -m scripts.etsy.fix_profile             # dry run — prints the plan, writes nothing
  python -m scripts.etsy.fix_profile --apply     # move it, then confirm from live data
  python -m scripts.etsy.fix_profile --verify    # re-read from Etsy, report what is where
  python -m scripts.etsy.fix_profile --restore caching/profile_backup_YYYYMMDD_HHMMSS.json

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

# The profile with only 3 named countries. Everything on it moves off.
ORPHAN_PROFILE = 308327773212
# The profile its 6 siblings already use.
TARGET_PROFILE = 308167021214


def _log(line: str = "") -> None:
    print(line)
    CACHE_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _banner(title: str) -> None:
    _log()
    _log("=" * 74)
    _log(f"  {title}  ·  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    _log("=" * 74)
    _log()


def _rates(profile: dict) -> dict[str, float]:
    """{country iso or '*' for rest-of-world: first-item cost}."""
    out = {}
    for d in profile.get("shipping_profile_destinations") or []:
        iso = d.get("destination_country_iso") or "*"
        cost = d["primary_cost"]
        out[iso] = cost["amount"] / cost["divisor"]
    return out


def _active_listings(client: EtsyClient, shop_id: int) -> list[dict]:
    rows, offset = [], 0
    while True:
        data = client._request(
            "GET",
            f"/application/shops/{shop_id}/listings"
            f"?state=active&limit=100&offset={offset}")
        page = data.get("results", []) or []
        rows.extend(page)
        if len(page) < 100:
            return rows
        offset += 100


def _profiles(client: EtsyClient, shop_id: int) -> dict[int, dict]:
    return {p["shipping_profile_id"]: p
            for p in client.get_shipping_profiles(shop_id)}


def _show_gap(profiles: dict[int, dict]) -> None:
    """Print exactly which countries are underpriced, and by how much."""
    orphan, target = profiles.get(ORPHAN_PROFILE), profiles.get(TARGET_PROFILE)
    if not orphan or not target:
        _log("  ⚠️  one of the profiles no longer exists — skipping the gap report")
        return

    a, b = _rates(orphan), _rates(target)
    fallback = a.get("*", 0.0)
    short = []
    for iso, want in sorted(b.items()):
        if iso == "*":
            continue
        charged = a.get(iso, fallback)
        if charged < want:
            short.append((iso, charged, want))

    _log(f"  on the orphan profile, rest-of-world = P{fallback:,.0f}")
    if not short:
        _log("  no destination is underpriced — nothing to recover")
        return
    delta = short[0][2] - short[0][1]
    _log(f"  {len(short)} countries collect P{short[0][1]:,.0f} where the postage "
         f"is P{short[0][2]:,.0f}  →  P{delta:,.0f} short per order")
    _log(f"  {', '.join(iso for iso, _, _ in short)}")


def _report(client: EtsyClient, shop_id: int) -> list[dict]:
    """Print who is on which profile; return the listings still on the orphan."""
    profiles = _profiles(client, shop_id)
    listings = _active_listings(client, shop_id)

    for pid, label in ((ORPHAN_PROFILE, "orphan"), (TARGET_PROFILE, "target")):
        p = profiles.get(pid)
        name = p.get("title", "?") if p else "— no longer exists —"
        n = len(_rates(p)) - 1 if p else 0
        on_it = [L for L in listings if L.get("shipping_profile_id") == pid]
        _log(f"  {label:<7} {pid}  {len(on_it)} listing(s) · {n} countries named")
        _log(f"          {name[:64]}")
    _log()

    stranded = [L for L in listings
                if L.get("shipping_profile_id") == ORPHAN_PROFILE]
    for L in stranded:
        _log(f"    {L['listing_id']}  {L.get('views', 0):>3}v  "
             f"{L.get('title', '')[:52]}")
    return stranded


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--restore", metavar="BACKUP.json")
    args = ap.parse_args()

    client = EtsyClient()
    shop_id = client.get_shop_id()

    # ── restore ─────────────────────────────────────────────────────────────
    if args.restore:
        _banner("Shipping profile · RESTORE")
        saved = json.loads(Path(args.restore).read_text(encoding="utf-8"))
        for lid, pid in saved.items():
            client.set_shipping_profile(shop_id, int(lid), int(pid))
            _log(f"  ↩️  {lid}  back to profile {pid}")
        _log(f"\n  restored: {len(saved)}")
        return 0

    # ── verify ──────────────────────────────────────────────────────────────
    if args.verify:
        _banner("Shipping profile · VERIFY")
        stranded = _report(client, shop_id)
        _log()
        if stranded:
            _log(f"  ✗ {len(stranded)} listing(s) still on the orphan profile")
            return 1
        _log("  ✓ nothing is left on the orphan profile")
        return 0

    # ── dry run / apply ─────────────────────────────────────────────────────
    _banner(f"Shipping profile · {'APPLY' if args.apply else 'DRY RUN'}")
    stranded = _report(client, shop_id)
    _log()
    _show_gap(_profiles(client, shop_id))
    _log()

    if not stranded:
        _log("  ✓ nothing to move.")
        return 0

    if not args.apply:
        _log(f"  Would move {len(stranded)} listing(s) → profile {TARGET_PROFILE}.")
        _log("  Nothing was written. Re-run with --apply to commit.")
        _log("  Only the shipping_profile_id field changes: no re-publish, no")
        _log("  Printify call, no change to images, text, price or state.")
        return 0

    CACHE_DIR.mkdir(exist_ok=True)
    backup_path = CACHE_DIR / f"profile_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
    backup_path.write_text(
        json.dumps({str(L["listing_id"]): L["shipping_profile_id"]
                    for L in stranded}, indent=2), encoding="utf-8")
    _log(f"  Backup written: {backup_path}")
    _log(f"  Undo with:  python -m scripts.etsy.fix_profile --restore {backup_path}")
    _log()

    moved, failed = 0, 0
    for L in stranded:
        lid = L["listing_id"]
        try:
            client.set_shipping_profile(shop_id, lid, TARGET_PROFILE)
        except EtsyError as exc:
            _log(f"  ✗ {lid}  {exc}")
            failed += 1
            continue
        # Read it back rather than trusting the write.
        live = client.get_listing(lid).get("shipping_profile_id")
        if live == TARGET_PROFILE:
            _log(f"  ✓ {lid}  now on {TARGET_PROFILE}, confirmed live")
            moved += 1
        else:
            _log(f"  ✗ {lid}  wrote {TARGET_PROFILE} but Etsy reports {live}")
            failed += 1

    _log()
    _log("=" * 74)
    _log(f"  moved  : {moved}")
    _log(f"  failed : {failed}")
    _log("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
