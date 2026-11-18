"""
retitle.py — Repair the title defects on the live Etsy listings
===============================================================
WHAT THIS FIXES

Nine live listings carry titles written before the shop's SEO rules were
settled. The defects fall into four groups:

  1. PIPE FORMAT — `A | B | C`. Etsy's title validator rejects `|` outright, so
     these are not merely old-style, they are unsaveable the moment anything
     else on the title changes.
  2. FILLER — `High QualityT-Shirt` (and a missing space). "High quality" has no
     search value; _TITLE_RULES in services/ai_content.py bans it explicitly.
  3. WRONG PRODUCT NAMED — a t-shirt whose title ends "Syntax Coffee Mug", and a
     crewneck sold as "Unisex Nerdy Long Sleeve". Both mislead the buyer and
     match the wrong queries.
  4. BUILD ARTEFACTS — a trailing `V2.0`, and one half of a pair named
     "I Hallucinate…" while the other half is "AI Hallucinate…".

Every one of the nine also breaks the rule that carries the actual SEO weight:
THE FIRST 40 CHARACTERS DECIDE WHETHER THE LISTING IS SEEN. All nine open with
the design's punchline instead of the phrase a shopper types. Repairing only the
cosmetic defects would leave that untouched, so each title is rebuilt to the
order _TITLE_RULES lays out: primary search phrase, secondary phrase, the joke,
a gift phrase, garment words last.

The nine leads are deliberately all DIFFERENT ("Funny AI Shirt", "Funny Chatbot
Sweatshirt", "Funny Introvert Sweatshirt", …). Three of these are tees that
would otherwise all have opened "Funny AI Shirt" and competed with each other
for one slot in Etsy search — the same cannibalisation the tag work is undoing.

THE NICHE TRANSITION (the remaining 20, added 26 Aug)

WhatToDo.md item 5 settled the audience question: Nullish sells to people who USE
AI, not the engineers who build it. The tag pass rewrote all 29 listings to that
audience, which left the shop half-transitioned — 20 titles still advertised
"Programmer Gift", "Software Engineer Gift", "Machine Learning" while their own
tags targeted students and office workers. Etsy weighs both fields, so a listing
that disagrees with itself about who it is for is a worse signal than either
state alone. These 20 close that gap.

EVERY DESIGN IS KEPT. Nothing here redraws, replaces or retires artwork — the
decision was explicitly "retag and retitle, do not redraw". Two designs needed a
judgement call rather than a translation:

  • 4546881437 "Wake Me Up When The Code Works" — the one design whose joke is
    genuinely about code and cannot be sold to AI users without mismatching buyer
    intent. Framed as generic burnout/work-frustration humour instead. The slogan
    on the garment is unchanged; only the search framing around it moved.
  • 4544413224 "Social Battery Low" — never had an AI angle at all. Sold as
    introvert/homebody humour, which is what it honestly is.

All 29 leads are distinct. The pairs split on the garment word and nothing else
("Funny Chatbot Shirt" vs "Funny Chatbot Sweatshirt"), which is the same rule the
tag work landed on: a pair is one joke on two garments, so it should share its
design and audience terms and differ only where the garment is named.

Descriptions, tags, price, images, artwork and state are untouched. This writes
exactly one field per listing.

USAGE
  python -m scripts.etsy.retitle                  # dry run — prints every before/after, writes nothing
  python -m scripts.etsy.retitle --apply          # commit the rewrites
  python -m scripts.etsy.retitle --verify         # re-read the live titles and grade them
  python -m scripts.etsy.retitle --apply --only 4513682953   # one listing, for a smoke test

VERIFYING
  --verify is the check. It re-reads each title from Etsy, un-escapes it, and
  confirms character-for-character that the live value equals the intended one,
  then re-runs the defect scan (pipe, filler, banned chars, length, wrong
  garment) against whatever is actually live. A green run means the rename
  landed; it does not depend on this script's own memory of what it sent.

  Every run appends to caching/retitle_log.txt, and --apply writes the previous
  titles to caching/title_backup_<timestamp>.json before touching anything.
"""

from __future__ import annotations

import argparse
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

from services.etsy_service import (
    ETSY_TITLE_BANNED_CHARS,
    ETSY_TITLE_MAX,
    EtsyClient,
    EtsyError,
)

CACHE_DIR = Path("caching")
LOG_FILE = CACHE_DIR / "retitle_log.txt"

