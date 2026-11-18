"""
section.py — Create the shop sections and file all 29 live listings into them
=============================================================================
WHY

All 29 live listings sat in NO section, so the shop page was one undifferentiated
wall of 29 products. Sections are the only navigation Etsy gives a shop page.

THE THREE SECTIONS

Three, not four. Every scheme with four left a token bucket of two or three
leftovers that did not belong together — "Tech Enthusiast" in particular, which
names a BUYER rather than a joke and was where the retired developer positioning
would have quietly reassembled.

  AI Humor                 16   the shop's actual niche: ChatGPT, prompts,
                                hallucinating, arguing with the bot
  Introvert & Overthinker   7   404 Social Skills, Social Battery Low, Ctrl Z,
                                Powered By Overthinking.exe
  Office & Work Humor       6   Copy Paste, Chatbot Meeting, Wake Me Up When The
                                Code Works, Ctrl Alt Delighted

Ctrl Z and Overthinking.exe are overthinking jokes, not tech jokes — merging them
with the introvert designs is what dissolves the weak fourth bucket. Ctrl Alt
Delighted was the orphan in every arrangement; Ctrl+Alt+Del is office-computer
vocabulary, so Office & Work is its least-forced home.

TWO FORCED CALLS

Etsy allows exactly ONE section per listing (`shop_section_id` is a single int),
so two designs that legitimately belong in two places had to be assigned:

  • Chatbot Meeting  — AI *and* office. Filed under Office & Work, matching its
    own title, which leads "Funny Meeting Humor Sweatshirt, Office Life Crewneck".
  • 404 Social Skills — introvert *and* tech. Filed under Introvert &
    Overthinker, matching its title lead "Funny Introvert Shirt".

NAME LENGTH

Etsy caps section titles at 24 characters. "Overthinkers & Introverts" is 25 and
is rejected, hence the singular "Introvert & Overthinker" (23).

IDEMPOTENT

Sections are matched by title before creating, because Etsy does NOT dedupe: a
second POST with the same title makes a second identical section that is
indistinguishable in the UI. Re-running this script re-uses what already exists.

USAGE
  python -m scripts.etsy.section                # dry run — shows the plan, writes nothing
  python -m scripts.etsy.section --apply        # create sections and file every listing
  python -m scripts.etsy.section --verify       # re-read from Etsy and confirm the placement
  python -m scripts.etsy.section --audit        # what sections exist and what is in them

VERIFYING
  --verify re-reads every live listing's shop_section_id from Etsy and checks it
  against the plan, and separately confirms no listing was left unfiled. It does
  not trust this script's memory of what it wrote.

  Runs append to caching/section_log.txt. --apply saves each listing's previous
  shop_section_id to caching/section_backup_<timestamp>.json first.
"""

from __future__ import annotations

import argparse
import collections
import html
import json
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from services.etsy_service import ETSY_SECTION_TITLE_MAX, EtsyClient, EtsyError

CACHE_DIR = Path("caching")
LOG_FILE = CACHE_DIR / "section_log.txt"

AI = "AI Humor"
INTROVERT = "Introvert & Overthinker"
OFFICE = "Office & Work Humor"

SECTION_ORDER = (AI, INTROVERT, OFFICE)

