"""
fix_shipping.py — Rewrite Etsy shipping rates from USD figures into real pesos
=============================================================================
THE BUG THIS FIXES

The Etsy shop bills in PHP. Printify does not: its print providers publish
carrier rates in USD, and Printify syncs them to Etsy as bare numbers. Etsy
stores whatever it receives and labels it with the SHOP's currency, so
Printful's "$8.49 to ship a sweatshirt to the US" lands in the profile as
P8.49 — about 14 US cents.

You are still charged the real $8.49. The buyer pays P8.49. Every US
sweatshirt order quietly loses ~P516 of postage, and the gap widens with
distance (an Australian order runs ~P1,338 short).

Prices are NOT affected: those numbers were typed as pesos on purpose, so
P2,030 means P2,030. Only the shipping fields are wrong, and only because
nobody converted them.

WHAT THIS SCRIPT DOES

For every destination in every Etsy shipping profile it:
  1. Looks up the print provider's real USD rate for that country, live from
     Printify's catalog (never hard-coded — carrier rates change).
  2. Converts to PHP and rounds up to the whole peso.
  3. Writes it back, but ONLY where the current figure is unmistakably
     broken — a bare USD number or a zero.

A destination already carrying a sane peso figure (>= P100) is never touched,
so profiles you already fixed by hand survive a run untouched. Profiles for
garments Printify has no rate card for — the Printful-dashboard ones — are
reported and skipped, never guessed at.

USAGE
  python -m scripts.etsy.fix_shipping                  # dry run — prints the plan, writes nothing
  python -m scripts.etsy.fix_shipping --apply          # actually write the rates
  python -m scripts.etsy.fix_shipping --fx 62.5        # override the USD->PHP rate
  python -m scripts.etsy.fix_shipping --profile 3124…  # limit to one profile (test first)

REQUIRES the shops_w scope. If you authorized before that was added to
etsy_auth.py, re-run `python -m scripts.auth.etsy_auth` or every write returns 401.

CAVEAT: Printify owns these profiles (printify_service.py passes
"shipping_template": True), and its re-syncs DO overwrite what this writes
(fixed 2026-08-20, found broken again 2026-11-02 with Printify's newer USD
figures). So tshirt_automation and sweatshirt_automation call repair()
automatically after every publish. Run this by hand only for publishes made
outside those pipelines (Printify dashboard, execute.py, reprice.py…).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

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
from services.printify_service import PrintifyClient


# USD -> PHP. Pinned rather than fetched so a run is reproducible and a bad
# FX API can never silently reprice the whole shop. Override with --fx.
PHP_PER_USD = 61.83

# Printify catalog coordinates per garment, and the words that identify the
# matching Etsy profile. Etsy profile titles are created by Printify and read
# like "Standard: Printful, Hoodie, Sweatshirt" / "Free Standard: Monster
# Digital, Kids classic tee".
# Matching is deliberately narrow. An earlier version matched the sweatshirt
# card on the word "printful", which swept in "Printful: Tshirts" and
# "Printful: Hoodies" — Printful-dashboard profiles for garments this repo
# never created — and would have repriced a t-shirt at sweatshirt postage.
# Only profiles whose garment we hold a real Printify rate card for are
# touched; everything else is reported and left alone.
# Order matters: "Sweatshirt" contains "shirt", so the sweatshirt card has to
# be tested first or every crewneck would be priced as a tee.
GARMENTS = [
    {"name": "Sweatshirt (Gildan 18000 / Printful via Printify)",
     "blueprint": 49, "provider": 410,
     "match": ("sweatshirt",),
     "listing_match": ("sweatshirt", "crewneck")},
    {"name": "T-shirt (Bella+Canvas 3001 / Monster Digital)",
     "blueprint": 12, "provider": 29,
     "match": ("monster digital",),
     "listing_match": ("t-shirt", "tshirt", "shirt", "tee")},
]

# A destination charging at least this many pesos is already a real peso
# figure, not a USD number in disguise. Every genuine rate clears it easily
# (the cheapest real one is ~P370); every broken rate is under P22.
SANE_PHP_FLOOR = 100.0


def load_printify_rates(client: PrintifyClient, blueprint: int, provider: int) -> dict:
    """{country_iso or 'REST_OF_THE_WORLD': (first_usd, additional_usd)}.

    Providers publish several service levels for the same country (Printful
    quotes both $7.69 and $8.49 to the US). We keep the DEARER one — charging
    the buyer the cheaper tier is how you end up subsidising postage again.
    """
    data = client.get(f"/catalog/blueprints/{blueprint}/print_providers/{provider}/shipping.json")
    rates: dict[str, tuple[float, float]] = {}
    for prof in data.get("profiles", []):
        first = prof["first_item"]["cost"] / 100
        extra = prof["additional_items"]["cost"] / 100
        for iso in prof.get("countries", []):
            if iso not in rates or first > rates[iso][0]:
                rates[iso] = (first, extra)
    return rates


def garment_for(profile_title: str | None, listing_titles: list[str]) -> dict | None:
    """Which garment's rate card applies to a profile.

    Printify names the profiles it creates ("Standard: Printful, Hoodie,
    Sweatshirt"), and that name is authoritative — a profile explicitly named
    for a Printful-dashboard garment must NOT be rescued by a listing title
    that merely contains "shirt".

    But 29 of this shop's profiles are type "manual" with title None: they do
    not come back from the shop's shipping-profiles endpoint at all, only from
    the listings that use them. For those, fall back to what the listing is
    selling.
    """
    low = (profile_title or "").lower().strip()
    if low:
        for g in GARMENTS:
            if any(word in low for word in g["match"]):
                return g
        return None                      # named, but for a garment we cannot price
    for title in listing_titles:
        lt = title.lower()
        for g in GARMENTS:
            if any(word in lt for word in g["listing_match"]):
                return g
    return None


def plan_profile(profile: dict, rates: dict, fx: float) -> list[dict]:
    """Every destination in one profile that is under-charging, with its fix."""
    changes = []
    for dest in profile.get("shipping_profile_destinations", []):
        iso = dest.get("destination_country_iso") or ""
        usd = rates.get(iso) or rates.get("REST_OF_THE_WORLD")
        if not usd:
            continue
        first_php = math.ceil(usd[0] * fx)
        extra_php = math.ceil(usd[1] * fx)

        pc, sc = dest["primary_cost"], dest["secondary_cost"]
        now_first = pc["amount"] / pc["divisor"]
        now_extra = sc["amount"] / sc["divisor"]

        # Only touch rates that are unmistakably broken: a bare USD figure
        # (every real peso rate is >= ~P370, every USD one <= ~P22) or a zero.
        # A destination already holding a sane peso amount is somebody's
        # deliberate setting — never second-guess it, even if our card is dearer.
        if now_first >= SANE_PHP_FLOOR:
            continue
        changes.append({
            "destination_id": dest["shipping_profile_destination_id"],
            "label": iso or "REST OF WORLD",
            "now_first": now_first, "now_extra": now_extra,
            "new_first": float(first_php),
            "new_extra": float(max(extra_php, now_extra)),
            "was_free": now_first == 0,
        })
    return changes


BACKUP_DIR = Path("caching")


def write_backup(profiles: list[dict]) -> Path:
    """Snapshot every destination's current cost before we overwrite anything.

    1,955 writes with no way back is not a repair, it is a gamble. This is the
    way back: --restore replays it verbatim.
    """
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / f"shipping_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
    snap = []
    for prof in profiles:
        for d in prof.get("shipping_profile_destinations", []):
            pc, sc = d["primary_cost"], d["secondary_cost"]
            snap.append({
                "profile_id": prof["shipping_profile_id"],
                "profile_title": prof.get("title"),
                "destination_id": d["shipping_profile_destination_id"],
                "country": d.get("destination_country_iso") or "",
                "primary_cost": pc["amount"] / pc["divisor"],
                "secondary_cost": sc["amount"] / sc["divisor"],
            })
    path.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    return path


def restore(etsy, shop_id: int, path: Path) -> int:
    """Put every destination back exactly as the snapshot found it."""
    rows = json.loads(path.read_text(encoding="utf-8"))
    print(f"  Restoring {len(rows)} destinations from {path.name}…")
    ok = bad = 0
    for r in rows:
        try:
            etsy.update_shipping_destination(
                shop_id, r["profile_id"], r["destination_id"],
                r["primary_cost"], r["secondary_cost"])
            ok += 1
        except Exception as exc:
            bad += 1
            print(f"     FAILED {r['destination_id']}: {type(exc).__name__}: {str(exc)[:120]}")
    print(f"  restored: {ok}   failed: {bad}")
    return 1 if bad else 0


def repair(apply: bool = False, fx: float = PHP_PER_USD, profile: int | None = None,
           limit: int | None = None, verbose: bool = True) -> dict:
    """Find (and with apply=True, fix) every under-charging shipping destination.

    Callable from the one-shot pipelines, which run it after every Printify
    publish because Printify re-syncs its own USD figures over our peso rates.
    A backup is written only when something is actually about to change.

    Returns {"needed": int, "written": int, "failed": int, "backup": Path | None}.
    """
    say = print if verbose else (lambda *a, **k: None)

    printify = PrintifyClient(os.environ["PRINTIFY_API_TOKEN"].strip())
    etsy = EtsyClient()
    shop_id = etsy.get_shop_id()

    say("\n  Reading live carrier rates from Printify…")
    for g in GARMENTS:
        g["rates"] = load_printify_rates(printify, g["blueprint"], g["provider"])
        say(f"     {g['name']}: {len(g['rates'])} destinations")

    say("\n  Reading Etsy listings to discover every profile in use…")
    listings, offset = [], 0
    while True:
        page = etsy._request(
            "GET", f"/application/shops/{shop_id}/listings"
                   f"?state=active&limit=100&offset={offset}")
        batch = page.get("results", [])
        listings += batch
        if len(listings) >= page.get("count", 0) or not batch:
            break
        offset += 100

    titles_by_profile: dict[int, list[str]] = {}
    for l in listings:
        titles_by_profile.setdefault(l.get("shipping_profile_id"), []).append(l.get("title", ""))
    say(f"     {len(listings)} active listings across {len(titles_by_profile)} profiles")

    # The shop endpoint only returns "shop"-type profiles. The 29 "manual" ones
    # this shop uses are invisible there and must be fetched by id.
    profiles = etsy.get_shipping_profiles(shop_id)
    known = {p["shipping_profile_id"] for p in profiles}
    for pid in titles_by_profile:
        if pid and pid not in known:
            profiles.append(etsy._request(
                "GET", f"/application/shops/{shop_id}/shipping-profiles/{pid}"))
    if profile:
        profiles = [p for p in profiles if p["shipping_profile_id"] == profile]
    say(f"     {len(profiles)} profile(s) to inspect")

    # Plan everything first, so the backup is only written when needed.
    plan: list[tuple[dict, list[dict]]] = []
    unmatched: list[str] = []
    for prof in profiles:
        pid = prof["shipping_profile_id"]
        garment = garment_for(prof.get("title"), titles_by_profile.get(pid, []))
        if not garment:
            unmatched.append(f"{pid}  {prof.get('title') or '(untitled)'}")
            continue
        changes = plan_profile(prof, garment["rates"], fx)
        if changes:
            plan.append((prof, changes))

    total_changes = sum(len(c) for _, c in plan)
    total_free = sum(1 for _, cs in plan for c in cs if c["was_free"])

    backup = None
    if apply and total_changes:
        backup = write_backup(profiles)
        say(f"\nBackup written: {backup}")
        say(f"  Undo this run with:  python -m scripts.etsy.fix_shipping --restore {backup}")

    applied = failed = 0
    for prof, changes in plan:
        pid = prof["shipping_profile_id"]
        say(f"\n  ── {prof.get('title') or '(untitled)'}  (#{pid})")
        for c in changes:
            tag = "  ← was FREE" if c["was_free"] else ""
            say(f"       {c['label']:<18} P{c['now_first']:>8,.2f} → P{c['new_first']:>8,.2f}{tag}")
            if apply and (limit is None or applied < limit):
                try:
                    etsy.update_shipping_destination(
                        shop_id, pid, c["destination_id"], c["new_first"], c["new_extra"])
                    applied += 1
                except Exception as exc:                       # keep going; report at the end
                    failed += 1
                    print(f"         FAILED: {type(exc).__name__}: {str(exc)[:160]}")

    say("\n" + "=" * 74)
    say(f"  destinations needing a fix : {total_changes}")
    say(f"  of those, currently free   : {total_free}")
    if unmatched:
        say(f"\n  SKIPPED — no Printify rate card matches these profiles ({len(unmatched)}).")
        say("  These are likely made on the Printful dashboard, outside Printify:")
        for u in unmatched[:12]:
            say(f"     {u}")
        if len(unmatched) > 12:
            say(f"     … and {len(unmatched) - 12} more")
    if apply:
        say(f"\n  written : {applied}")
        if failed:
            print(f"  failed  : {failed}  (401 here means the token predates shops_w — "
                  f"re-run: python -m scripts.auth.etsy_auth)")
    else:
        say("\n  Dry run — nothing was written. Re-run with --apply to commit.")
    say("=" * 74)
    return {"needed": total_changes, "written": applied, "failed": failed, "backup": backup}


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert Etsy shipping rates from USD figures to PHP.")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--fx", type=float, default=PHP_PER_USD, help=f"USD->PHP rate (default {PHP_PER_USD})")
    ap.add_argument("--profile", type=int, help="limit to one shipping_profile_id")
    ap.add_argument("--restore", metavar="FILE", help="undo a run: replay a backup JSON verbatim")
    ap.add_argument("--limit", type=int, help="stop after N writes (smoke-test a real apply)")
    args = ap.parse_args()

    if not os.environ.get("PRINTIFY_API_TOKEN", "").strip():
        print("PRINTIFY_API_TOKEN is not set in .env.")
        return 1

    print("=" * 74)
    print(f"  Etsy shipping repair  ·  {'APPLY' if args.apply else 'DRY RUN'}  ·  {args.fx:.2f} PHP/USD")
    print("=" * 74)

    if args.restore:
        etsy = EtsyClient()
        return restore(etsy, etsy.get_shop_id(), Path(args.restore))

    result = repair(apply=args.apply, fx=args.fx, profile=args.profile, limit=args.limit)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
