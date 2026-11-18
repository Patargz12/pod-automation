"""
redescribe.py — Rewrite the DESCRIPTION on live Etsy listings, in batches
=========================================================================
WHY THIS EXISTS

audit.py checks titles, tags, price and placement. Until 13 Sept it never
looked at descriptions, so the one field it could not see was the one field
that was never fixed: most listings carried pre-25-Aug copy written by the old
engineer-facing prompt, addressing developers on designs that are not developer
jokes. audit.py now runs qa_descriptions() below on every listing, so the
"is the shop clean" number includes descriptions; this script is the tool that
FIXES what that reports.

WHO THE DESIGN IS FOR
  Whether a design is a developer joke decides the copy's audience. The title
  test (audit.is_dev_design) cannot see a joke the title format hides, and the
  QA gate then throws out every correct draft. So before generating for a
  listing that is not pinned either way in caching/dev_designs.json, the
  ARTWORK is classified once (ai_content.classify_design) and the verdict
  pinned — one small Gemini call instead of two wasted generations.

WHAT IT DOES NOT TOUCH
  Titles and tags are already correct and audited. This script only ever PATCHes
  the description field, and the generator it calls returns nothing else.

  It also never writes to caching/tag_usage.json. generate_listing_content()
  records tags as a side effect; generate_description() deliberately does not,
  because a description run publishes no tags and the saturation ledger must
  only count tags that are actually on a listing.

THE IMAGE PROBLEM
  The design source art is not in the repo, so there is nothing on disk to write
  from. Each listing's own product photo is fetched from Etsy and used instead —
  the print is legible in the mockup, and it has the side benefit that the copy
  describes what the buyer is actually looking at.

USAGE
  python -m scripts.etsy.redescribe                      # dry-run, oldest 10 not yet done
  python -m scripts.etsy.redescribe --limit 5            # dry-run, oldest 5
  python -m scripts.etsy.redescribe --ids 4432534143,... # dry-run, exactly these
  python -m scripts.etsy.redescribe --limit 10 --apply   # write them
  python -m scripts.etsy.redescribe --status             # how many done / left

SAFETY
  --apply backs every listing's CURRENT description up to
  caching/desc_backup_<timestamp>.json BEFORE the first write, and records each
  finished id in caching/redescribed.json so a rerun never repeats work or
  double-charges Gemini. A batch can be reverted from its own backup file.
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

import requests

from scripts.etsy.audit import (
    DEV_TITLE_RE,
    active_listings,
    design_name_of,
    garment_of,
    is_dev_design,
    load_dev_pins,
    pin_design,
)
from services.ai_content import classify_design, generate_description
from services.etsy_service import EtsyClient, EtsyError

CACHE_DIR = Path("caching")
LEDGER_FILE = CACHE_DIR / "redescribed.json"

# A credible six-block description runs 200-260 words. Anything shorter than this
# coming back from Gemini means the structure collapsed and must not be published.
MIN_DESCRIPTION_CHARS = 400


# ── Description QA ───────────────────────────────────────────────────────────
# A rewrite is not free: it spends a Gemini call and replaces copy that may
# already be fine. So nothing is regenerated until a listing FAILS one of these.
# Every check below is deterministic and reads only what Etsy already returned —
# no model, no cost — and each names the defect rather than just scoring it.

FILLER_PHRASES = ("high quality", "premium", "limited edition", "best seller",
                  "hot sale", "top quality")

# Words that mean the copy is addressing people who BUILD software. On a design
# that is not a developer joke, these are the pre-25-Aug defect: the old
# engineer-facing prompt stamped them onto everyday-AI designs.
DEV_PROSE_RE = DEV_TITLE_RE

# Word boundaries are not optional here: "sweatshirt" literally contains the
# substring "tshirt", so a plain `in` test reports every sweatshirt listing as
# describing a tee.
TEE_RE = re.compile(r"\b(t-?shirts?|jersey tee|bella\+?canvas|3001)\b")
SWEAT_RE = re.compile(r"\b(sweatshirts?|crewneck|gildan|18000|heavy blend)\b")

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
MARKUP_RE = re.compile(r"</?[a-z]+>|\*\*|##|\[.+?\]\(.+?\)")

# Printify appends an EU GPSR compliance block to the description of every
# product created since the regulation took effect: the EU representative, the
# 1999/44/EC warranty line, hazard info and care instructions. It is legally
# required text for EU sales and it is NOT ours to rewrite — a rewrite must
# regenerate the prose above it and re-attach this verbatim.
GPSR_MARKERS = ("EU representative:", "Product information:",
                "Warnings, Hazard:", "Care instructions:")


def split_compliance_tail(text: str) -> tuple[str, str]:
    """Split a description into (our prose, Printify's compliance block)."""
    idx = None
    for marker in GPSR_MARKERS:
        i = text.find(marker)
        if i >= 0:
            idx = i if idx is None else min(idx, i)
    if idx is None:
        return text.strip(), ""
    return text[:idx].strip(), text[idx:].strip()


def check_description(text: str, garment: str, is_dev: bool) -> list[str]:
    """Every defect in one listing's description. Empty list means leave it alone.

    Only OUR prose is judged. The compliance block is Printify's, is required,
    and would otherwise trip half these rules on its own — it names the garment,
    ends without punctuation, and repeats on every listing.
    """
    d, _tail = split_compliance_tail((text or "").strip())
    low = d.lower()
    faults = []

    if not d:
        return ["empty"]
    if len(d) < MIN_DESCRIPTION_CHARS:
        faults.append(f"too short ({len(d)} chars)")

    # Block 1 is the HOOK: the design's own text, or a riff on it. It is quoted
    # artwork, not copy we get to choose — a shirt that literally reads "LIMITED
    # EDITION" or "Debugging my prompts" must be allowed to say so. Vocabulary
    # rules therefore apply to the body, where the audience is actually being
    # addressed. Old pre-rewrite copy fails on the body regardless, since its
    # developer framing runs through every block rather than one line.
    body = "\n".join(d.splitlines()[1:]).lower() or low

    # The defect the whole rewrite exists for: developer copy on a design that
    # is not a developer joke. Correct and expected on one that is.
    if not is_dev:
        hits = sorted({m.group(0) for m in DEV_PROSE_RE.finditer(body)})
        if hits:
            faults.append("developer copy on everyday-AI design: " + ", ".join(hits[:6]))

    # Naming the wrong garment is worse than saying nothing: the buyer reads a
    # t-shirt description under a sweatshirt photo.
    if garment == "tee" and SWEAT_RE.search(low):
        faults.append("describes a sweatshirt on a tee listing")
    if garment == "sweatshirt" and TEE_RE.search(low):
        faults.append("describes a tee on a sweatshirt listing")

    # A filler phrase inside quotation marks is the design's joke being quoted
    # ("anyone who feels 'limited edition'"), not the listing claiming the shirt
    # is a limited edition. Only unquoted use is the marketing filler Etsy
    # shoppers scroll past.
    unquoted = re.sub(r"[\"“”'‘’](.+?)[\"“”'‘’]", " ", body)
    filler = [p for p in FILLER_PHRASES if p in unquoted]
    if filler:
        faults.append("filler: " + ", ".join(filler))

    if MARKUP_RE.search(d):
        faults.append("markdown/HTML leaked into copy")
    if EMOJI_RE.search(d):
        faults.append("emoji")
    if re.search(r"\bFRONT\b", d):
        faults.append("prompt formatting leaked (FRONT)")
    if not d.rstrip().endswith((".", "!", "?", '"', "'")):
        faults.append("ends mid-sentence — possibly truncated")

    return faults


def qa_descriptions(live: list[dict], pins: tuple[set[int], set[int]],
                    on_printify: set[int] | None = None) -> dict[int, list[str]]:
    """Every live description through check_description(), plus the two checks
    that need the WHOLE shop: prose duplicated across listings, and a Printify
    listing missing its EU GPSR block. {listing id: faults}; a clean listing is
    absent. This is what audit.py calls, so both tools report the same number.

    A Printful listing with no block is NOT a fault: the block text is
    Printful's, not ours to copy from a Printify sibling, and the 21 of them are
    parked in WhatToDo.md until the real text exists.
    """
    faults_by_id = {}
    seen_text = Counter(html.unescape(l.get("description") or "").strip() for l in live)
    for l in live:
        lid = l["listing_id"]
        t = html.unescape(l.get("title") or "")
        d = html.unescape(l.get("description") or "").strip()
        f = check_description(d, garment_of(t), is_dev_design(lid, t, pins))
        # Duplicated prose splits one listing's ranking across several and reads
        # as boilerplate to a shopper comparing two of your products.
        if d and seen_text[d] > 1:
            f.append(f"identical copy on {seen_text[d]} listings")
        if (on_printify is not None and lid in on_printify
                and d and not split_compliance_tail(d)[1]):
            f.append("no EU GPSR block — `redescribe.py --fix-compliance --apply`")
        if f:
            faults_by_id[lid] = f
    return faults_by_id


PRINTIFY_MAP_FILE = CACHE_DIR / "printify_listing_map.json"


def printify_listing_map(refresh: bool = False) -> dict:
    """{etsy listing id (str): printify product info} for every mapped product.

    This is how provider is established. A listing that Printify does not claim
    is fulfilled somewhere else — in this shop, Printful — and Printful products
    carry a DIFFERENT EU representative and different blank sourcing, so their
    compliance text cannot be copied from a Printify sibling.
    """
    if not refresh and PRINTIFY_MAP_FILE.exists():
        return _load(PRINTIFY_MAP_FILE, {})
    import os
    from services.printify_service import PrintifyClient
    pc = PrintifyClient(os.environ["PRINTIFY_API_TOKEN"])
    shop = pc.get_shops()[0]["id"]
    out, page = {}, 1
    while True:
        data = pc.get(f"/shops/{shop}/products.json?limit=50&page={page}").get("data", [])
        for p in data:
            ext_id = (p.get("external") or {}).get("id")
            if ext_id:
                out[str(ext_id)] = {"pid": p.get("id"), "bp": p.get("blueprint_id"),
                                    "pp": p.get("print_provider_id"), "title": p.get("title")}
        if len(data) < 50:
            break
        page += 1
    _save(PRINTIFY_MAP_FILE, out)
    return out


def canonical_blocks(live: list[dict], mapped: dict) -> dict[str, str]:
    """The compliance block already in use, per garment, taken from live data.

    Never composed. Etsy is the source of truth: whatever Printify wrote onto
    the majority of this shop's sweatshirts is what a sweatshirt missing one
    should get, character for character. Composing the text by hand would mean
    inventing a legal declaration — the garment's origin country and care rules
    differ per blueprint, and the wrong one is worse than none.
    """
    per_garment: dict[str, Counter] = {}
    for l in live:
        if str(l["listing_id"]) not in mapped:
            continue  # Printful text must never be inferred from Printify's
        g = garment_of(html.unescape(l.get("title") or ""))
        tail = split_compliance_tail(html.unescape(l.get("description") or ""))[1]
        if tail:
            per_garment.setdefault(g, Counter())[tail] += 1
    return {g: c.most_common(1)[0][0] for g, c in per_garment.items()}


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def primary_image_bytes(client: EtsyClient, listing_id: int) -> bytes | None:
    """The listing's rank-1 photo, at the largest size Etsy offers."""
    images = client.get_listing_images(listing_id)
    if not images:
        return None
    first = min(images, key=lambda im: im.get("rank", 99))
    url = (first.get("url_fullxfull")
           or first.get("url_570xN")
           or first.get("url_75x75"))
    if not url:
        return None
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=10,
                    help="how many listings this batch (default 10)")
    ap.add_argument("--ids", default="",
                    help="comma-separated listing ids, overrides --limit")
    ap.add_argument("--apply", action="store_true",
                    help="actually write to Etsy (default is a dry run)")
    ap.add_argument("--status", action="store_true",
                    help="show progress and exit")
    ap.add_argument("--check", action="store_true",
                    help="QA every live description and report defects; writes "
                         "nothing and spends no Gemini calls")
    ap.add_argument("--all", action="store_true",
                    help="rewrite even listings whose description passes QA")
    ap.add_argument("--fix-compliance", action="store_true", dest="fix_compliance",
                    help="append the missing EU GPSR block to listings that have "
                         "none, copying the block their garment already uses")
    ap.add_argument("--redo", action="store_true",
                    help="ignore the ledger and rewrite even if already done")
    ap.add_argument("--out", default=str(CACHE_DIR / "desc_pending.json"),
                    help="where a dry run saves the copy it generated")
    ap.add_argument("--from", dest="from_file", default="",
                    help="publish the reviewed copy in this file verbatim, "
                         "generating nothing (use with --apply)")
    args = ap.parse_args()

    client = EtsyClient()
    shop_id = client.get_shop_id()
    live = active_listings(client, shop_id)
    done = set(_load(LEDGER_FILE, {"listing_ids": []}).get("listing_ids", []))
    pins = load_dev_pins()

    if args.status:
        print(f"\n  {len(live)} active · {len(done)} rewritten · "
              f"{len(live) - len(done)} left\n")
        return 0

    if args.fix_compliance:
        mapped = printify_listing_map()
        blocks = canonical_blocks(live, mapped)
        print(f"\n  {'APPLY' if args.apply else 'DRY RUN'} · compliance blocks\n")
        for g, b in sorted(blocks.items()):
            print(f"  canonical {g} block: {len(b)} chars, "
                  f"{b.splitlines()[1][:60]}…")
        gap = [l for l in live
               if not split_compliance_tail(html.unescape(l.get("description") or ""))[1]]
        fixable = [l for l in gap if str(l["listing_id"]) in mapped]
        foreign = [l for l in gap if str(l["listing_id"]) not in mapped]
        print(f"\n  {len(gap)} listing(s) have no block: "
              f"{len(fixable)} on Printify, {len(foreign)} not on Printify\n")

        if args.apply and fixable:
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            bpath = CACHE_DIR / f"desc_backup_{stamp}.json"
            _save(bpath, {str(l["listing_id"]): html.unescape(l.get("description") or "")
                          for l in fixable})
            print(f"  backup → {bpath}\n")

        for l in sorted(fixable, key=lambda x: x["listing_id"]):
            lid = l["listing_id"]
            title = html.unescape(l.get("title") or "")
            g = garment_of(title)
            block = blocks.get(g)
            print(f"  {lid}  {design_name_of(title)[:44]}  [{g}]")
            if not block:
                print("      ⏭  no existing block for this garment to copy\n")
                continue
            body = html.unescape(l.get("description") or "").rstrip()
            if args.apply:
                client.update_listing_description(shop_id, lid, f"{body}\n\n{block}")
                print("      ✓ block appended\n")
            else:
                print(f"      would append {len(block)} chars "
                      f"({block.splitlines()[1][:56]}…)\n")

        if foreign:
            print("  NOT touched — not Printify products, so their compliance text\n"
                  "  (EU representative, blank sourcing) is a different company's and\n"
                  "  must not be copied from a Printify listing:\n")
            for l in sorted(foreign, key=lambda x: x["listing_id"]):
                t = html.unescape(l.get("title") or "")
                print(f"    {l['listing_id']}  [{garment_of(t)}]  {design_name_of(t)[:44]}")
            print()
        return 0

    # QA pass. Runs before any selection so a rewrite only ever targets a
    # listing that actually fails — a description that is already correct is
    # left alone rather than replaced with a different correct one.
    on_printify = {int(k) for k in printify_listing_map()}
    faults_by_id = qa_descriptions(live, pins, on_printify)

    if args.check:
        rows = sorted(live, key=lambda l: l["listing_id"])
        print(f"\n  QA · {len(live)} live descriptions\n")
        counts = Counter()
        for l in rows:
            lid = l["listing_id"]
            if lid not in faults_by_id:
                continue
            name = design_name_of(html.unescape(l.get("title") or ""))
            print(f"  {lid}  {name[:46]}")
            for f in faults_by_id[lid]:
                print(f"      ✗ {f}")
                counts[f.split(":")[0].split(" (")[0]] += 1
        clean = len(live) - len(faults_by_id)
        print(f"\n  {clean} pass · {len(faults_by_id)} need rewriting\n")
        for defect, n in counts.most_common():
            print(f"    {str(n).rjust(4)}  {defect}")
        print()
        return 0

    # A dry run and an --apply run would otherwise each call Gemini separately,
    # and the model is not deterministic — so the copy reviewed would never be
    # the copy published, which makes reviewing it pointless. A dry run saves
    # what it generated; --from republishes exactly that, generating nothing.
    reviewed = {}
    if args.from_file:
        reviewed = _load(Path(args.from_file), {})
        if not reviewed:
            print(f"\n  ✗ {args.from_file} is empty or unreadable.\n")
            return 1

    # Oldest first: the lowest listing ids carry the oldest copy, which is the
    # worst copy in the catalogue and therefore the biggest gain per rewrite.
    if reviewed:
        wanted = {int(k) for k in reviewed}
        batch = [l for l in live if l["listing_id"] in wanted]
    elif args.ids:
        wanted = {int(x) for x in args.ids.replace(" ", "").split(",") if x}
        batch = [l for l in live if l["listing_id"] in wanted]
        missing = wanted - {l["listing_id"] for l in batch}
        if missing:
            print(f"  ⚠️  not live, skipping: {sorted(missing)}")
    else:
        pending = [l for l in live if args.redo or l["listing_id"] not in done]
        # Only listings that actually failed QA, unless --all is asked for.
        if not args.all:
            pending = [l for l in pending if l["listing_id"] in faults_by_id]
        batch = sorted(pending, key=lambda l: l["listing_id"])[:args.limit]

    if not batch:
        print("\n  Nothing to do — every live listing is already rewritten.\n")
        return 0

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"\n  {mode} · {len(batch)} listing(s) · shop {shop_id}\n")

    # Back up BEFORE the first write, not per listing: a half-written batch must
    # still be fully revertible.
    backup_path = None
    if args.apply:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = CACHE_DIR / f"desc_backup_{stamp}.json"
        _save(backup_path, {
            str(l["listing_id"]): html.unescape(l.get("description") or "")
            for l in batch
        })
        print(f"  backup → {backup_path}\n")

    written, skipped, generated = [], [], {}
    for n, listing in enumerate(batch, 1):
        lid = listing["listing_id"]
        title = html.unescape(listing.get("title") or "")
        name = design_name_of(title)
        garment = garment_of(title)
        is_dev = is_dev_design(lid, title, pins)
        old = html.unescape(listing.get("description") or "")
        _prose, tail = split_compliance_tail(old)

        print(f"  [{n}/{len(batch)}] {lid}  {name[:52]}")
        print(f"          {garment} · {'DEVELOPER' if is_dev else 'everyday-AI'} · "
              f"old description {len(old)} chars")

        if reviewed:
            new = (reviewed.get(str(lid)) or "").strip()
            if len(new) < MIN_DESCRIPTION_CHARS:
                print("          ⏭  no reviewed copy for this id — skipped\n")
                skipped.append((lid, "not in reviewed file"))
                continue
            print(f"          publishing reviewed copy, {len(new)} chars")
            if args.apply:
                try:
                    client.update_listing_description(shop_id, lid, new)
                except EtsyError as e:
                    print(f"          ✗ Etsy rejected the write: {e}\n")
                    skipped.append((lid, "etsy rejected"))
                    continue
                done.add(lid)
                _save(LEDGER_FILE, {"listing_ids": sorted(done)})
                print("          ✓ written\n")
            else:
                print("          (dry run — nothing written)\n")
            written.append(lid)
            continue

        if garment == "unknown":
            print("          ⏭  garment not identifiable from the title — skipped\n")
            skipped.append((lid, "unknown garment"))
            continue

        try:
            photo = primary_image_bytes(client, lid)
        except (EtsyError, requests.RequestException) as e:
            print(f"          ⏭  could not fetch photo ({type(e).__name__}) — skipped\n")
            skipped.append((lid, "no photo"))
            continue
        if not photo:
            print("          ⏭  listing has no images — skipped\n")
            skipped.append((lid, "no photo"))
            continue

        # Not pinned either way: the audience above came from the TITLE, which
        # is a guess. Ask the artwork once and pin the answer, so the draft is
        # written for the right buyer and the QA gate below judges it the same
        # way. Only the pin is trusted afterwards — never the title again.
        if lid not in pins[0] and lid not in pins[1]:
            verdict, why = classify_design(photo)
            if verdict is None:
                print(f"          ? artwork not classified ({why}) — keeping the "
                      f"title's verdict")
            else:
                if verdict != is_dev:
                    print(f"          ! artwork says {'DEVELOPER' if verdict else 'everyday-AI'}, "
                          f"title said {'DEVELOPER' if is_dev else 'everyday-AI'} — "
                          f"artwork wins")
                is_dev = verdict
                pin_design(lid, is_dev)
                pins = load_dev_pins()
                print(f"          pinned {'DEVELOPER' if is_dev else 'everyday-AI'}: {why[:90]}")

        # Generated copy goes through the SAME QA as existing copy before it is
        # allowed anywhere near the shop. Lifting the developer ban means the
        # model can now aim at the wrong audience in either direction — an
        # introvert design came back written for "dev team members" on the first
        # run of this batch — so a failing draft is retried once and dropped if
        # it fails again. Publishing nothing beats publishing the defect we are
        # here to remove.
        new, ai_used, faults = "", False, ["not generated"]
        for attempt in (1, 2):
            new, ai_used = generate_description(
                image_bytes=photo,
                garment=garment,
                listing_title=title,
                is_dev_design=is_dev,
                fallback_description="",
            )
            if not ai_used or len(new) < MIN_DESCRIPTION_CHARS:
                faults = [f"generation failed ({len(new)} chars)"]
            else:
                faults = check_description(new, garment, is_dev)
            if not faults:
                break
            if attempt == 1:
                print(f"          ↻ draft failed QA ({'; '.join(faults)}) — regenerating")

        if faults:
            print(f"          ⏭  still failing QA ({'; '.join(faults)}) — "
                  f"skipped, nothing written")
            if any(f.startswith("developer copy") for f in faults):
                print(f"          !! the model keeps writing developer copy for a design "
                      f"pinned everyday-AI — look at the artwork; if it IS a developer "
                      f"joke: python -m scripts.etsy.classify_dev --listing {lid} --refresh --apply")
            print()
            skipped.append((lid, "; ".join(faults)))
            continue

        # Re-attach Printify's compliance block verbatim. Dropping it would
        # strip legally required EU product information off the listing.
        if tail:
            new = f"{new}\n\n{tail}"
            print(f"          + compliance block preserved ({len(tail)} chars)")

        generated[str(lid)] = new
        print(f"          new description {len(new)} chars")
        print("          ┌─────────────────────────────────────────────")
        for line in new.splitlines():
            print(f"          │ {line}")
        print("          └─────────────────────────────────────────────")

        if args.apply:
            try:
                client.update_listing_description(shop_id, lid, new)
            except EtsyError as e:
                print(f"          ✗ Etsy rejected the write: {e}\n")
                skipped.append((lid, "etsy rejected"))
                continue
            done.add(lid)
            _save(LEDGER_FILE, {"listing_ids": sorted(done)})
            print("          ✓ written\n")
        else:
            print("          (dry run — nothing written)\n")
        written.append(lid)

    if generated and not args.apply:
        _save(Path(args.out), generated)
        print(f"  reviewed copy saved → {args.out}")
        print(f"  publish exactly this with:\n"
              f"    python -m scripts.etsy.redescribe --from {args.out} --apply\n")

    verb = "rewritten" if args.apply else "would rewrite"
    print(f"\n  {verb}: {len(written)}")
    if written:
        print("  ids: " + ", ".join(str(i) for i in written))
    if skipped:
        print(f"  skipped: {len(skipped)}")
        for lid, why in skipped:
            print(f"    {lid}  {why}")
    if backup_path:
        print(f"  revert this batch from: {backup_path}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
