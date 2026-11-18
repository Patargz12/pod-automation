"""
audit.py — Batch-agnostic SEO / price / placement audit of live Etsy listings
=============================================================================
WHY THIS EXISTS

retitle.py, retag.py and section.py are one-off remediation scripts: each holds a
hardcoded map of the 29 listing ids that were fixed in Aug 2026. They --verify
clean while being completely BLIND to anything published later. That is how ten
listings went live on 24 Aug 2026 carrying old-niche titles and 42 banned tags
without a single check failing.

This script has no id list. It reads whatever is live, applies the same rules
those scripts encode, and reports. Run it after every activation batch.

USAGE
  python -m scripts.etsy.audit                     # every active listing
  python -m scripts.etsy.audit --new               # only listings not yet marked reviewed
  python -m scripts.etsy.audit --since 2026-08-24  # only listings activated on/after a date
  python -m scripts.etsy.audit --listing 4551830572
  python -m scripts.etsy.audit --quiet             # one line per listing, defects only
  python -m scripts.etsy.audit --accept            # mark every CLEAN listing as reviewed

THE REVIEWED LEDGER
  caching/audited.json records listing ids that have passed. --new diffs live
  listings against it, so "what did I just activate?" needs no bookkeeping.
  --accept only ever adds listings that currently pass, so a dirty listing can
  never be silently retired into the ledger.

WHAT IT CHECKS  (per listing)
  title    length floor/ceiling, banned characters, filler phrases, pipe format,
           wrong-garment wording, punchline-first opener, AI brand names
  tags     count, per-tag length, BANNED_TAGS, duplicates, shop-wide saturation,
           AI brand names
  brands   ChatGPT, Claude, OpenAI, … in title/tags/description (brand_guard.py)
  price    EVERY size/colour, not the listing's base price — a short 5XL is
           invisible otherwise.
           Printify: each variation against the ladder for its garment; a size
           the ladder has no rung for (XS, tee 4XL/5XL) is a note, not a defect.
           Printful: each variation against set_price.py's target (a note, not
           a defect, if set_price.PLAN does not know the listing yet)
  place    shop section assigned, shipping profile, auto-renew
  desc     redescribe.py's QA — developer copy on an everyday-AI design, wrong
           garment, filler, markup/emoji, truncation, duplicated prose, and a
           missing EU GPSR block on a Printify listing. Same checks, same
           code, so this is the one "is the shop clean" number.

Exit code is the number of listings with defects, so CI/scripts can gate on it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
from collections import Counter
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

from services.brand_guard import find_brands
from services.etsy_service import EtsyClient, ETSY_TITLE_BANNED_CHARS
from services.ai_content import (
    BANNED_TAGS,
    ETSY_TAG_LEN,
    ETSY_TAG_MAX,
    ETSY_TITLE_MAX,
    TAG_SATURATION_LIMIT,
)
from services.printify_service import SWEATSHIRT_PROFILE, TEE_PROFILE

CACHE_DIR = Path("caching")
LEDGER = CACHE_DIR / "audited.json"
# ── Provider ─────────────────────────────────────────────────────────────────
# Not every listing is a Printify product. 21 are legacy Printful listings, and
# the Printify ladders are the WRONG cost basis for them: Printful's own price
# for a Gildan 18000 is $17.50 against the ~$20.41 baked into the sweatshirt
# ladder, so a correctly-priced Printful crewneck sits BELOW the Printify ladder
# and used to audit as "breaks the net-profit floor" on every run.
#
# Provider is established the same way redescribe.py does it: whatever Printify
# claims in caching/printify_listing_map.json is Printify; everything else is
# Printful. Printful listings are checked against set_price.py's own target —
# the tool that priced them — or, if set_price.PLAN does not know the listing's
# Printful product, reported as unchecked rather than judged on the wrong ladder.
PRINTIFY_MAP_FILE = CACHE_DIR / "printify_listing_map.json"
# Listings whose DESIGN is a developer joke, where the title format hides it.
# The automatic test reads the design name (the first title segment), which works
# for punchline-first titles but not for the search-phrase-first format the older
# listings use — there the joke sits in the tail, past the keywords. Which designs
# are developer jokes is a judgment call anyway, so it is curated, not inferred.
#   caching/dev_designs.json  ->  {"listing_ids": [...], "everyday_ids": [...]}
# "listing_ids" pins a design as DEVELOPER; "everyday_ids" pins it as everyday-AI
# even where its title happens to trip DEV_TITLE_RE. Either list beats the title
# test. classify_dev.py fills both from the artwork; is_dev_design() reads them.
DEV_DESIGNS_FILE = CACHE_DIR / "dev_designs.json"

# Etsy's floor for a title that uses the space it is given. Below this, slots
# that could hold a search phrase are sitting empty.
TITLE_MIN_USEFUL = 120

FILLER = ("high quality", "premium", "limited edition", "best seller", "hot sale")

# ── Two-tier tag policy ──────────────────────────────────────────────────────
# BANNED_TAGS in ai_content.py is one flat list, built when the shop was moving
# OFF the developer niche entirely. That is no longer the policy: developer
# designs stay in the catalogue and deserve developer tags. What is not wanted is
# the shop drifting back to reading as a dev store, or a developer tag landing on
# a design that is not a developer joke.
#
#   DEV_TAGS      allowed — but only on a listing whose TITLE is actually a
#                 developer joke. On anything else they target the wrong buyer.
#   HARD_BANNED   never allowed: pure topic labels and words too generic to rank,
#                 which waste a slot regardless of audience.
DEV_TAGS = frozenset({
    "computer science", "software engineer", "software developer", "developer gift",
    "developer shirt", "dev humor", "web developer", "programmer", "programmer gift",
    "programmer shirt", "coding", "coding shirt", "coding humor", "coder gift",
    "machine learning", "data scientist", "data scientist gift", "prompt engineer",
    "ai engineer gift", "ml engineer", "devops gift", "debugging shirt",
    "it professional", "sysadmin gift", "linux shirt", "git humor",
})
HARD_BANNED = BANNED_TAGS - DEV_TAGS

# A listing counts as a developer design when its DESIGN NAME says so — the first
# comma segment, which is the joke itself. Testing the whole title fails on
# exactly the listings this matters for: old-niche copy stuffed "Programmer Gift,
# Data Scientist Humor" into the keyword tail of everyday-AI designs, so a
# full-title test reads "I Fear My Chat History" as a developer joke and then
# excuses the very tags that are wrong on it.
DEV_TITLE_RE = re.compile(
    r"\b(program(mer|ming)?|developer|dev|coding|code|coder|debug(ging)?|git|"
    r"software|engineer|sysadmin|devops|linux|terminal|compil(e|er|ing)|syntax|"
    r"machine learning|data scientist|algorithm|stack ?overflow|hackathon)\b")

SWEATSHIRT_WORDS = ("sweatshirt", "crewneck", "sweater")
TEE_WORDS = ("t-shirt", "tshirt", "tee", "shirt")

# Word-boundary matched, because "swea(tshirt)" contains "tshirt" and plain
# substring tests flag every correctly-titled sweatshirt as a mislabelled tee.
TEE_RE = re.compile(r"\b(t-shirt|tshirt|tee|tees)\b")
SWEATSHIRT_RE = re.compile(r"\b(sweatshirt|crewneck|sweater)\b")

# A title must OPEN with something a buyer would actually type. The shop's
# convention — set when the 29 were rewritten — is a search phrase first and the
# joke second: "Funny AI Sweatshirt, ... , <joke>". A title whose first segment
# reaches the garment noun without passing one of these words is leading with
# the punchline, and spends the highest-weighted characters on words nobody
# searches for.
SEARCH_ANCHORS = frozenset({
    "funny", "sarcastic", "humor", "humour", "cute", "retro", "vintage", "nerdy",
    "geeky", "gift", "ai", "tech", "office", "introvert", "overthinker",
    "student", "coworker", "work", "meeting", "productivity", "burnout", "homebody",
    "antisocial", "chatbot", "prompt", "robot",
})


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_dev_pins() -> tuple[set[int], set[int]]:
    """(developer ids, everyday ids) from caching/dev_designs.json."""
    blob = _load(DEV_DESIGNS_FILE, {})
    return (set(blob.get("listing_ids", [])), set(blob.get("everyday_ids", [])))


def pin_design(listing_id: int, is_dev: bool) -> None:
    """Record an artwork verdict so no tool has to guess from the title again."""
    dev, everyday = load_dev_pins()
    (dev if is_dev else everyday).add(listing_id)
    (everyday if is_dev else dev).discard(listing_id)
    _save(DEV_DESIGNS_FILE, {"listing_ids": sorted(dev), "everyday_ids": sorted(everyday),
                             "updated": dt.date.today().isoformat()})


def design_name_of(title: str) -> str:
    """The first comma segment — the joke itself, before the keyword tail."""
    return html.unescape(title or "").replace("|", ",").split(",")[0].strip()


def is_dev_design(listing_id: int, title: str, pins: tuple[set[int], set[int]]) -> bool:
    """Is this a developer design? A pin (either way) wins; otherwise the design
    name is tested. The ONE place every tool decides this, so audit.py and
    redescribe.py can never disagree about the same listing."""
    dev, everyday = pins
    if listing_id in dev:
        return True
    if listing_id in everyday:
        return False
    return bool(DEV_TITLE_RE.search(design_name_of(title).lower()))


def active_listings(client: EtsyClient, shop_id: int) -> list[dict]:
    """Every active listing in the shop, paged out."""
    rows, offset = [], 0
    while True:
        # NOT /listings/active — that endpoint is served from Etsy's SEARCH
        # INDEX, which lags activation by minutes to hours. On 30 Aug it showed
        # 54 while the shop really had 69 live, hiding a whole 15-listing batch
        # and reporting "auditing 0". getListingsByShop with state=active reads
        # the shop record directly and is correct the instant a listing goes
        # live. Same fields, same shape.
        page = client._request(
            "GET", f"/application/shops/{shop_id}/listings",
            params={"state": "active", "limit": 100, "offset": offset})
        rows += page.get("results", [])
        if len(rows) >= page.get("count", 0) or not page.get("results"):
            return rows
        offset += 100


def garment_of(title: str) -> str:
    """Which product the title says it is. Sweatshirt wins: 'Crewneck Sweatshirt'
    also contains 'shirt', so testing tee first would misclassify every one."""
    low = title.lower()
    if any(w in low for w in SWEATSHIRT_WORDS):
        return "sweatshirt"
    if any(w in low for w in TEE_WORDS):
        return "tee"
    return "unknown"


def _opens_with_punchline(title: str) -> bool:
    """True when the first segment reaches the garment noun without a search
    anchor — i.e. the joke is occupying the highest-weighted characters."""
    first = title.replace("|", ",").split(",")[0].lower()
    words = [w.strip(".!?'\"") for w in first.split()]
    lead = []
    for w in words:
        if w in SWEATSHIRT_WORDS or w in TEE_WORDS:
            break
        lead.append(w)
    else:
        return False          # no garment noun in the first segment — not our shape
    return not any(w in SEARCH_ANCHORS for w in lead)


def scan_title(title: str, garment: str) -> list[str]:
    problems = []
    low = title.lower()

    bad = sorted(set(title) & ETSY_TITLE_BANNED_CHARS)
    if bad:
        problems.append(f"banned character(s) {' '.join(bad)}")
    if "|" in title:
        problems.append("pipe format — shop convention is comma-separated")
    if len(title) > ETSY_TITLE_MAX:
        problems.append(f"{len(title)} chars, over Etsy's {ETSY_TITLE_MAX}")
    elif len(title) < TITLE_MIN_USEFUL:
        problems.append(f"only {len(title)} chars — wasting search slots")
    for phrase in FILLER:
        if phrase in low:
            problems.append(f"filler phrase {phrase!r}")
    if garment == "sweatshirt" and TEE_RE.search(low):
        problems.append("names a tee but this is a sweatshirt")
    if garment == "tee" and SWEATSHIRT_RE.search(low):
        problems.append("names a sweatshirt but this is a tee")
    if _opens_with_punchline(title):
        problems.append("punchline-first opener — no search phrase in the first segment")
    if "  " in title or title != title.strip():
        problems.append("stray whitespace")
    brands = find_brands(title)
    if brands:
        problems.append(f"AI brand name(s) {', '.join(sorted(set(brands)))} — trademark risk")
    return problems


def scan_tags(tags: list[str], usage: Counter, is_dev_design: bool) -> tuple[list[str], list[str]]:
    """Returns (defects, notes). Developer tags are a defect only when the design
    itself is not a developer joke — on a genuine dev design they are correct
    targeting and merely worth counting."""
    problems, notes = [], []
    if len(tags) != ETSY_TAG_MAX:
        problems.append(f"{len(tags)} tags, should be {ETSY_TAG_MAX}")
    for t in tags:
        if len(t) > ETSY_TAG_LEN:
            problems.append(f"{t!r} is {len(t)} chars, limit {ETSY_TAG_LEN}")
        if t in HARD_BANNED:
            problems.append(f"{t!r} is banned (topic label or too generic to rank)")
        elif t in DEV_TAGS and not is_dev_design:
            problems.append(f"{t!r} is a developer tag on a non-developer design")
        if usage[t] > TAG_SATURATION_LIMIT:
            problems.append(f"{t!r} on {usage[t]} listings (limit {TAG_SATURATION_LIMIT})")
        if find_brands(t):
            problems.append(f"{t!r} names an AI brand — trademark risk")
    if len(set(tags)) != len(tags):
        problems.append("duplicate tags within this listing")
    if is_dev_design:
        n = sum(1 for t in tags if t in DEV_TAGS)
        notes.append(f"developer design — {n} developer tag(s), on-target")
    return problems, notes


def printify_ids() -> set[int] | None:
    """Listing ids Printify claims, or None when the map has never been built."""
    if not PRINTIFY_MAP_FILE.exists():
        return None
    return {int(k) for k in _load(PRINTIFY_MAP_FILE, {})}


def scan_printful_price(client: EtsyClient, listing_id: int) -> tuple[list[str], list[str]]:
    """Price check for a Printful listing: every size/colour against the exact
    price set_price.py would set today, from the same cost, postage and target.
    Reuses set_price.plan_listing() so the audit cannot disagree with the tool.
    Unlike the Printify ladder check this sees every variation, not just the
    listing's base price."""
    from scripts.etsy import set_price
    spec = set_price.PLAN.get(listing_id)
    if not spec:
        return [], ["Printful listing not in set_price.PLAN — no cost basis, "
                    "price unchecked (WhatToDo.md open items)"]
    try:
        ship_usd = spec.get("ship_usd", set_price.SHIP_USD_DEFAULT)  # per garment
        _, rows, unmatched = set_price.plan_listing(
            client, listing_id, spec, set_price.TARGET_DEFAULT,
            ship_usd * set_price.FX, "paid")
    except Exception as e:  # catalog or inventory fetch failed — say so, do not guess
        return [], [f"Printful listing — cost lookup failed ({e}), price unchecked"]

    problems, notes = [], []
    short = [(s, c, cur, want) for s, c, _usd, cur, want in rows if cur < want - 1]
    over = [(s, c, cur, want) for s, c, _usd, cur, want in rows if cur > want + 1]
    if short:
        worst = max(short, key=lambda r: r[3] - r[2])
        problems.append(f"{len(short)} variation(s) BELOW set_price target "
                        f"({spec['label']}) — worst {worst[0]} {worst[1]} "
                        f"P{worst[2]:,.0f} vs P{worst[3]:,.0f}")
    if over:
        worst = max(over, key=lambda r: r[2] - r[3])
        notes.append(f"{len(over)} variation(s) above set_price target "
                     f"({spec['label']}) — most {worst[0]} {worst[1]} "
                     f"P{worst[2]:,.0f} vs P{worst[3]:,.0f}")
    if unmatched:
        notes.append(f"{len(unmatched)} variation(s) not in Printful catalog, unchecked")
    if not short and not over:
        notes.append(f"priced to P{set_price.TARGET_DEFAULT} net on every variation "
                     f"({spec['label']}, Printful)")
    return problems, notes