# listing_id -> section title. Pairs (tee + sweatshirt of one design) always land
# in the SAME section — a pair is one joke on two garments.
PLAN: dict[int, str] = {
    # ── AI Humor (49) ───────────────────────────────────────────────────────
    4513682953: AI,   # AI Overthinking · tee
    4545634438: AI,   # AI Overthinking · sweatshirt
    4525458859: AI,   # I Hallucinate With Confidence · tee
    4548389600: AI,   # I Hallucinate With Confidence · sweatshirt
    4532805381: AI,   # Be Polite To The AI · tee
    4551025574: AI,   # Be Polite To The AI · sweatshirt
    4532808742: AI,   # Prompt Regret Repeat · tee
    4551832314: AI,   # Prompt Regret Repeat · sweatshirt
    4532817446: AI,   # Blame The Prompt · tee
    4551016877: AI,   # Blame The Prompt · sweatshirt
    4555797226: AI,   # AI Productivity
    4556162450: AI,   # My Prompts Are Longer Than My Answers
    4551798209: AI,   # The AI Said It Probably True
    4552547847: AI,   # My Prompt History Is a Crime Scene
    4551834830: AI,   # Prompt It Till You Make It
    4555786273: AI,   # My Brain Loading AI Already Done

    # ── 29 Aug batch · everyday-AI (8) ──────────────────────────────────────
    4548361080: AI,   # Retrying My Prompt 4th Time
    4548361232: AI,   # Processing Robot / thought bubble
    4549100904: AI,   # AI Said Please Just In Case
    4549112560: AI,   # Prompt Engineering Send Help
    4550350577: AI,   # My AI Hallucinated My Entire Career
    4550361548: AI,   # Technically The AI Wrote This
    4550361838: AI,   # Powered By AI And Mild Panic
    4551821327: AI,   # Saying Please To The AI Just In Case

    # ── 30 Aug batch · everyday-AI (8) ──────────────────────────────────────
    4532801689: AI,   # AI Said It Probably True · tee
    4532802063: AI,   # Out Of Tokens Full Of Opinions · tee
    4532806751: AI,   # Technically The AI Wrote This · tee  (pairs 4550361548)
    4545635796: AI,   # I Break AI
    4546896918: AI,   # Robot Fortune Teller / Ask Me Anything
    4546898934: AI,   # It's Not Broken You Wrote A Bad Prompt
    4546901580: AI,   # Fine-Tuning My Prompts
    4546903574: AI,   # Debugging My Prompts Again

    # ── 30 Aug batch 4 · tee twins, everyday-AI (9) ─────────────────────────
    4529256253: AI,   # I Said Please Just In Case · tee   (pairs 4549100904)
    4529256751: AI,   # Prompt Pray Regenerate · tee       (pairs 4551825881)
    4529260987: AI,   # 100 Percent Confident Zero Sure    (pairs 4550362668)
    4529271518: AI,   # Hallucinating Responsibly · tee    (pairs 4551805357)
    4529273482: AI,   # Powered By AI And Mild Panic · tee (pairs 4550361838)
    4532802365: AI,   # I Don't Hallucinate I Improvise    (pairs 4551770331)
    4532802629: AI,   # I Fear My Chat History · tee       (pairs 4551771273)
    4532807965: AI,   # My AI Hallucinated My Career · tee (pairs 4550350577)
    4532812984: AI,   # Prompt It Till You Make It · tee   (pairs 4551834830)

    # ── 30 Aug batch 5 · tee twins, everyday-AI (6) ─────────────────────────
    4513688433: AI,   # I Break AI Then Try Again · tee    (pairs 4545635796)
    4513704707: AI,   # Debugging My Prompts Again · tee   (pairs 4546903574)
    4513721857: AI,   # Please Rephrase Your Prompt · tee  (own design)
    4513727456: AI,   # It's Just A Bad Prompt · tee       (pairs 4546898934)
    4525455789: AI,   # Prompt Engineering Send Help · tee (pairs 4549112560)
    4525457113: AI,   # Please To The Robot Just In Case   (pairs 4549100904)

    # ── 30 Aug batch 6 · final batch (2) ────────────────────────────────────
    4513733845: AI,   # Ask Me Anything Feeling Predictive (pairs 4546896918)
    4513747408: AI,   # Retrying My Prompt 4th Time · tee  (pairs 4548361080)

    # ── Introvert & Overthinker (18) ─────────────────────────────────────────
    4464482796: INTROVERT,   # 404 Social Skills Not Found · tee   (forced call)
    4544413718: INTROVERT,   # 404 Social Skills Not Found · sweatshirt
    4544413224: INTROVERT,   # Social Battery Low / Recharging
    4532816690: INTROVERT,   # Ctrl Z Undo Life Choices · tee
    4551781634: INTROVERT,   # Ctrl Z Undo Life Choices · sweatshirt
    4529259987: INTROVERT,   # Powered By Overthinking.exe · tee
    4551803523: INTROVERT,   # Powered By Overthinking.exe · sweatshirt

    # ── 30 Aug batch · lifestyle, non-AI (5) ────────────────────────────────
    4544001789: INTROVERT,   # Stay Cozy  (no joke — see report)
    4544395095: INTROVERT,   # Energy Optimized Sloth
    4544396865: INTROVERT,   # Selectively Social
    4544920297: INTROVERT,   # One Of A Kind Monster
    4544935902: INTROVERT,   # Intellectual Badass

    # ── 30 Aug batch 4 · tee twin (1) ───────────────────────────────────────
    4529271196: INTROVERT,   # Powered By Artificial Confidence · tee (pairs 4551813654)

    # ── 30 Aug batch 5 · tee twins, lifestyle (3) ───────────────────────────
    4450734196: INTROVERT,   # Recharging Introvert · tee     (pairs 4544413224)
    4458951221: INTROVERT,   # Energy Optimized Sloth · tee   (pairs 4544395095)
    4458964742: INTROVERT,   # One Of A Kind Monster · tee    (pairs 4544920297)

    # ── 30 Aug batch 6 · final batch (2) ────────────────────────────────────
    4434924023: INTROVERT,   # Intellectual Badass · tee      (pairs 4544935902)
    4434986834: INTROVERT,   # Selectively Social Programmer  (pairs 4544396865)

    # ── Office & Work Humor (32) ─────────────────────────────────────────────
    4532804475: OFFICE,      # Powered By Copy Paste · tee
    4551015747: OFFICE,      # Powered By Copy Paste · sweatshirt
    4556788708: OFFICE,      # Chatbot Meeting                     (forced call)
    4546881437: OFFICE,      # Wake Me Up When The Code Works
    4505970613: OFFICE,      # Ctrl Alt Delighted · tee
    4546881347: OFFICE,      # Ctrl Alt Delighted · sweatshirt

    # ── 29 Aug batch · developer designs + retro/work (7) ───────────────────
    4546887107: OFFICE,      # Everything Is Fine / burning laptop
    4549098693: OFFICE,      # Save It To A Floppy Disk (retro tech)
    4548377543: OFFICE,      # I Pushed To Main On A Friday        (dev)
    4548390138: OFFICE,      # I Run On Coffee And Code            (dev)
    4548391200: OFFICE,      # Merged Without Testing              (dev)
    4549086581: OFFICE,      # Programmer Code Error               (dev)
    4551016281: OFFICE,      # My Code Compiles Out Of Fear        (dev)

    # ── 30 Aug batch · developer designs (2) ────────────────────────────────
    4545618869: OFFICE,      # I Don't Break Code, I Power Up      (dev)
    4546897800: OFFICE,      # Build The Future One Line Of Code   (dev)

    # ── 30 Aug batch 4 · tee twins, dev + retro (5) ─────────────────────────
    4525452687: OFFICE,      # Wake Me Up When The Code Works (dev, pairs 4546881437)
    4525453263: OFFICE,      # Vibe Coded Prayed Deployed     (dev, no twin)
    4529259611: OFFICE,      # My Code Compiles Out Of Fear   (dev, pairs 4551016281)
    4529268716: OFFICE,      # Spaghetti Code · tee           (dev, pairs 4551830572)
    4529272098: OFFICE,      # Ctrl Alt Rebooted My Mood · tee (pairs 4551784776)

    # ── 30 Aug batch 5 · tee twins, dev + work/retro (6) ────────────────────
    4513724283: OFFICE,      # Everything Is Fine · tee        (pairs 4546887107)
    4525454875: OFFICE,      # Trained It On My Code    (dev, pairs 4549086581)
    4525456017: OFFICE,      # Save It To A Floppy Disk · tee  (pairs 4549098693)
    4525456711: OFFICE,      # Coffee And Code          (dev, pairs 4548390138)
    4525466600: OFFICE,      # Pushed To Main On Friday (dev, pairs 4548377543)
    4525467542: OFFICE,      # Merged Without Testing   (dev, pairs 4548391200)

    # ── 30 Aug · renewed from expired (2) ───────────────────────────────────
    4432638406: OFFICE,      # There's No Place Like 127.0.0.1     (dev)
    4433996964: OFFICE,      # A Perfect Day Is A Day Of Coding    (dev)

    # ── 30 Aug batch 6 · final batch (4) ────────────────────────────────────
    4432534143: OFFICE,      # No Place Like 127.0.0.1 · HOODIE    (dev)
    4432645676: OFFICE,      # Hello World · tee                   (dev)
    4505505517: OFFICE,      # Funny Programmer / Coding Humor      (dev)
    4513730491: OFFICE,      # Build The Future One Line Of Code    (dev)
}


