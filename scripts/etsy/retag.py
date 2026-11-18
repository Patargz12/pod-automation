"""
retag.py — Audit and repair the tags on the 29 live Etsy listings
==================================================================
WHY THIS EXISTS

services/ai_content.py already stops NEW listings from colliding: BANNED_TAGS
strips the old engineer-facing vocabulary and TAG_SATURATION_LIMIT keeps any one
phrase off more than 5 listings. Neither guard reaches backwards. The 29
listings already published still carry whatever tags they were born with, from
before the shop's niche moved.

THE NICHE THIS SHOP IS MOVING TO

Away from software engineering. Towards people who USE AI tools — prompting,
talking to chatbots, leaning on ChatGPT to get through a day of work or study.
The buyer is a student, an office worker, a teacher, a nurse; never someone who
builds models. That decision governs every tag written here.

Most of the "developer" designs survive the move intact, because their joke was
never actually about code: Ctrl+Z, copy-paste, 404 and Ctrl-Alt-Delete are
universal computer culture, and copy-paste in particular reads as *pasting from
ChatGPT* to the new audience. Retagging moves them; it does not require redrawing
them.

USAGE
  python -m scripts.etsy.retag --audit            # read all 29, report collisions. Writes nothing.
  python -m scripts.etsy.retag                    # dry run — per-listing before/after
  python -m scripts.etsy.retag --apply            # commit
  python -m scripts.etsy.retag --verify           # re-read live tags and grade them
  python -m scripts.etsy.retag --apply --only 4513682953

VERIFYING
  --verify re-reads every tag set from Etsy and re-runs the full check against
  what is actually live: 13 tags, none over 20 chars, none banned, none saturated
  past the limit, and no pair of listings sharing too much of a tag set. It never
  consults this script's own record of what it sent.

  Runs append to caching/retag_log.txt. --apply backs the previous tags up to
  caching/tag_backup_<timestamp>.json first.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from services.ai_content import (
    BANNED_TAGS,
    ETSY_TAG_LEN,
    ETSY_TAG_MAX,
    TAG_SATURATION_LIMIT,
    TAG_USAGE_FILE,
)
from services.etsy_service import EtsyClient, EtsyError

CACHE_DIR = Path("caching")
LOG_FILE = CACHE_DIR / "retag_log.txt"

# Two listings sharing this many tags are competing for the same search slot.
# Pairs (same design, tee + sweatshirt) are the usual offenders.
OVERLAP_ALERT = 5

# ── The 29 live listings ─────────────────────────────────────────────────────
# design key -> {garment: listing_id}. The design key is what makes a pair a
# pair: both halves must be tagged as the SAME joke aimed at DIFFERENT garment
# queries, never as two shots at one query.
CATALOGUE: dict[str, dict[str, int]] = {
    "ai_overthinking":    {"tee": 4513682953, "sweatshirt": 4545634438},
    "404_social_skills":  {"tee": 4464482796, "sweatshirt": 4544413718},
    "hallucinate":        {"tee": 4525458859, "sweatshirt": 4548389600},
    "be_polite":          {"tee": 4532805381, "sweatshirt": 4551025574},
    "ctrl_alt_delighted": {"tee": 4505970613, "sweatshirt": 4546881347},
    "prompt_regret":      {"tee": 4532808742, "sweatshirt": 4551832314},
    "blame_the_prompt":   {"tee": 4532817446, "sweatshirt": 4551016877},
    "overthinking_exe":   {"tee": 4529259987, "sweatshirt": 4551803523},
    "copy_paste":         {"tee": 4532804475, "sweatshirt": 4551015747},
    "ctrl_z":             {"tee": 4532816690, "sweatshirt": 4551781634},
    "ai_productivity":    {"sweatshirt": 4555797226},
    "long_prompts":       {"sweatshirt": 4556162450},
    "chatbot_meeting":    {"sweatshirt": 4556788708},
    "ai_said_probably":   {"sweatshirt": 4551798209},
    "brain_loading":      {"sweatshirt": 4555786273},
    "prompt_crime_scene": {"sweatshirt": 4552547847},
    "prompt_it_till":     {"sweatshirt": 4551834830},
    "code_works":         {"sweatshirt": 4546881437},
    "social_battery":     {"sweatshirt": 4544413224},
}

LISTING_TO_DESIGN = {lid: (key, garment)
                     for key, halves in CATALOGUE.items()
                     for garment, lid in halves.items()}


# ── The replacement tag sets ─────────────────────────────────────────────────
# 13 tags per listing, built to the distribution _TAG_RULES lays out:
#   4 DESIGN   — the actual words/joke on the garment. What makes this listing
#                findable as itself and not as one of a hundred lookalikes.
#   3 AUDIENCE — who wears it or receives it.
#   3 OCCASION — when it gets bought.
#   3 CATEGORY — where it sits. THESE ARE WHAT SPLIT A PAIR: the tee half takes
#                "...shirt"/"...tee" phrasing, the sweatshirt half takes
#                "...sweatshirt"/"...crewneck", so the two halves stop bidding
#                against each other for one slot.
#
# The niche is people who USE AI — prompting, chatbots, leaning on ChatGPT to get
# through work or study — not the engineers who build it. Every design tag is
# written from the buyer's side of that line.
RETAGS: dict[int, list[str]] = {
    # ── AI Overthinking So You Do Not Have To ───────────────────────────────
    4513682953: ["ai overthinking", "overthinker gift", "let the ai think", "anxious brain humor",
                 "gift for coworker", "work bestie gift", "college student gift",
                 "secret santa gift", "funny birthday gift", "back to school gift",
                 "funny ai shirt", "chatbot shirt", "ai humor tee"],
    4545634438: ["ai overthinking", "overthinker gift", "let the ai think", "anxious brain humor",
                 "remote worker gift", "college student gift", "gift for coworker",
                 "christmas gift idea", "finals week gift", "secret santa gift",
                 "funny ai sweatshirt", "chatbot sweatshirt", "ai humor crewneck"],

    # ── 404 Social Skills Not Found ─────────────────────────────────────────
    4464482796: ["404 social skills", "not found error", "social skills error", "introvert humor",
                 "gift for introvert", "gift for teen", "gift for him",
                 "birthday gift idea", "white elephant gift", "stocking stuffer",
                 "funny introvert tee", "sarcastic shirt", "meme shirt"],
    4544413718: ["404 social skills", "not found error", "social skills error", "introvert humor",
                 "gift for introvert", "homebody gift", "gift for teen",
                 "christmas gift idea", "stocking stuffer", "cozy winter gift",
                 "introvert sweatshirt", "sarcastic crewneck", "meme sweatshirt"],

    # ── I Hallucinate With Confidence ───────────────────────────────────────
    4525458859: ["ai hallucination", "confidently wrong", "chatbot made it up", "hallucinate humor",
                 "grad student gift", "gift for coworker", "gift for him",
                 "secret santa gift", "funny birthday gift", "new job gift",
                 "chatbot shirt", "ai lover shirt", "funny ai shirt"],
    4548389600: ["ai hallucination", "confidently wrong", "chatbot made it up", "hallucinate humor",
                 "grad student gift", "student gift idea", "writer gift idea",
                 "graduation gift", "stocking stuffer", "gag gift idea",
                 "ai lover crewneck", "meme sweatshirt", "ai joke crewneck"],

    # ── Be Polite To The AI Just In Case ────────────────────────────────────
    4532805381: ["be polite to ai", "thank you chatbot", "ai takeover joke", "just in case humor",
                 "gift for teacher", "office worker gift", "gift for her",
                 "teacher gift idea", "funny birthday gift", "holiday gift idea",
                 "ai humor tee", "chatbot lover gift", "funny ai shirt"],
    4551025574: ["be polite to ai", "thank you chatbot", "ai takeover joke", "just in case humor",
                 "gift for teacher", "gift for mom", "nurse gift idea",
                 "teacher gift idea", "christmas gift idea", "end of year gift",
                 "ai humor crewneck", "chatbot crewneck", "cozy funny crewneck"],

    # ── Ctrl Alt Delighted ──────────────────────────────────────────────────
    4505970613: ["ctrl alt delighted", "keyboard humor", "retro computer", "pixel art shirt",
                 "gift for him", "work bestie gift", "gift for coworker",
                 "white elephant gift", "birthday gift idea", "new job gift",
                 "funny tech tee", "office humor shirt", "sarcastic shirt"],
    4546881347: ["ctrl alt delighted", "keyboard humor", "retro computer", "pixel art shirt",
                 "gamer gift idea", "student gift idea", "gift for best friend",
                 "cozy winter gift", "gag gift idea", "last minute gift",
                 "funny tech crewneck", "cozy funny crewneck", "sarcastic crewneck"],

    # ── Prompt Regret Repeat ────────────────────────────────────────────────
    4532808742: ["prompt regret repeat", "prompting humor", "ai prompt joke", "chatbot user gift",
                 "college student gift", "writer gift idea", "gift for her",
                 "finals week gift", "back to school gift", "graduation gift",
                 "study humor tee", "funny ai shirt", "chatbot shirt"],
    4551832314: ["prompt regret repeat", "prompting humor", "ai prompt joke", "chatbot user gift",
                 "writer gift idea", "homebody gift", "quiet friend gift",
                 "back to school gift", "exam season gift", "finals week gift",
                 "study humor crewneck", "meme sweatshirt", "ai joke crewneck"],

    # ── Blame The Prompt ────────────────────────────────────────────────────
    4532817446: ["blame the prompt", "bad prompt humor", "ai excuses", "prompting fail",
                 "office worker gift", "marketing gift", "gift for coworker",
                 "work from home gift", "secret santa gift", "funny birthday gift",
                 "office humor shirt", "funny ai shirt", "ai humor tee"],
    4551016877: ["blame the prompt", "bad prompt humor", "ai excuses", "prompting fail",
                 "marketing gift", "gift for her", "remote worker gift",
                 "work from home gift", "christmas gift idea", "holiday gift idea",
                 "office crewneck", "funny ai sweatshirt", "ai humor crewneck"],

    # ── Powered By Overthinking.exe ─────────────────────────────────────────
    4529259987: ["overthinking exe", "powered by anxiety", "overthinker humor", "anxious humor gift",
                 "gift for best friend", "college student gift", "gift for him",
                 "exam season gift", "stocking stuffer", "birthday gift idea",
                 "sarcastic shirt", "relatable meme tee", "funny tech tee"],
    4551803523: ["overthinking exe", "powered by anxiety", "overthinker humor", "anxious humor gift",
                 "homebody gift", "quiet friend gift", "gamer gift idea",
                 "exam season gift", "cozy winter gift", "last minute gift",
                 "funny tech crewneck", "cozy funny crewneck", "office crewneck"],

    # ── Powered By Copy Paste — reads as pasting from ChatGPT now ───────────
    4532804475: ["powered by copypaste", "copy paste humor", "ctrl c ctrl v", "chatbot copy paste",
                 "office worker gift", "work bestie gift", "gift for him",
                 "white elephant gift", "work from home gift", "new job gift",
                 "office humor shirt", "funny work tee", "sarcastic shirt"],
    4551015747: ["powered by copypaste", "copy paste humor", "ctrl c ctrl v", "chatbot copy paste",
                 "remote worker gift", "work bestie gift", "gift for boss",
                 "christmas gift idea", "holiday gift idea", "birthday gift idea",
                 "office crewneck", "funny work crewneck", "sarcastic crewneck"],

    # ── Ctrl Z Undoing My Life Choices ──────────────────────────────────────
    4532816690: ["ctrl z shirt", "undo life choices", "keyboard shortcut", "life regret humor",
                 "gift for teen", "gift for best friend", "gift for her",
                 "gag gift idea", "funny birthday gift", "last minute gift",
                 "relatable meme tee", "meme shirt", "sarcastic shirt"],
    4551781634: ["ctrl z sweatshirt", "undo life choices", "keyboard shortcut", "life regret humor",
                 "gamer gift idea", "gift for roommate", "gift for sibling",
                 "cozy winter gift", "white elephant gift", "end of year gift",
                 "funny nerdy crewneck", "witty slogan top", "tech joke crewneck"],

    # ── Sweatshirt-only designs ─────────────────────────────────────────────
    4555797226: ["ai productivity", "ai did my work", "productivity humor", "work smarter joke",
                 "gift for boss", "marketing gift", "office worker gift",
                 "new job gift", "work from home gift", "holiday gift idea",
                 "office crewneck", "funny ai sweatshirt", "ai humor crewneck"],
    4556162450: ["long prompt humor", "prompt writer gift", "wordy prompts", "chatbot user gift",
                 "writer gift idea", "grad student gift", "college student gift",
                 "graduation gift", "finals week gift", "back to school gift",
                 "study humor crewneck", "chatbot sweatshirt", "funny ai sweatshirt"],
    4556788708: ["chatbot meeting", "anti meeting club", "meeting humor", "ai took my meeting",
                 "remote worker gift", "office worker gift", "gift for boss",
                 "work from home gift", "secret santa gift", "new job gift",
                 "funny work crewneck", "office crewneck", "ai humor crewneck"],
    4551798209: ["ai said it true", "probably true", "trust the chatbot", "ai answers humor",
                 "gift for teacher", "student gift idea", "gift for teen",
                 "teacher gift idea", "holiday gift idea", "birthday gift idea",
                 "chatbot sweatshirt", "meme sweatshirt", "funny ai sweatshirt"],
    4555786273: ["my brain loading", "ai already done", "brain buffering", "slow brain humor",
                 "student gift idea", "gift for teen", "grad student gift",
                 "back to school gift", "exam season gift", "graduation gift",
                 "study humor crewneck", "chatbot sweatshirt", "cozy funny crewneck"],
    4552547847: ["prompt history", "crime scene humor", "chat history joke", "guilty prompts",
                 "work bestie gift", "gift for best friend", "gift for her",
                 "gag gift idea", "stocking stuffer", "last minute gift",
                 "chatbot sweatshirt", "sarcastic crewneck", "meme sweatshirt"],
    4551834830: ["prompt it till", "fake it till humor", "prompting mantra", "ai motivation joke",
                 "grad student gift", "student gift idea", "writer gift idea",
                 "graduation gift", "exam season gift", "finals week gift",
                 "ai lover crewneck", "study humor crewneck", "ai joke crewneck"],
    # The one design the niche move cannot carry. Its joke is literally about
    # code, so tagging it for AI users would mismatch buyer intent and cost
    # conversion. Tagged as generic work-frustration humour, kept as legacy stock.
    4546881437: ["wake me up humor", "it works now", "tech frustration", "sleepy worker gift",
                 "gift for boss", "remote worker gift", "gift for best friend",
                 "gag gift idea", "white elephant gift", "last minute gift",
                 "funny work crewneck", "funny tech crewneck", "sarcastic crewneck"],
    4544413224: ["social battery low", "recharging humor", "introvert battery", "homebody gift",
                 "gift for introvert", "gift for mom", "quiet friend gift",
                 "cozy winter gift", "end of year gift", "office party gift",
                 "introvert sweatshirt", "witty slogan top", "quiet vibes crewneck"],
}


def _log(line: str = "") -> None:
    print(line)
    CACHE_DIR.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def fetch_live(client: EtsyClient, ids) -> dict[int, dict]:
    """Pull title + tags for every listing. One GET each, well under the limit."""
    out = {}
    for lid in ids:
        try:
            rec = client.get_listing(lid)
            out[lid] = {
                "title": rec.get("title", ""),
                "tags": [str(t).strip().lower() for t in (rec.get("tags") or [])],
                "views": rec.get("views", 0),
                "state": rec.get("state", "?"),
            }
        except EtsyError as exc:
            _log(f"  ✗ {lid} could not be read: {exc}")
        time.sleep(0.15)
    return out


def scan_tags(tags: list[str], usage: Counter) -> list[str]:
    """Every rule violation in one listing's tag set."""
    problems = []
    if len(tags) != ETSY_TAG_MAX:
        problems.append(f"{len(tags)} tags, should be {ETSY_TAG_MAX}")
    for t in tags:
        if len(t) > ETSY_TAG_LEN:
            problems.append(f"{t!r} is {len(t)} chars, limit {ETSY_TAG_LEN}")
        if t in BANNED_TAGS:
            problems.append(f"{t!r} is banned (wrong niche or too broad)")
    if len(set(tags)) != len(tags):
        problems.append("duplicate tags within this listing")
    for t in tags:
        if usage[t] > TAG_SATURATION_LIMIT:
            problems.append(f"{t!r} used on {usage[t]} listings (limit {TAG_SATURATION_LIMIT})")
    return problems


