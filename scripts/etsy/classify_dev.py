"""
classify_dev.py — Decide from the ARTWORK whether a design is a developer joke
===============================================================================
WHY THIS EXISTS

Every tool that writes or judges copy first asks "is this a developer design?"
(audit.is_dev_design). The automatic answer reads the design name — the first
title segment — which works for punchline-first titles and fails for the
search-phrase-first format the shop now uses, where the joke sits in the tail.
A developer joke behind a "Funny AI Sweatshirt" title was judged everyday-AI;
generate_description() was told so; and the QA gate then threw away every
correct developer draft the model wrote. Four generations burned on 4546881437
before someone looked at the shirt and pinned it by hand.

This looks at the shirt. One small Gemini call per listing (the product photo
and a yes/no question), and the verdict is pinned in caching/dev_designs.json
— "listing_ids" for developer, "everyday_ids" for everyday-AI — where every
tool reads it before falling back to the title.

A HUMAN PIN WINS. A listing already in either list is not re-asked unless
--refresh, and a model verdict never moves a listing out of a list on its own;
it is reported as a disagreement for a person to settle.

USAGE
  python -m scripts.etsy.classify_dev                      # dry run: every live listing not yet pinned
  python -m scripts.etsy.classify_dev --apply              # pin the verdicts
  python -m scripts.etsy.classify_dev --listing 4546881437 --refresh --apply   # re-ask one and pin it
  python -m scripts.etsy.classify_dev --all                # re-ask pinned ones too, report disagreements
                                              # (pins stay unless --override)

Verdicts are cached in caching/design_class.json so a dry run followed by
--apply costs one call per listing, not two.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
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

import requests

from scripts.etsy.audit import (
    DEV_TITLE_RE,
    _load,
    _save,
    active_listings,
    design_name_of,
    load_dev_pins,
    pin_design,
)
from scripts.etsy.redescribe import primary_image_bytes
from services.ai_content import classify_design
from services.etsy_service import EtsyClient, EtsyError

CACHE_DIR = Path("caching")
VERDICTS_FILE = CACHE_DIR / "design_class.json"

# Seconds between Gemini calls. These are small, fast calls, and a sweep of the
# whole shop fires them far quicker than a generation run does — quick enough
# to trip the free tier's per-MINUTE limit, which the key pool cannot tell from
# the daily one and burns the key for the run. Paced under 10 a minute instead.
PACE_SECONDS = 6.5


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listing", type=int, action="append", help="one id (repeatable)")
    ap.add_argument("--all", action="store_true", help="include listings already pinned")
    ap.add_argument("--refresh", action="store_true", help="re-ask Gemini even if cached")
    ap.add_argument("--override", action="store_true",
                    help="with --apply: let a model verdict move a pinned listing")
    ap.add_argument("--apply", action="store_true", help="write the pins (default: dry run)")
    args = ap.parse_args()

    client = EtsyClient()
    live = active_listings(client, client.get_shop_id())
    dev, everyday = load_dev_pins()
    verdicts = _load(VERDICTS_FILE, {})

    rows = live
    if args.listing:
        rows = [l for l in rows if l["listing_id"] in set(args.listing)]
    elif not args.all:
        rows = [l for l in rows if l["listing_id"] not in dev and l["listing_id"] not in everyday]
    rows = sorted(rows, key=lambda l: l["listing_id"])

    print(f"\n  {'APPLY' if args.apply else 'DRY RUN'} · {len(rows)} listing(s) · "
          f"pinned so far: {len(dev)} developer, {len(everyday)} everyday-AI\n")

    asked = disagree = pinned = failed = 0
    for l in rows:
        lid = l["listing_id"]
        title = html.unescape(l.get("title") or "")
        name = design_name_of(title)
        title_says = bool(DEV_TITLE_RE.search(name.lower()))
        print(f"  {lid}  {name[:50]}")

        v = verdicts.get(str(lid))
        if v is None or args.refresh:
            try:
                photo = primary_image_bytes(client, lid)
            except (EtsyError, requests.RequestException) as e:
                print(f"      ⏭  could not fetch photo ({type(e).__name__})\n")
                failed += 1
                continue
            if not photo:
                print("      ⏭  no images\n")
                failed += 1
                continue
            if asked:
                time.sleep(PACE_SECONDS)
            is_dev, why = classify_design(photo)
            asked += 1
            if is_dev is None:
                print(f"      ⏭  not classified: {why}\n")
                failed += 1
                continue
            v = {"dev": is_dev, "reason": why, "title_says_dev": title_says,
                 "checked": dt.date.today().isoformat()}
            verdicts[str(lid)] = v
            _save(VERDICTS_FILE, verdicts)   # every verdict, as it lands
        else:
            print("      (cached)")

        model_dev = bool(v["dev"])
        print(f"      artwork: {'DEVELOPER' if model_dev else 'everyday-AI'} · "
              f"title: {'DEVELOPER' if title_says else 'everyday-AI'}"
              f"{'' if model_dev == title_says else '  ← DIFFERS'}")
        print(f"      {v['reason'][:110]}")

        pinned_as = True if lid in dev else False if lid in everyday else None
        if pinned_as is not None and pinned_as != model_dev:
            disagree += 1
            print(f"      !! pinned {'DEVELOPER' if pinned_as else 'everyday-AI'} by hand — "
                  f"{'overriding' if args.override and args.apply else 'pin kept'}")
            if not (args.override and args.apply):
                print()
                continue
        if args.apply and pinned_as != model_dev:
            pin_design(lid, model_dev)
            dev, everyday = load_dev_pins()
            pinned += 1
            print(f"      ✓ pinned {'DEVELOPER' if model_dev else 'everyday-AI'}")
        print()

    print(f"  asked Gemini: {asked} · pinned: {pinned} · disagreements with a hand pin: "
          f"{disagree} · failed: {failed}")
    print(f"  pins now: {len(dev)} developer, {len(everyday)} everyday-AI "
          f"({len(dev) + len(everyday)}/{len(live)} live listings decided by artwork)")
    if not args.apply:
        print("  Nothing pinned. Re-run with --apply to write caching/dev_designs.json.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