def _log(line: str = "") -> None:
    print(line)
    CACHE_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def live_listings(client: EtsyClient, shop_id: int) -> list[dict]:
    """Every active listing, paged. Includes shop_section_id and title."""
    rows, offset = [], 0
    while True:
        # NOT /listings/active — that endpoint is served from Etsy's SEARCH
        # INDEX, which lags activation by minutes to hours. On 30 Aug it showed
        # 54 while the shop really had 69 live, hiding a whole 15-listing batch
        # and reporting "auditing 0". getListingsByShop with state=active reads
        # the shop record directly and is correct the instant a listing goes
        # live. Same fields, same shape.
        r = client._request("GET", f"/application/shops/{shop_id}/listings",
                            params={"state": "active", "limit": 100, "offset": offset})
        page = r.get("results", []) or []
        rows += page
        if len(rows) >= r.get("count", 0) or not page:
            break
        offset += 100
    return rows


def resolve_sections(client: EtsyClient, shop_id: int, apply: bool) -> dict[str, int]:
    """Title -> shop_section_id, creating any that do not exist yet.

    Matching on title first is what makes re-running safe. Etsy happily creates a
    second section with an identical name, and there is no way to tell the two
    apart afterwards from the seller UI.
    """
    existing = {s["title"]: s["shop_section_id"] for s in client.get_shop_sections(shop_id)}
    out = {}
    for title in SECTION_ORDER:
        if len(title) > ETSY_SECTION_TITLE_MAX:
            raise EtsyError(f"Section title too long for Etsy: {title!r}")
        if title in existing:
            out[title] = existing[title]
            _log(f"  ✓ section exists   {existing[title]:>12}  {title}")
        elif apply:
            s = client.create_shop_section(shop_id, title)
            out[title] = s["shop_section_id"]
            _log(f"  + section CREATED  {s['shop_section_id']:>12}  {title}")
            time.sleep(0.3)
        else:
            out[title] = 0
            _log(f"  + would create                  {title}")
    return out