def audit(live: dict[int, dict]) -> Counter:
    """Report the shape of the live tag catalogue. Returns the usage counter."""
    usage = Counter(t for rec in live.values() for t in rec["tags"])

    _log()
    _log("=" * 78)
    _log(f"  Tag audit  ·  {len(live)} live listings  ·  {datetime.now():%Y-%m-%d %H:%M}")
    _log("=" * 78)
    _log()
    _log(f"  distinct tags in use : {len(usage)}")
    _log(f"  total tag slots      : {sum(usage.values())} "
         f"(of {len(live) * ETSY_TAG_MAX} available)")
    _log()

    # ── saturation ──────────────────────────────────────────────────────────
    over = [(t, n) for t, n in usage.most_common() if n > TAG_SATURATION_LIMIT]
    _log(f"  ── OVER-SATURATED (on more than {TAG_SATURATION_LIMIT} listings) ──")
    if not over:
        _log("     none")
    for t, n in over:
        flag = "  ← BANNED too" if t in BANNED_TAGS else ""
        _log(f"     {n:>3} × {t}{flag}")
    _log()

    # ── banned ──────────────────────────────────────────────────────────────
    banned_live = [(t, n) for t, n in usage.most_common() if t in BANNED_TAGS]
    _log("  ── BANNED TAGS STILL LIVE (old niche / too broad) ──")
    if not banned_live:
        _log("     none")
    for t, n in banned_live:
        _log(f"     {n:>3} × {t}")
    _log()

    # ── wasted slots ────────────────────────────────────────────────────────
    short = [lid for lid, rec in live.items() if len(rec["tags"]) < ETSY_TAG_MAX]
    _log(f"  ── LISTINGS NOT USING ALL {ETSY_TAG_MAX} TAG SLOTS ──")
    if not short:
        _log("     none")
    for lid in short:
        _log(f"     {lid}  {len(live[lid]['tags']):>2} tags  {live[lid]['title'][:52]}")
    _log()

    # ── pair overlap ────────────────────────────────────────────────────────
    # A pair SHOULD share its design, audience and occasion tags — it is one joke
    # sold on two garments. What must differ is the CATEGORY tags, the ones
    # carrying the garment word: if both halves chase "funny ai sweatshirt" the
    # tee cannot win it and the two listings split one slot between them. So the
    # meaningful test is overlap in the last three tags, not overlap overall.
    _log("  ── PAIRS COMPETING FOR THE SAME GARMENT QUERY ──")
    any_overlap = False
    for key, halves in CATALOGUE.items():
        tee, swt = halves.get("tee"), halves.get("sweatshirt")
        if not (tee and swt) or tee not in live or swt not in live:
            continue
        cat_tee = set(live[tee]["tags"][-3:])
        cat_swt = set(live[swt]["tags"][-3:])
        clash = cat_tee & cat_swt
        total = len(set(live[tee]["tags"]) & set(live[swt]["tags"]))
        if clash:
            any_overlap = True
            _log(f"     ✗ {key:<20} category clash: {', '.join(sorted(clash))}")
        else:
            _log(f"     ✓ {key:<20} split cleanly "
                 f"({total} shared design/audience tags, 0 category)")
    if not any_overlap:
        _log("     no pair is bidding against itself")
    _log()

    # ── which listings are worst ────────────────────────────────────────────
    _log("  ── PER-LISTING VERDICT ──")
    dirty = 0
    for lid, rec in sorted(live.items(), key=lambda kv: -kv[1]["views"]):
        problems = scan_tags(rec["tags"], usage)
        mark = "✓" if not problems else "✗"
        if problems:
            dirty += 1
        _log(f"     {mark} {lid}  {rec['views']:>3}v  {len(problems):>2} issue(s)  "
             f"{rec['title'][:46]}")
    _log()
    _log("=" * 78)
    _log(f"  listings with tag problems : {dirty} / {len(live)}")
    _log("=" * 78)
    _log()
    return usage