# Etsy's own floor for a title that ranks. Shorter than ~125 chars and slots that
# could hold a search phrase are sitting empty.
TITLE_MIN_USEFUL = 120

# Filler phrases with no search value — banned by _TITLE_RULES in ai_content.py.
FILLER = ("high quality", "premium", "limited edition", "best seller", "hot sale")


# ── The rewrites ─────────────────────────────────────────────────────────────
# Each entry: listing_id -> (new title, garment, why it is being changed).
# `garment` drives the wrong-product check in the defect scan: a "tee" title must
# not advertise a sweatshirt or a mug, and vice versa.
RETITLES: dict[int, tuple[str, str, str]] = {
    # ── group 1: pipe format ────────────────────────────────────────────────
    4513682953: (
        "Funny AI Shirt, Chatbot Humor Tee, AI Overthinking So You Don't Have To, "
        "Gift for Overworked Coworker or Student, Unisex Graphic Tee",
        "tee",
        "pipe format + 'High QualityT-Shirt' filler and missing space; "
        "top-viewed tee (12 views)",
    ),
    4555786273: (
        "Funny AI Sweatshirt, Artificial Intelligence Humor Crewneck, "
        "My Brain Loading AI Already Done, Gift for College Student, Unisex Crewneck",
        "sweatshirt",
        "pipe format",
    ),
    4551803523: (
        "Funny Overthinker Sweatshirt, Anxiety Humor Crewneck, "
        "Powered By Overthinking Exe, Gift for Stressed Coworker, Unisex Crewneck Sweatshirt",
        "sweatshirt",
        "pipe format (mixed with commas)",
    ),
    4551781634: (
        "Funny Sarcastic Sweatshirt, Relatable Humor Crewneck, "
        "Ctrl Z Undoing My Life Choices, Gift for Coworker or Student, Unisex Crewneck",
        "sweatshirt",
        "pipe format",
    ),

    # ── group 2: wrong product named ────────────────────────────────────────
    4529259987: (
        "Funny Overthinking Shirt, Sarcastic Tech Humor Tee, Powered By Overthinking Exe, "
        "Gift for Stressed Coworker, Unisex Graphic Tee",
        "tee",
        "title ended 'Syntax Coffee Mug' on a t-shirt listing",
    ),
    4544413718: (
        "Funny Introvert Sweatshirt, Antisocial Humor Crewneck, 404 Social Skills Not Found, "
        "Gift for Introvert Friend, Unisex Crewneck Sweatshirt",
        "sweatshirt",
        "crewneck advertised as 'Unisex Nerdy Long Sleeve'",
    ),

    # ── group 3: build artefacts ────────────────────────────────────────────
    4532804475: (
        "Funny Office Humor Shirt, Sarcastic Work Tee, Powered By Copy Paste System Ready, "
        "Gift for Coworker or Office Friend, Unisex Graphic Tee",
        "tee",
        "trailing 'V2.0' read as a build artefact",
    ),

    # ── group 4: pair naming split ──────────────────────────────────────────
    # The tee is the anchor (9 views); the sweatshirt is renamed to match it.
    4525458859: (
        "Funny Chatbot Shirt, AI Humor Graphic Tee, I Hallucinate With Confidence, "
        "Gift for AI Lover or College Student, Unisex Graphic Tee",
        "tee",
        "pair anchor — sweatshirt renamed to match this",
    ),
    4548389600: (
        "Funny Chatbot Sweatshirt, AI Humor Crewneck, I Hallucinate With Confidence, "
        "Gift for AI Lover or Student, Unisex Crewneck Sweatshirt",
        "sweatshirt",
        "was 'AI Hallucinate…' while its tee is 'I Hallucinate…'; also drops the "
        "banned 'Machine Learning' / 'Data Scientist' phrases",
    ),

    # ══ group 5: the niche transition — 20 titles still selling the old buyer ══
    # Added 26 Aug once WhatToDo.md item 5 was decided. Each of these carried
    # "Programmer" / "Developer" / "Software Engineer" / "Machine Learning" /
    # "Data Scientist" while its tags already targeted AI users.

    # ── paired designs (the other half was done in group 1-4) ───────────────
    4545634438: (
        "AI Humor Sweatshirt, Artificial Intelligence Crewneck, "
        "AI Overthinking So You Don't Have To, Gift for Overworked Coworker, Unisex Crewneck",
        "sweatshirt",
        "'Robot Programmer Gift, Code Debugging, for Developers' — pairs with 4513682953",
    ),
    4464482796: (
        "Funny Introvert Shirt, Antisocial Humor Graphic Tee, 404 Social Skills Not Found, "
        "Gift for Introvert Friend, Unisex Graphic Tee",
        "tee",
        "'Funny Programmer Shirt, Software Engineer Gift, Coding Humor' — "
        "pairs with 4544413718",
    ),
    4532805381: (
        "Funny Chatbot Shirt, AI Humor Graphic Tee, Be Polite To The AI Just In Case, "
        "Gift for AI Lover or Coworker, Unisex Graphic Tee",
        "tee",
        "'Programmer Gift, Data Scientist Shirt, Machine Learning'",
    ),
    4551025574: (
        "Funny Chatbot Sweatshirt, AI Humor Crewneck, Be Polite To The AI Just In Case, "
        "Gift for AI Lover or Coworker, Unisex Crewneck Sweatshirt",
        "sweatshirt",
        "'Gift For Programmers' — pairs with 4532805381",
    ),
    4505970613: (
        "Retro Tech Humor Shirt, Cute Vintage Computer Graphic Tee, Ctrl Alt Delighted, "
        "Gift for Coworker or College Student, Unisex Graphic Tee",
        "tee",
        "ALL-CAPS 'CTRL + ALT + DELIGHTED' (Printify rejects excessive caps) plus "
        "'for Programmers, Software Engineer Gift'",
    ),
    4546881347: (
        "Retro Tech Humor Sweatshirt, Cute Vintage Computer Crewneck, Ctrl Alt Delighted, "
        "Gift for Coworker or College Student, Unisex Crewneck",
        "sweatshirt",
        "'Programmer Code Lover Shirt' — pairs with 4505970613",
    ),
    4532808742: (
        "Funny Prompt Humor Shirt, Chatbot Lover Graphic Tee, Prompt Regret Repeat, "
        "Gift for College Student or Coworker, Unisex Graphic Tee",
        "tee",
        "'AI Engineer Tee, Developer Humor, Funny Coding'",
    ),
    4551832314: (
        "Funny Prompt Humor Sweatshirt, Chatbot Lover Crewneck, Prompt Regret Repeat, "
        "Gift for College Student or Coworker, Unisex Crewneck",
        "sweatshirt",
        "'Engineer Crewneck, Programmer Developer Shirt' — pairs with 4532808742",
    ),
    4532817446: (
        "Sarcastic AI Shirt, Funny Chatbot Humor Graphic Tee, AI Blame The Prompt, "
        "Gift for Coworker or College Student, Unisex Graphic Tee",
        "tee",
        "'Prompt Engineer, Developer Coder Gift, Machine Learning'",
    ),
    4551016877: (
        "Sarcastic AI Sweatshirt, Funny Chatbot Humor Crewneck, AI Blame The Prompt, "
        "Gift for Coworker or College Student, Unisex Crewneck",
        "sweatshirt",
        "'Prompt Engineer Gift, Developer Programmer' — pairs with 4532817446",
    ),
    4551015747: (
        "Funny Office Humor Sweatshirt, Sarcastic Work Crewneck, "
        "Powered By Copy Paste System Ready, Gift for Coworker, Unisex Crewneck Sweatshirt",
        "sweatshirt",
        "'Funny Programmer Developer Gift' — pairs with 4532804475",
    ),
    4532816690: (
        "Funny Sarcastic Shirt, Relatable Humor Graphic Tee, Ctrl Z Undo Life Choices, "
        "Gift for Coworker or Student, Unisex Graphic Tee",
        "tee",
        "'Funny Programmer Tee, IT Worker, Developer Graphic Tee' — "
        "pairs with 4551781634",
    ),

    # ── sweatshirt-only designs ─────────────────────────────────────────────
    4555797226: (
        "Funny Productivity Sweatshirt, AI Humor Crewneck, AI Productivity Assistant Joke, "
        "Gift for Overworked Coworker, Unisex Crewneck Sweatshirt",
        "sweatshirt",
        "'Funny Programmer Gift, Prompt Engineer Shirt, Developer AI Assistant'",
    ),
    4556162450: (
        "Funny AI User Sweatshirt, Chatbot Humor Crewneck, "
        "My Prompts Are Longer Than My Answers, Gift for Student, Unisex Crewneck Sweatshirt",
        "sweatshirt",
        "led with 'AI Prompt Engineer Sweatshirt' — the wrong buyer in the first 40 chars",
    ),
    4556788708: (
        "Funny Meeting Humor Sweatshirt, Office Life Crewneck, Chatbot Meeting Survival, "
        "Gift for Tired Coworker, Unisex Crewneck Sweatshirt",
        "sweatshirt",
        "no banned words, but opened with the punchline and wasted the first 40 chars",
    ),
    4551798209: (
        "Funny Tech Humor Sweatshirt, Chatbot Believer Crewneck, "
        "The AI Said It Probably True, Gift for Coworker or Student, Unisex Crewneck",
        "sweatshirt",
        "'Programmer Gift, Data Scientist Shirt'",
    ),
    4552547847: (
        "Funny AI Addict Sweatshirt, Chatbot Humor Crewneck, "
        "My Prompt History Is a Crime Scene, Gift for Student, Unisex Crewneck Sweatshirt",
        "sweatshirt",
        "'Funny AI Engineer Gift'",
    ),
    4551834830: (
        "Funny Student Humor Sweatshirt, Chatbot Crewneck, Prompt It Till You Make It, "
        "Gift for College Student, Unisex Crewneck Sweatshirt",
        "sweatshirt",
        "'Prompt Engineer Gift, Data Scientist Tech Crewneck'",
    ),
    # The one design that cannot make the move. Slogan kept exactly as printed;
    # only the search framing around it changes. See the docstring.
    4546881437: (
        "Funny Burnout Sweatshirt, Sarcastic Work Humor Crewneck, "
        "Wake Me Up When The Code Works, Gift for Tired Coworker, Unisex Crewneck",
        "sweatshirt",
        "legacy stock — reframed as work-frustration humour, not an AI listing",
    ),
    # Never had an AI angle; sold as what it honestly is.
    4544413224: (
        "Funny Homebody Sweatshirt, Antisocial Humor Crewneck, "
        "Social Battery Low Recharging, Gift for Introvert Friend, Unisex Crewneck",
        "sweatshirt",
        "no banned words, but opened with the punchline",
    ),
}

