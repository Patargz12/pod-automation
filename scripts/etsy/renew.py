"""
renew.py — Turn Etsy auto-renew ON for every live listing that has it OFF
========================================================================
THE PROBLEM

An Etsy listing lives four months and then expires. `should_auto_renew` is the
"Automatic" / "Manual" radio in the listing's Renewal options; with it off the
listing silently drops out of the catalogue on its expiry date, taking its views
and its search history with it.

Seven live t-shirts were sitting on Manual, including the two best-performing
tees in the shop. The sweatshirt pipeline sets the flag on publish
(marketing_sweatshirt.py); the tee pipeline never has, which is why the split
falls exactly along garment lines.

WHAT IT COSTS

Nothing today. Etsy charges the $0.20 listing fee when a listing actually
RENEWS, not when the flag is flipped, so this run is free and the first charge
lands on each listing's own expiry date four months out. Renewing a listing that
has never sold is still the right call at $0.20 — the alternative is losing its
accumulated search history and starting from zero.

USAGE
  python -m scripts.etsy.renew             # dry run — shows what is off, writes nothing
  python -m scripts.etsy.renew --apply     # turn auto-renew on
  python -m scripts.etsy.renew --verify    # re-read from Etsy and confirm

VERIFYING
  --verify re-reads every active listing and reports any still on Manual. It
  reads the live state rather than trusting this script's own write, so a clean
  run is real confirmation. Runs append to caching/renew_log.txt.
"""

from __future__ import annotations

import argparse
import html
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from services.etsy_service import EtsyClient, EtsyError

CACHE_DIR = Path("caching")
LOG_FILE = CACHE_DIR / "renew_log.txt"


def _log(line: str = "") -> None:
    print(line)
    CACHE_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def active_listings(client: EtsyClient, shop_id: int) -> list[dict]:
    """Every active listing in the shop, paged out."""
    rows, offset = [], 0
    while True:
        # NOT /listings/active — that endpoint is served from Etsy's SEARCH
        # INDEX, which lags activation by minutes to hours. On 30 Aug it showed
        # 54 while the shop really had 69 live, hiding a whole 15-listing batch.
        # getListingsByShop with state=active reads the shop record directly and
        # is correct the instant a listing goes live. Same fields, same shape.
        page = client._request(
            "GET", f"/application/shops/{shop_id}/listings",
            params={"state": "active", "limit": 100, "offset": offset})
        rows += page.get("results", [])
        if len(rows) >= page.get("count", 0) or not page.get("results"):
            return rows
        offset += 100


def _describe(client: EtsyClient, listing: dict) -> str:
    """One line per listing, with the expiry date pulled from the full record.

    The field is `ending_timestamp` (NOT `ending_tsz` — that is the v2 name and
    silently returns nothing on v3). If the summary record already carries it we
    use that; otherwise fall back to a per-listing GET, because the expiry IS
    the deadline this script exists to beat.
    """
    lid = listing["listing_id"]
    title = html.unescape(listing.get("title", ""))[:44]
    views = listing.get("views")
    end = listing.get("ending_timestamp")
    if not end:
        try:
            end = client.get_listing(lid).get("ending_timestamp")
        except EtsyError:
            end = None
    if end:
        when = datetime.fromtimestamp(end)
        left = (when - datetime.now()).days
        stamp = f"expires {when:%Y-%m-%d} ({left}d)"
    else:
        stamp = "expiry unknown"
    return f"  {lid}  {stamp}  {views:>3}v  {title}"


def run(apply: bool) -> int:
    client = EtsyClient()
    shop_id = client.get_shop_id()
    rows = active_listings(client, shop_id)
    off = [L for L in rows if not L.get("should_auto_renew")]

    _log()
    _log("=" * 74)
    _log(f"  Auto-renew  ·  {'APPLY' if apply else 'DRY RUN'}  ·  "
         f"{datetime.now():%Y-%m-%d %H:%M:%S}  ·  shop {shop_id}")
    _log("=" * 74)
    _log()
    _log(f"  {len(rows)} active listings · {len(off)} on Manual")
    _log()

    if not off:
        _log("  Nothing to do — every live listing is already on Automatic.")
        _log()
        return 0

    for L in off:
        _log(_describe(client, L))
    _log()

    if not apply:
        _log("  Nothing was written. Re-run with --apply to commit.")
        _log(f"  Cost to apply: nothing now. Etsy bills $0.20 per listing when it")
        _log(f"  actually renews, on each listing's own expiry date.")
        _log()
        return 0

    done, failed = 0, 0
    for L in off:
        lid = L["listing_id"]
        try:
            client.set_auto_renew(shop_id, lid, True)
        except EtsyError as exc:
            _log(f"  ✗ {lid}  write failed: {exc}")
            failed += 1
            continue
        # Confirm from the server rather than from a 200.
        if client.get_listing(lid).get("should_auto_renew"):
            _log(f"  ✓ {lid}  auto-renew ON, confirmed live")
            done += 1
        else:
            _log(f"  ✗ {lid}  Etsy still reports Manual")
            failed += 1
        time.sleep(0.3)

    _log()
    _log("=" * 74)
    _log(f"  switched to Automatic : {done}")
    _log(f"  failed                : {failed}")
    _log("=" * 74)
    _log()
    return 1 if failed else 0


def verify() -> int:
    client = EtsyClient()
    shop_id = client.get_shop_id()
    rows = active_listings(client, shop_id)
    off = [L for L in rows if not L.get("should_auto_renew")]

    _log()
    _log("=" * 74)
    _log(f"  Verify auto-renew  ·  {datetime.now():%Y-%m-%d %H:%M:%S}")
    _log("=" * 74)
    _log()
    _log(f"  {len(rows)} active · {len(rows) - len(off)} Automatic · {len(off)} Manual")
    _log()
    for L in off:
        _log(_describe(client, L))
    if not off:
        _log("  ✓ every live listing is on Automatic")
    _log()
    return 1 if off else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Turn Etsy auto-renew on for live listings set to Manual.")
    ap.add_argument("--apply", action="store_true", help="commit the change")
    ap.add_argument("--verify", action="store_true", help="re-read and confirm")
    args = ap.parse_args()
    try:
        return verify() if args.verify else run(args.apply)
    except EtsyError as exc:
        print(f"\n✗ Etsy: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