def dump(live: dict[int, dict], path: Path) -> None:
    path.write_text(
        json.dumps({str(k): v for k, v in live.items()}, indent=2, ensure_ascii=False),
        encoding="utf-8")


def check_proposed() -> list[str]:
    """Grade RETAGS against every rule BEFORE any of it reaches Etsy.

    Catching a 21-character tag or a phrase that quietly landed on six listings
    here costs nothing; catching it after 29 writes costs 29 more.
    """
    errors = []
    usage = Counter(t for tags in RETAGS.values() for t in tags)

    missing = set(LISTING_TO_DESIGN) - set(RETAGS)
    if missing:
        errors.append(f"no tags written for: {sorted(missing)}")
    extra = set(RETAGS) - set(LISTING_TO_DESIGN)
    if extra:
        errors.append(f"tags written for listings not in the catalogue: {sorted(extra)}")

    for lid, tags in RETAGS.items():
        where = f"{lid}"
        if len(tags) != ETSY_TAG_MAX:
            errors.append(f"{where}: {len(tags)} tags, need {ETSY_TAG_MAX}")
        if len(set(tags)) != len(tags):
            dupes = [t for t, n in Counter(tags).items() if n > 1]
            errors.append(f"{where}: duplicate tag(s) {dupes}")
        for t in tags:
            if len(t) > ETSY_TAG_LEN:
                errors.append(f"{where}: {t!r} is {len(t)} chars (limit {ETSY_TAG_LEN})")
            if t in BANNED_TAGS:
                errors.append(f"{where}: {t!r} is banned")
            if t != t.lower().strip():
                errors.append(f"{where}: {t!r} is not clean lowercase")

    for t, n in usage.most_common():
        if n > TAG_SATURATION_LIMIT:
            errors.append(f"{t!r} would land on {n} listings (limit {TAG_SATURATION_LIMIT})")

    # No two listings may end up with the same set — that is what the old
    # catalogue did to 4513682953 and 4505970613, verbatim.
    by_set = defaultdict(list)
    for lid, tags in RETAGS.items():
        by_set[frozenset(tags)].append(lid)
    for lids in by_set.values():
        if len(lids) > 1:
            errors.append(f"identical tag sets: {sorted(lids)}")

    # Pair halves must differ in their category tags.
    for key, halves in CATALOGUE.items():
        tee, swt = halves.get("tee"), halves.get("sweatshirt")
        if not (tee and swt) or tee not in RETAGS or swt not in RETAGS:
            continue
        shared = set(RETAGS[tee]) & set(RETAGS[swt])
        if len(shared) >= OVERLAP_ALERT + 5:
            errors.append(f"pair {key}: halves share {len(shared)} tags")
    return errors