# Words that mean the title is advertising the wrong thing. A tee title must not
# contain the sweatshirt words and vice versa; nothing may mention a mug.
WRONG_GARMENT = {
    "tee": ("sweatshirt", "crewneck", "hoodie", "long sleeve", "mug"),
    "sweatshirt": ("t-shirt", "t shirt", "tee", "mug"),
}


def _log(line: str = "") -> None:
    print(line)
    CACHE_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def live_title(client: EtsyClient, listing_id: int) -> str:
    """The listing's title as Etsy actually stores it.

    getListing hands back HTML-escaped text, so "Don't" arrives as "Don&#39;t"
    and reads 4 characters longer than it is. Unescaping here is what makes a
    byte-for-byte comparison against what we sent meaningful.
    """
    return html.unescape(client.get_listing(listing_id).get("title", ""))


def scan_defects(title: str, garment: str) -> list[str]:
    """Every rule violation in a title. Empty list means it is clean."""
    problems = []
    low = title.lower()

    bad_chars = sorted(set(title) & ETSY_TITLE_BANNED_CHARS)
    if bad_chars:
        problems.append(f"illegal character(s) {' '.join(bad_chars)}")
    if len(title) > ETSY_TITLE_MAX:
        problems.append(f"{len(title)} chars, over Etsy's {ETSY_TITLE_MAX}")
    elif len(title) < TITLE_MIN_USEFUL:
        problems.append(f"only {len(title)} chars — wasting search slots")
    for phrase in FILLER:
        if phrase in low:
            problems.append(f"filler phrase {phrase!r}")
    for word in WRONG_GARMENT.get(garment, ()):
        if word in low:
            problems.append(f"names {word!r} but this is a {garment}")
    if "  " in title or title != title.strip():
        problems.append("stray whitespace")

    return problems