def run(apply: bool) -> int:
    client = EtsyClient()
    shop_id = client.get_shop_id()

    _log()
    _log("=" * 74)
    _log(f"  Sections  ·  {'APPLY' if apply else 'DRY RUN'}  ·  "
         f"{datetime.now():%Y-%m-%d %H:%M:%S}  ·  shop {shop_id}")
    _log("=" * 74)
    _log()

    ids = resolve_sections(client, shop_id, apply)
    _log()

    rows = live_listings(client, shop_id)
    by_id = {L["listing_id"]: L for L in rows}

    unplanned = [lid for lid in by_id if lid not in PLAN]
    missing = [lid for lid in PLAN if lid not in by_id]
    if unplanned:
        _log(f"  ⚠️  {len(unplanned)} live listing(s) not in the plan: {unplanned}")
    if missing:
        _log(f"  ⚠️  {len(missing)} planned listing(s) not live: {missing}")
    if unplanned or missing:
        _log()

    backup, changed, skipped, failed = {}, 0, 0, 0

    for title in SECTION_ORDER:
        members = [lid for lid, t in PLAN.items() if t == title and lid in by_id]
        _log(f"  ── {title}  ({len(members)} listings)")
        for lid in members:
            L = by_id[lid]
            now = L.get("shop_section_id")
            want = ids[title]
            name = html.unescape(L.get("title", ""))[:56]
            backup[str(lid)] = now

            if now and want and now == want:
                _log(f"     ✓ {lid}  already filed        {name}")
                skipped += 1
                continue

            _log(f"     → {lid}  {str(now or 'unfiled'):>12} → {want or '(new)':>12}  {name}")
            if not apply:
                changed += 1
                continue

            try:
                client.set_listing_section(shop_id, lid, want)
            except EtsyError as exc:
                _log(f"       ✗ WRITE FAILED: {exc}")
                failed += 1
                continue
            changed += 1
            time.sleep(0.3)
        _log()

    if apply and backup:
        CACHE_DIR.mkdir(exist_ok=True)
        path = CACHE_DIR / f"section_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(backup, indent=2), encoding="utf-8")
        _log(f"  previous section ids saved to {path}")

    _log("=" * 74)
    _log(f"  {'filed' if apply else 'would file':<20}: {changed}")
    _log(f"  already correct     : {skipped}")
    _log(f"  failed              : {failed}")
    _log("=" * 74)
    if not apply and changed:
        _log("  Nothing was written. Re-run with --apply to commit.")
    _log()
    return 1 if failed else 0


