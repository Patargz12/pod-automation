"""
set_price.py — Price a listing's every size/colour to a fixed net-profit target
===============================================================================
`reprice.py` pushes a HARDCODED ladder onto Printify products. This does the
opposite: it reads the real fulfilment cost from Printful, derives the price
that nets the target, and writes it per variation straight to Etsy.

WHY IT EXISTS

The two oldest fleece listings (4433996964 crewneck, 4432534143 hoodie) are not
on Printify — they are direct Printful listings, so `reprice.py` cannot see
them and the Printify sweatshirt ladder is the wrong cost basis for them
(Printify's price for a Gildan 18000 is ~$20.41; Printful's own is $17.50).
Pricing them off that ladder overcharges the crewneck and badly underprices
the hoodie, which costs $20.25 where the crewneck costs $17.50.

WHERE THE COST COMES FROM

Printful's catalog endpoint is public — no API key:

    GET https://api.printful.com/products/{id}      145 = Gildan 18000 crewneck
                                                    146 = Gildan 18500 hoodie

`variant.price` is the fulfilment price for a one-placement print, per size AND
per colour — Navy costs $3.22 more than Black at 4XL and $3.67 more at 5XL, and
that is genuinely why this crewneck already carried higher Navy prices at the
top two sizes. Cached to caching/printful_catalog_{id}.json.

Cross-check: $17.50 here against the $20.41 that falls out of the Printify
sweatshirt ladder is a ~17% Printify markup, which is the expected shape.

THE MARGIN FORMULA

Etsy takes 11% of item + shipping, then P25 processing and P12.37 ($0.20) to
list. With postage CHARGED to the buyer it mostly cancels; with FREE shipping
it does not, and becomes pure cost:

    paid:  net = 0.89 * (price + ship) - 37.37 - cost - ship
    free:  net = 0.89 *  price         - 37.37 - cost - ship

So the price that hits the target is:

    paid:  price = (target + 37.37 + cost + 0.11 * ship) / 0.89
    free:  price = (target + 37.37 + cost +        ship) / 0.89

--postage free exists to show why free shipping is untenable here, not as a
recommendation: it puts the crewneck at ~P3,300 and the hoodie past P3,500.

NOT MODELLED — read before trusting the target
  · Offsite Ads (15% on an attributed order) wipes out the whole P500 margin
  · PH VAT on Etsy fees (12%) costs roughly P50 a sale
  · a Printful plan with a per-print surcharge on top of the catalog price
Treat P500 as the floor before ads, not a guaranteed take.

USAGE
  python -m scripts.etsy.set_price                       # dry run, both listings
  python -m scripts.etsy.set_price --apply
  python -m scripts.etsy.set_price --listing 4432534143  # just one
  python -m scripts.etsy.set_price --target 600          # a different net target
  python -m scripts.etsy.set_price --ship-usd 10.72      # override postage for specs without one
  python -m scripts.etsy.set_price --postage free        # price for free shipping instead

Prices are per variation via Etsy's inventory PUT, which REPLACES the whole
matrix — so this reads the live matrix, changes only the price field and hands
the rest back untouched. A backup of the previous prices is written to
caching/price_backup_<ts>.json before anything is applied.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from services.etsy_service import EtsyClient, EtsyError

CACHE_DIR = Path("caching")
LOG_FILE = CACHE_DIR / "price_log.txt"

FX = 61.83               # PHP per USD, same rate the sweatshirt ladder was built on
ETSY_RATE = 0.89         # 1 - (6.5% transaction + 4.5% processing)
ETSY_FLAT = 25 + 12.37   # flat processing + the $0.20 listing fee
TARGET_DEFAULT = 500
SHIP_USD_DEFAULT = 21.99  # Printful AU postage: the binding destination

# Which Printful catalog product each listing actually is, and the postage
# Printful bills to the BINDING destination for that garment — the most
# expensive row on the listing's own #PF- rate card, which is Printful-generated
# and so charges exactly what Printful bills:
#   fleece  "Standard: Printful, Hoodie, Sweatshirt"  AU            P1,360  $21.99
#   tees    "Printful: Tshirts (#PF-FRG1)"            rest-of-world P704    $11.38
# A spec without "ship_usd" uses --ship-usd (default: the fleece figure).
TEE_SHIP_USD = 11.38
_TEE = {"product": 71, "label": "Bella+Canvas 3001 tee", "ship_usd": TEE_SHIP_USD}
PLAN = {
    4433996964: {"product": 145, "label": "Gildan 18000 crewneck"},
    4432534143: {"product": 146, "label": "Gildan 18500 hoodie"},
    # 127.0.0.1 crewneck: the last legacy Printful fleece, still on the old
    # P2,500/2,700 prices until 13 Sept 2026. Same blank and same fleece card as
    # 4433996964; SKU suffix _5434 is Printful's Gildan 18000 Black S variant id.
    4432638406: {"product": 145, "label": "Gildan 18000 crewneck"},
    # The 17 Printful tees moved off the free-shipping card on 8 Sept 2026
    # (caching/unfree_backup_20260908_221543.json, taxonomy 482). Blank confirmed
    # from the descriptions and from the SKUs, whose suffix is the Printful
    # variant id (…_4016 = Bella+Canvas 3001, Black, S). 4513727456 is the 18th,
    # missed on 13 Sept and found by audit.py's per-size check the same day.
    **{lid: _TEE for lid in (
        4434986834, 4434924023, 4432645676, 4464482796, 4458964742, 4458951221,
        4450734196, 4513682953, 4513688433, 4513704707, 4513721857, 4513724283,
        4513730491, 4513733845, 4513747408, 4505970613, 4505505517, 4513727456)},
}

# The listings label sizes 2X/3X/4X/5X; Printful's catalog says 2XL/3XL/4XL/5XL.
SIZE_ALIASES = {"2X": "2XL", "3X": "3XL", "4X": "4XL", "5X": "5XL",
                "XXL": "2XL", "XXXL": "3XL"}


def _log(line: str = "") -> None:
    print(line)
    CACHE_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _banner(title: str) -> None:
    _log()
    _log("=" * 78)
    _log(f"  {title}")
    _log("=" * 78)


def _norm_size(size: str) -> str:
    s = (size or "").strip().upper()
    return SIZE_ALIASES.get(s, s)


def printful_costs(product_id: int, refresh: bool = False) -> dict[tuple[str, str], float]:
    """{(size, colour): USD fulfilment cost} from Printful's public catalog."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / f"printful_catalog_{product_id}.json"
    if cache.exists() and not refresh:
        blob = json.loads(cache.read_text(encoding="utf-8"))
    else:
        # Printful 403s urllib's default User-Agent; any real one is accepted.
        req = urllib.request.Request(
            f"https://api.printful.com/products/{product_id}",
            headers={"User-Agent": "nullish-pod-automation/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            blob = json.loads(r.read().decode("utf-8"))
        cache.write_text(json.dumps(blob, indent=2), encoding="utf-8")
    out = {}
    for v in blob["result"]["variants"]:
        out[(_norm_size(v.get("size")), (v.get("color") or "").strip())] = float(v["price"])
        out[int(v["id"])] = float(v["price"])   # SKU suffix -> exact variant price
    return out


def _variant_id(prod: dict) -> int | None:
    """Printful listings carry the catalog variant id as the SKU suffix
    ("6959273F76ACF_4016"). Exact where present; some rows have none."""
    sku = str(prod.get("sku") or "")
    tail = sku.rsplit("_", 1)[-1] if "_" in sku else ""
    return int(tail) if tail.isdigit() else None


def price_for(cost_php: float, ship_php: float, target: float, postage: str) -> float:
    """The retail price that nets `target` after Etsy's cut and Printful's bill."""
    if postage == "paid":       # buyer is charged postage; it cancels bar the 11%
        return (target + ETSY_FLAT + cost_php + (1 - ETSY_RATE) * ship_php) / ETSY_RATE
    return (target + ETSY_FLAT + cost_php + ship_php) / ETSY_RATE      # free shipping


def _variation(prod: dict) -> tuple[str, str]:
    """(size, colour) for one inventory product row.

    The size property is not named consistently: Printful rows say "Size",
    Printify sweatshirts "Sizes", Printify tees "Clothing sizes". Anything with
    "size" in the name is the size; the other property is the colour."""
    size = colour = ""
    for pv in prod.get("property_values", []):
        vals = pv.get("values") or []
        val = str(vals[0]) if vals else ""
        if "size" in (pv.get("property_name") or "").lower():
            size = val
        else:
            colour = val
    return _norm_size(size), colour.strip()


def _current(off: dict) -> float:
    p = off.get("price") or {}
    if isinstance(p, dict):
        return p.get("amount", 0) / (p.get("divisor") or 1)
    return float(p)


def plan_listing(client: EtsyClient, listing_id: int, spec: dict,
                 target: float, ship_php: float, postage: str):
    """Returns (inventory, rows, unmatched). Mutates inventory prices in place."""
    costs = printful_costs(spec["product"])
    inv = client.get_listing_inventory(listing_id)
    rows, unmatched = [], []

    for prod in inv.get("products", []):
        size, colour = _variation(prod)
        vid = _variant_id(prod)
        usd = costs.get(vid) if vid is not None else None
        if usd is None:                       # no SKU id -> match by name
            usd = costs.get((size, colour))
        if usd is None:                       # colour name mismatch -> size average
            same = [v for k, v in costs.items() if isinstance(k, tuple) and k[0] == size]
            usd = sum(same) / len(same) if same else None
        if usd is None:
            unmatched.append((size, colour))
            continue
        want = round(price_for(usd * FX, ship_php, target, postage), 2)
        for off in prod.get("offerings", []):
            rows.append((size, colour, usd, _current(off), want))
            off["price"] = want
    return inv, rows, unmatched


ORDER = ["XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL"]


def _show(listing_id: int, spec: dict, rows, ship_php: float,
          target: float, postage: str) -> None:
    _log(f"  {listing_id}  {spec['label']}  ·  Printful product {spec['product']}")
    _log(f"  postage {postage} (P{ship_php:,.0f})  ·  target net P{target:,.0f}")
    _log()
    _log(f"    {'size':<6}{'colour':<10}{'cost USD':>10}{'current':>11}{'new':>11}{'delta':>10}")
    seen = set()
    for size, colour, usd, cur, want in sorted(
            rows, key=lambda r: (ORDER.index(r[0]) if r[0] in ORDER else 99, r[1])):
        if (size, colour) in seen:
            continue
        seen.add((size, colour))
        _log(f"    {size:<6}{colour:<10}{usd:>10,.2f}{cur:>11,.2f}"
             f"{want:>11,.2f}{want - cur:>+10,.0f}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Price listings to a fixed net-profit target from Printful cost.")
    ap.add_argument("--listing", type=int, action="append",
                    help="listing id (repeatable; default: everything in PLAN)")
    ap.add_argument("--target", type=float, default=TARGET_DEFAULT,
                    help=f"net profit per sale, PHP (default {TARGET_DEFAULT})")
    ap.add_argument("--ship-usd", type=float, default=SHIP_USD_DEFAULT,
                    help=f"Printful postage to the binding destination "
                         f"(default {SHIP_USD_DEFAULT}, Australia)")
    ap.add_argument("--postage", choices=("paid", "free"), default="paid",
                    help="is the buyer charged postage? (default paid)")
    ap.add_argument("--refresh", action="store_true", help="re-fetch Printful catalog")
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run)")
    args = ap.parse_args()

    listings = args.listing or list(PLAN)
    unknown = [l for l in listings if l not in PLAN]
    if unknown:
        _log(f"  x no Printful product mapped for {unknown} — add it to PLAN.")
        return 1

    if args.refresh:
        for lid in listings:
            printful_costs(PLAN[lid]["product"], refresh=True)

    client = EtsyClient()

    _banner(f"Net-target pricing · {'APPLY' if args.apply else 'DRY RUN'}")
    _log(f"  FX P{FX}/USD · Etsy {int((1 - ETSY_RATE) * 100)}% + P{ETSY_FLAT:.2f} flat")
    _log("  Not modelled: Offsite Ads 15%, PH VAT on Etsy fees. P500 is a floor")
    _log("  before ads, not a guaranteed take.")

    staged = []
    for lid in listings:
        spec = PLAN[lid]
        ship_php = spec.get("ship_usd", args.ship_usd) * FX
        _log()
        inv, rows, unmatched = plan_listing(
            client, lid, spec, args.target, ship_php, args.postage)
        _show(lid, spec, rows, ship_php, args.target, args.postage)
        if unmatched:
            _log(f"    !! no Printful cost for {sorted(set(unmatched))} — left unchanged")
        staged.append((lid, inv, rows))

    _log()
    if not args.apply:
        _log("  Nothing was written. Re-run with --apply to commit.")
        _log("  Only the price field changes: quantity, variations, images,")
        _log("  title, tags and state are handed back untouched.")
        return 0

    CACHE_DIR.mkdir(exist_ok=True)
    backup = CACHE_DIR / f"price_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
    backup.write_text(json.dumps(
        {str(lid): [{"size": r[0], "colour": r[1], "was": r[3]} for r in rows]
         for lid, _inv, rows in staged}, indent=2), encoding="utf-8")
    _log(f"  Backup written: {backup}")
    _log()

    done, failed = 0, 0
    for lid, inv, _rows in staged:
        try:
            client.update_listing_inventory(lid, inv)
        except EtsyError as exc:
            _log(f"  x {lid}  {exc}")
            failed += 1
            continue
        live = client.get_listing_inventory(lid)          # trust Etsy, not the write
        prices = {_current(o) for p in live.get("products", [])
                  for o in p.get("offerings", [])}
        _log(f"  ok {lid}  live prices now P{min(prices):,.0f}-P{max(prices):,.0f}")
        done += 1

    _log()
    _log("=" * 78)
    _log(f"  priced : {done}")
    _log(f"  failed : {failed}")
    _log("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