def scan_printify_price(client: EtsyClient, listing_id: int,
                        garment: str) -> tuple[list[str], list[str]]:
    """Price check for a Printify listing: every size/colour against the ladder
    rung for its size. The listing's base `price` field is only the cheapest
    variation, so checking that alone — as this did until 13 Sept — could never
    see a 5XL sitting below its rung. Same shape as scan_printful_price(), and
    the same size normaliser (2X -> 2XL), so the two checks read a matrix alike.

    A size the ladder has no rung for is a NOTE: it is a gap in the ladder, not
    a listing defect, and filling it is a pricing decision."""
    from scripts.etsy import set_price
    profile = {"sweatshirt": SWEATSHIRT_PROFILE, "tee": TEE_PROFILE}.get(garment)
    if not profile:
        return ["garment not identifiable from the title — cannot check price"], []
    ladder = {set_price._norm_size(s): float(p) for s, p in profile.price_by_size.items()}
    try:
        inv = client.get_listing_inventory(listing_id)
    except Exception as e:
        return [], [f"inventory fetch failed ({e}) — price unchecked"]

    rows, gaps = [], set()
    for prod in inv.get("products", []):
        size, colour = set_price._variation(prod)
        want = ladder.get(size)
        if want is None:
            gaps.add(size or "?")
            continue
        for off in prod.get("offerings", []):
            # Disabled rows are combos the blank does not come in (Maroon stops
            # at 2XL, Dark Grey has no S/L). Etsy holds them at P0; nobody can
            # buy one, so they are not a price defect.
            if off.get("is_enabled") is False:
                continue
            rows.append((size, colour, set_price._current(off), want))
    if not rows and not gaps:
        return [], ["no inventory rows — price unchecked"]

    problems, notes = [], []
    short = [r for r in rows if r[2] < r[3] - 1]
    over = [r for r in rows if r[2] > r[3] + 1]
    if short:
        worst = max(short, key=lambda r: r[3] - r[2])
        problems.append(f"{len(short)} variation(s) BELOW the {garment} ladder — "
                        f"worst {worst[0]} {worst[1]} P{worst[2]:,.0f} vs "
                        f"P{worst[3]:,.0f} — breaks the net-profit floor")
    if over:
        worst = max(over, key=lambda r: r[2] - r[3])
        notes.append(f"{len(over)} variation(s) above the {garment} ladder — "
                     f"most {worst[0]} {worst[1]} P{worst[2]:,.0f} vs P{worst[3]:,.0f}")
    if gaps:
        notes.append(f"size(s) {', '.join(sorted(gaps))} have no rung on the {garment} "
                     f"ladder — unchecked (ladder gap, a pricing decision)")
    if rows and not short and not over:
        notes.append(f"on the {garment} ladder on every variation ({len(rows)} checked)")
    return problems, notes