def verify() -> int:
    """Re-read placement from Etsy. Does not trust what this script thinks it wrote."""
    client = EtsyClient()
    shop_id = client.get_shop_id()
    ids = {s["title"]: s["shop_section_id"] for s in client.get_shop_sections(shop_id)}
    rows = live_listings(client, shop_id)

    _log()
    _log("=" * 74)
    _log(f"  Verify sections  ·  {datetime.now():%Y-%m-%d %H:%M:%S}")
    _log("=" * 74)
    _log()

    want_id = {t: ids.get(t) for t in SECTION_ORDER}
    for t, i in want_id.items():
        _log(f"  {t:<26} id={i}")
    _log()

    ok, bad, unfiled = 0, 0, 0
    groups = collections.defaultdict(list)

    for L in rows:
        lid = L["listing_id"]
        now = L.get("shop_section_id")
        name = html.unescape(L.get("title", ""))[:54]
        want_title = PLAN.get(lid)
        want = want_id.get(want_title)

        if not now:
            _log(f"  ✗ {lid}  UNFILED — no section          {name}")
            unfiled += 1
            continue
        if want and now == want:
            groups[want_title].append(name)
            ok += 1
        else:
            actual = next((t for t, i in ids.items() if i == now), f"id {now}")
            _log(f"  ✗ {lid}  in {actual!r}, expected {want_title!r}   {name}")
            bad += 1

    for t in SECTION_ORDER:
        _log(f"\n  ── {t}  ({len(groups[t])})")
        for n in sorted(groups[t]):
            _log(f"       {n}")

    _log()
    _log("=" * 74)
    _log(f"  correctly filed : {ok} / {len(rows)}")
    _log(f"  wrong section   : {bad}")
    _log(f"  unfiled         : {unfiled}")
    _log("=" * 74)
    _log()
    return 1 if (bad or unfiled) else 0


def audit() -> int:
    """What sections exist right now and what is sitting in each. Read-only."""
    client = EtsyClient()
    shop_id = client.get_shop_id()
    sections = client.get_shop_sections(shop_id)
    rows = live_listings(client, shop_id)

    names = {s["shop_section_id"]: s["title"] for s in sections}
    counts = collections.Counter(L.get("shop_section_id") for L in rows)

    _log()
    _log(f"  {len(sections)} section(s) on shop {shop_id}, {len(rows)} active listings")
    _log()
    for sid, title in names.items():
        _log(f"  {sid:>12}  {title:<26} {counts.get(sid, 0):>3} listings")
    if counts.get(None):
        _log(f"  {'—':>12}  {'(no section)':<26} {counts[None]:>3} listings")
    _log()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Create shop sections and file the live listings.")
    ap.add_argument("--apply", action="store_true", help="commit (default is a dry run)")
    ap.add_argument("--verify", action="store_true", help="re-read placement from Etsy")
    ap.add_argument("--audit", action="store_true", help="show existing sections, read-only")
    args = ap.parse_args()

    try:
        if args.audit:
            return audit()
        if args.verify:
            return verify()
        return run(args.apply)
    except EtsyError as exc:
        print(f"\n✗ Etsy: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