def _print_pair(listing_id: int, before: str, after: str, reason: str,
                garment: str) -> None:
    _log(f"  {listing_id}   ({garment})")
    _log(f"    why    : {reason}")
    _log(f"    before : [{len(before):>3}] {before}")
    for p in scan_defects(before, garment):
        _log(f"             ✗ {p}")
    _log(f"    after  : [{len(after):>3}] {after}")
    for p in scan_defects(after, garment):
        _log(f"             ✗ STILL BROKEN: {p}")
    _log()


def run(apply: bool, only: int | None) -> int:
    targets = {only: RETITLES[only]} if only else RETITLES
    if only and only not in RETITLES:
        print(f"✗ {only} is not one of the {len(RETITLES)} listings this script fixes.")
        return 2

    client = EtsyClient()
    shop_id = client.get_shop_id()

    _log()
    _log("=" * 74)
    _log(f"  Retitle  ·  {'APPLY' if apply else 'DRY RUN'}  ·  "
         f"{datetime.now():%Y-%m-%d %H:%M:%S}  ·  shop {shop_id}")
    _log("=" * 74)
    _log()

    backup, changed, skipped, failed = {}, 0, 0, 0

    for listing_id, (new_title, garment, reason) in targets.items():
        try:
            before = live_title(client, listing_id)
        except EtsyError as exc:
            _log(f"  {listing_id}   ✗ could not read: {exc}")
            failed += 1
            continue

        backup[str(listing_id)] = before

        if before == new_title:
            _log(f"  {listing_id}   ✓ already correct — skipping")
            _log()
            skipped += 1
            continue

        _print_pair(listing_id, before, new_title, reason, garment)

        if not apply:
            changed += 1
            continue

        try:
            client.update_listing_title(shop_id, listing_id, new_title)
        except EtsyError as exc:
            _log(f"           ✗ WRITE FAILED: {exc}")
            _log()
            failed += 1
            continue

        # Confirm from the server, not from the fact the call returned 200.
        live = live_title(client, listing_id)
        if live == new_title:
            _log("           ✓ written and confirmed live")
            changed += 1
        else:
            _log(f"           ✗ Etsy stored something else: {live!r}")
            failed += 1
        _log()
        time.sleep(0.3)          # well inside Etsy's 10 req/sec ceiling

    if apply and backup:
        CACHE_DIR.mkdir(exist_ok=True)
        path = CACHE_DIR / f"title_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(backup, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        _log(f"  previous titles saved to {path}")

    _log("=" * 74)
    verb = "retitled" if apply else "would retitle"
    _log(f"  {verb:<20}: {changed}")
    _log(f"  already correct     : {skipped}")
    _log(f"  failed              : {failed}")
    _log("=" * 74)
    if not apply and changed:
        _log("  Nothing was written. Re-run with --apply to commit.")
    _log()
    return 1 if failed else 0