def scan_placement(listing: dict, garment: str, provider: str,
                   client: EtsyClient) -> tuple[list[str], list[str]]:
    """Returns (defects, notes). Notes are deliberate deviations worth seeing but
    not worth failing on — a price ABOVE the floor is a pricing decision, whereas
    a price BELOW it silently breaks the net-profit target and is a real defect.

    The floor depends on who fulfils the listing: the Printify ladder for
    Printify products, set_price.py's cost-derived price for Printful ones."""
    problems, notes = [], []
    if not listing.get("shop_section_id"):
        problems.append("no shop section — invisible in shop navigation")
    if listing.get("should_auto_renew") is not True:
        problems.append("Manual renewal — will expire silently in four months")

    if provider == "printful":
        p, n = scan_printful_price(client, listing["listing_id"])
    else:
        p, n = scan_printify_price(client, listing["listing_id"], garment)
    return problems + p, notes + n


def _ts(v) -> str:
    if not v:
        return "—"
    return dt.datetime.fromtimestamp(int(v), dt.timezone.utc).strftime("%Y-%m-%d")


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit live Etsy listings against shop SEO policy.")
    ap.add_argument("--new", action="store_true", help="only listings not in caching/audited.json")
    ap.add_argument("--since", help="only listings activated on/after YYYY-MM-DD")
    ap.add_argument("--listing", type=int, help="audit one listing id")
    ap.add_argument("--quiet", action="store_true", help="one line per listing")
    ap.add_argument("--accept", action="store_true", help="record every clean listing as reviewed")
    ap.add_argument("--reseed-usage", action="store_true",
                    help="rebuild caching/tag_usage.json from every live listing")
    args = ap.parse_args()

    client = EtsyClient()
    shop_id = client.get_shop_id()
    every = active_listings(client, shop_id)

    # Saturation is a property of the WHOLE shop, so it is always counted over
    # every live listing — never over the filtered subset being reported on.
    usage = Counter()
    for L in every:
        for t in (L.get("tags") or []):
            usage[str(t).strip().lower()] += 1

    reviewed = set(_load(LEDGER, {"reviewed": []}).get("reviewed", []))
    pins = load_dev_pins()
    on_printify = printify_ids()
    if on_printify is None:
        print(f"\n  ! {PRINTIFY_MAP_FILE} missing — run `python -m scripts.etsy.redescribe --check` once to "
              f"build it. Treating every listing as Printify; Printful ones will false-flag.")
        on_printify = {L["listing_id"] for L in every}

    # Descriptions: the same QA redescribe.py runs, over the WHOLE shop (the
    # duplicate-prose test needs every listing, like saturation does). Imported
    # here, not at module level — redescribe.py imports this module.
    from scripts.etsy import redescribe
    desc_faults = redescribe.qa_descriptions(every, pins, on_printify)

    rows = every
    if args.listing:
        rows = [L for L in rows if L["listing_id"] == args.listing]
    if args.new:
        rows = [L for L in rows if L["listing_id"] not in reviewed]
    if args.since:
        cut = dt.datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()
        rows = [L for L in rows if (L.get("state_timestamp") or 0) >= cut]

    print(f"\n  shop {shop_id} · {len(every)} active · auditing {len(rows)} · "
          f"{len(reviewed)} previously reviewed\n")

    dirty, clean_ids, dev_designs = 0, [], []
    for L in sorted(rows, key=lambda r: r.get("state_timestamp") or 0, reverse=True):
        lid = L["listing_id"]
        title = html.unescape(L.get("title", ""))
        tags = [str(t).strip().lower() for t in (L.get("tags") or [])]
        garment = garment_of(title)

        is_dev = is_dev_design(lid, title, pins)
        if is_dev:
            dev_designs.append(lid)
        provider = "printify" if lid in on_printify else "printful"
        tag_problems, notes = scan_tags(tags, usage, is_dev)
        place_problems, place_notes = scan_placement(L, garment, provider, client)
        notes += place_notes
        problems = (scan_title(title, garment) + tag_problems + place_problems
                    + [f"description: {f}" for f in desc_faults.get(lid, [])])
        desc_brands = find_brands(html.unescape(L.get("description", "")))
        if desc_brands:
            problems.append(f"description: AI brand name(s) "
                            f"{', '.join(sorted(set(desc_brands)))} — trademark risk")

        if problems:
            dirty += 1
        else:
            clean_ids.append(lid)

        if args.quiet:
            if problems:
                print(f"  ✗ {lid}  {len(problems):>2} defect(s)  {title[:52]}")
            continue

        mark = "✓" if not problems else "✗"
        print(f"  {mark} {lid}  activated {_ts(L.get('state_timestamp'))}  "
              f"{L.get('views', 0):>3}v  {garment}  {provider}")
        print(f"      {title[:96]}")
        for p in problems:
            print(f"      ! {p}")
        for n in notes:
            print(f"      · {n}")
        print()

    print(f"  ── {len(rows) - dirty} clean · {dirty} with defects ──")
    if desc_faults:
        print(f"  ── descriptions: {len(every) - len(desc_faults)}/{len(every)} pass · "
              f"{len(desc_faults)} need `redescribe.py` ──")
    else:
        print(f"  ── descriptions: {len(every)}/{len(every)} pass ──")

    over = [(t, n) for t, n in usage.most_common() if n > TAG_SATURATION_LIMIT]
    banned_live = [(t, n) for t, n in usage.most_common() if t in HARD_BANNED]
    dev_live = [(t, n) for t, n in usage.most_common() if t in DEV_TAGS]
    if over:
        print(f"\n  Over-saturated shop-wide (limit {TAG_SATURATION_LIMIT}):")
        for t, n in over:
            print(f"    {n:>3}x  {t}{'  ← banned too' if t in HARD_BANNED else ''}")
    if banned_live:
        print(f"\n  Hard-banned tags still live ({sum(n for _, n in banned_live)} instances):")
        for t, n in banned_live:
            print(f"    {n:>3}x  {t}")
    if dev_live:
        # Not a failure — developer designs are still sold. This is the drift
        # gauge: how loudly the shop as a whole reads as a developer store.
        share = len(dev_designs) / len(every) * 100 if every else 0
        print(f"\n  Developer signal: {len(dev_designs)} of {len(every)} listings "
              f"({share:.0f}%) are developer designs · "
              f"{sum(n for _, n in dev_live)} developer tag instances")
        for t, n in dev_live[:8]:
            print(f"    {n:>3}x  {t}")

    if args.reseed_usage:
        # retag.py's seed_usage_ledger() rebuilds this from caching/tags_after.json,
        # a snapshot of the 29 taken in Aug 2026. Rebuilding from what is actually
        # live is the only version that stays true as batches are added.
        from services.ai_content import TAG_USAGE_FILE
        TAG_USAGE_FILE.parent.mkdir(exist_ok=True)
        TAG_USAGE_FILE.write_text(json.dumps(dict(usage.most_common()), indent=2),
                                  encoding="utf-8")
        print(f"\n  Reseeded {TAG_USAGE_FILE} from {len(every)} live listings "
              f"({len(usage)} distinct tags, {sum(usage.values())} uses).")

    if args.accept and clean_ids:
        merged = sorted(reviewed | set(clean_ids))
        _save(LEDGER, {"reviewed": merged, "updated": dt.date.today().isoformat()})
        print(f"\n  Recorded {len(clean_ids)} clean listing(s). Ledger now holds {len(merged)}.")

    return dirty


if __name__ == "__main__":
    sys.exit(main())