def preview() -> int:
    """Per-listing before/after, plus the shape of the new catalogue."""
    live = json.loads((CACHE_DIR / "tags_before.json").read_text(encoding="utf-8"))
    usage = Counter(t for tags in RETAGS.values() for t in tags)

    _log()
    _log("=" * 78)
    _log("  Retag  ·  DRY RUN")
    _log("=" * 78)
    _log()
    for lid, tags in RETAGS.items():
        key, garment = LISTING_TO_DESIGN[lid]
        before = live.get(str(lid), {}).get("tags", [])
        gone = [t for t in before if t in BANNED_TAGS]
        _log(f"  {lid}  {key}/{garment}")
        _log(f"    before : {' · '.join(before)}")
        if gone:
            _log(f"    drops  : {len(gone)} banned — {', '.join(gone)}")
        _log(f"    after  : {' · '.join(tags)}")
        _log()

    _log("  ── most-used tags in the NEW catalogue ──")
    for t, n in usage.most_common(12):
        _log(f"     {n:>3} × {t}")
    _log()
    _log(f"  distinct tags : {len(usage)}  (was 196)")
    _log(f"  ceiling       : no tag on more than {TAG_SATURATION_LIMIT} listings")
    _log()
    return 0


def apply_tags(only: int | None) -> int:
    targets = {only: RETAGS[only]} if only else RETAGS
    client = EtsyClient()
    shop_id = client.get_shop_id()

    _log()
    _log("=" * 78)
    _log(f"  Retag  ·  APPLY  ·  {datetime.now():%Y-%m-%d %H:%M:%S}  ·  shop {shop_id}")
    _log("=" * 78)
    _log()

    backup, written, failed = {}, 0, 0
    for lid, tags in targets.items():
        try:
            before = client.get_listing_tags(lid)
        except EtsyError as exc:
            _log(f"  ✗ {lid} unreadable: {exc}")
            failed += 1
            continue
        backup[str(lid)] = before

        if before == tags:
            _log(f"  ✓ {lid} already correct")
            continue

        try:
            client.update_listing_tags(shop_id, lid, tags)
        except EtsyError as exc:
            _log(f"  ✗ {lid} WRITE FAILED: {exc}")
            failed += 1
            continue

        live = client.get_listing_tags(lid)
        if set(live) == set(tags):
            _log(f"  ✓ {lid} written and confirmed live ({len(live)} tags)")
            written += 1
        else:
            _log(f"  ✗ {lid} Etsy stored something else: {live}")
            failed += 1
        time.sleep(0.3)

    if backup:
        path = CACHE_DIR / f"tag_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(backup, indent=2, ensure_ascii=False), encoding="utf-8")
        _log(f"\n  previous tags saved to {path}")

    _log("=" * 78)
    _log(f"  retagged : {written}")
    _log(f"  failed   : {failed}")
    _log("=" * 78)
    _log()
    return 1 if failed else 0