def verify(only: int | None) -> int:
    """Re-read every title from Etsy and grade it. Independent of what we sent."""
    targets = {only: RETITLES[only]} if only else RETITLES
    client = EtsyClient()

    _log()
    _log("=" * 74)
    _log(f"  Verify  ·  {datetime.now():%Y-%m-%d %H:%M:%S}")
    _log("=" * 74)
    _log()

    clean, dirty = 0, 0
    for listing_id, (expected, garment, _) in targets.items():
        try:
            live = live_title(client, listing_id)
        except EtsyError as exc:
            _log(f"  ✗ {listing_id}  could not read: {exc}")
            dirty += 1
            continue

        problems = scan_defects(live, garment)
        matches = live == expected

        if matches and not problems:
            _log(f"  ✓ {listing_id}  [{len(live):>3}]  {live}")
            clean += 1
            continue

        dirty += 1
        _log(f"  ✗ {listing_id}  [{len(live):>3}]  {live}")
        if not matches:
            _log("      live title does not match the intended rewrite")
            _log(f"      intended: {expected}")
        for p in problems:
            _log(f"      ✗ {p}")
        _log()

    _log("=" * 74)
    _log(f"  clean : {clean} / {len(targets)}")
    _log(f"  dirty : {dirty}")
    _log("=" * 74)
    _log()
    return 1 if dirty else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Repair title defects on the live Etsy listings.")
    ap.add_argument("--apply", action="store_true",
                    help="commit the rewrites (default is a dry run)")
    ap.add_argument("--verify", action="store_true",
                    help="re-read live titles and grade them; writes nothing")
    ap.add_argument("--only", type=int, metavar="LISTING_ID",
                    help="act on a single listing")
    args = ap.parse_args()

    try:
        if args.verify:
            return verify(args.only)
        return run(args.apply, args.only)
    except EtsyError as exc:
        print(f"\n✗ Etsy: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