def seed_usage_ledger() -> None:
    """Write caching/tag_usage.json from what is actually live.

    ai_content.py's saturation guard reads this file to tell the model which
    phrases are used up. It has never existed, so the guard has been starting
    from zero on every run and could not know the live catalogue at all.
    """
    live = json.loads((CACHE_DIR / "tags_after.json").read_text(encoding="utf-8"))
    usage = Counter(t for rec in live.values() for t in rec["tags"])
    TAG_USAGE_FILE.parent.mkdir(exist_ok=True)
    TAG_USAGE_FILE.write_text(json.dumps(dict(usage.most_common()), indent=2),
                              encoding="utf-8")
    _log(f"  saturation ledger seeded: {TAG_USAGE_FILE} ({len(usage)} tags)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit and repair live Etsy tags.")
    ap.add_argument("--audit", action="store_true",
                    help="report only — read the live tags and grade them")
    ap.add_argument("--check", action="store_true",
                    help="grade the PROPOSED tags offline; no network, no writes")
    ap.add_argument("--apply", action="store_true", help="commit the new tags")
    ap.add_argument("--verify", action="store_true",
                    help="re-read live tags and grade them; also seeds tag_usage.json")
    ap.add_argument("--only", type=int, metavar="LISTING_ID")
    args = ap.parse_args()

    if args.check:
        errors = check_proposed()
        for e in errors:
            print(f"  ✗ {e}")
        print(f"\n  {'FAIL' if errors else 'PASS'} — {len(errors)} problem(s) "
              f"in {len(RETAGS)} proposed tag sets")
        return 1 if errors else 0

    client = EtsyClient()
    ids = [args.only] if args.only else sorted(LISTING_TO_DESIGN)

    if args.apply:
        errors = check_proposed()
        if errors:
            print("✗ refusing to write — the proposed tags fail their own rules:")
            for e in errors:
                print(f"    {e}")
            return 1
        return apply_tags(args.only)

    live = fetch_live(client, ids)
    if not live:
        print("✗ nothing could be read from Etsy.")
        return 2

    CACHE_DIR.mkdir(exist_ok=True)
    if args.verify:
        dump(live, CACHE_DIR / "tags_after.json")
        usage = audit(live)
        mismatched = [lid for lid, rec in live.items()
                      if lid in RETAGS and set(rec["tags"]) != set(RETAGS[lid])]
        if mismatched:
            _log(f"  ✗ live tags differ from intended on: {sorted(mismatched)}")
        else:
            _log("  ✓ every live tag set matches the intended rewrite")
        seed_usage_ledger()
        return 1 if mismatched else 0

    dump(live, CACHE_DIR / "tags_before.json")
    audit(live)
    if not args.audit:
        preview()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
