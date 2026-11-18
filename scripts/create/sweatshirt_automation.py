"""
sweatshirt_automation.py — One-shot pipeline: local → Printify → Etsy → Pinterest
=================================================================================
Drop a design in "assets/designs/", run ONE command, and a few minutes later that
design is a live Etsy listing wearing our own mockups and a Pinterest pin that
links to it.

SCOPE: only the images in assets/designs/ are processed (upscaled 4x into
assets/designs_final/ first). The rest of assets/designs_final/ (every design
made before) is never touched — same rule as tshirt_automation.py.

    python -m scripts.create.sweatshirt_automation

WHAT IT DOES (per design, failures isolated so one bad design never sinks the batch):

  1. RENDER    — sweatshirt mockups for every color into output/sweatshirt/<design>/,
                 plus the flat hero copy in output/final_thumbnail/.
  2. PRINTIFY  — Gemini writes the SEO title/description/tags, the product is
                 created AND published (no manual positioning check — the default
                 placement is already correct for this product).
  3. WAIT      — Printify→Etsy sync is asynchronous. Poll the product until
                 external.id appears; that value IS the Etsy listing id.
  4. MOCKUPS   — swap Printify's default images for ours (marketing_sweatshirt),
                 rank 1 = Black_Sweatshirt.png, and flip renewal to Automatic.
     SHIPPING  — Printify's sync rewrites Etsy shipping as USD-as-PHP (US
                 "P8.79"); put the peso rates back before going live, and
                 check again at the very end (fix_shipping.repair).
  5. TAGS      — verify Etsy's tags match the Printify product's, repair if not.
                 (See "TAG ALIGNMENT" below.)
  6. ACTIVATE  — take the listing LIVE.  💸 This charges Etsy's $0.20 listing fee.
  7. WAIT      — poll until Etsy reports 'active', so the URL actually resolves.
                 Pinning a draft listing produces a dead link.
  8. EXTRAS    — append the info images (Sweatshirt_Guide size chart,
                 Sweatshirt_Printed, Sweatshirt_Chart color chart) after our
                 mockups, then build a slideshow video from those mockups and
                 upload it. Same ledgers as add_guide.py / video_pipeline.py.
  9. PINTEREST — a Pinterest-SEO Gemini pass writes the pin copy and classifies
                 the design onto the AI-humor or programming-humor board, then
                 creates the pin linking to the live Etsy listing.
 10. LEDGER    — caching/pinned_sweatshirt.json, so re-runs skip finished designs.
                 "Finished" is env-aware — see PINTEREST ACCESS TIER below.

TAG ALIGNMENT (Printify ↔ Etsy):
  publish_product() already sends "tags": True, but Printify's Etsy tag sync is
  gated by shop-level publishing settings — which is why you used to toggle
  "Tags" by hand before publishing. An unattended run cannot depend on a manual
  toggle, so stage 5 does not trust the sync: it reads the tags back off the
  Printify product, compares them to what actually landed on Etsy, and PATCHes
  Etsy directly when they differ. Tags are never regenerated — a second Gemini
  call would produce a different set and make the platforms drift apart.

THE PLACEHOLDER GUARD:
  generate_listing_content() never raises; when every Gemini key is exhausted it
  quietly returns placeholder copy. That is harmless when a human reviews drafts,
  but this pipeline publishes automatically — so a quota-exhausted run would put
  live listings with lorem-ipsum titles in front of customers. Any design whose
  ledger entry has ai_used=False is therefore LEFT IN DRAFTS and reported at the
  end. Override with --allow-placeholder if you really mean it.

PINTEREST ACCESS TIER — and why you never need --only for it:
  On TRIAL access pins must go to the sandbox host, where they are visible only
  to you (PINTEREST_ENV=sandbox, the default). The API returns a real pin id and
  nothing errors — the pin simply isn't public until Standard access is granted.

  A sandbox pin is therefore NOT a finished design. The ledger records which
  environment each pin was made in, and a design only counts as done when it
  carries a pin from the environment you are pinning to right now. So:

      PINTEREST_ENV=sandbox   python -m scripts.create.sweatshirt_automation   # record the demo
      (Pinterest grants Standard access)
      PINTEREST_ENV=production python -m scripts.create.sweatshirt_automation   # same command

  The second run finds every sandbox-pinned design pending again and re-pins it
  for real. Nothing is re-charged on Etsy — those listings are already active,
  and only a draft→active transition costs $0.20.

ONE-TIME SETUP:
  1. .env: PRINTIFY_API_TOKEN, ETSY_KEYSTRING, ETSY_SHARED_SECRET,
           GEMINI_API_KEY_1..5, PINTEREST_APP_ID, PINTEREST_APP_SECRET
  2. python -m scripts.auth.etsy_auth        → caching/.etsy_tokens.json
  3. python -m scripts.auth.pinterest_auth   → caching/.pinterest_tokens.json

USAGE:
  python -m scripts.create.sweatshirt_automation                 # the whole thing
  python -m scripts.create.sweatshirt_automation --dry-run       # trace everything, change nothing, spend nothing
  python -m scripts.create.sweatshirt_automation --only "name.png"  # touch ONLY this design
  python -m scripts.create.sweatshirt_automation --skip-pinterest  # stop after the listing is live
  python -m scripts.create.sweatshirt_automation --skip-video      # no slideshow video (stage 8)
  python -m scripts.create.sweatshirt_automation --skip-info-images  # no size/print/color charts (stage 8)
  python -m scripts.create.sweatshirt_automation --extras-only     # ONLY stage 8, on every live listing
                                                  # in the ledger that is missing them
                                                  # (add --only "name.png" / --dry-run)
  python -m scripts.create.sweatshirt_automation --connect-pinterest
                                                  # redo the Pinterest login/consent in the
                                                  # browser first, then run (review video)
  python -m scripts.create.sweatshirt_automation --no-upscale    # the file in assets/designs/ is already high-res
  python -m scripts.create.sweatshirt_automation --all           # also finish every unfinished design in the ledger
  python -m scripts.create.sweatshirt_automation --forget "name.png" [more...]
                                                  # re-run stages 4-8 for design(s)
  python -m scripts.create.sweatshirt_automation --clear-ledger  # wipe the pinned ledger
  python -m scripts.create.sweatshirt_automation --allow-placeholder  # publish even without AI copy
  python -m scripts.create.sweatshirt_automation --help

RECOVERY — fixing a design AFTER it is live:
  Everything stays editable and editing never re-charges the $0.20. But if you
  fix print positioning in Printify, republish with the "Mockups" checkbox OFF
  (or call PrintifyClient.republish_product) — a republish that includes images
  reasserts Printify's default mockups over ours on a live listing.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

# Ensure UTF-8 output (emoji) on Windows cp1252 consoles / piped stdout.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from scripts.create import sweatshirt
from scripts.create import marketing_sweatshirt
from scripts.create.execute import gather_files, DESIGNS_DIR, FINAL_DESIGNS_DIR
from scripts.etsy.listing_extras import add_extras, backfill
from services.ai_content import (
    BOARD_AI,
    BOARD_DEV,
    generate_pin_content,
)
from services.etsy_service import EtsyClient, EtsyError, EtsyAuthError
from services.pinterest_service import (
    PinterestClient,
    PinterestError,
    PinterestAuthError,
    is_sandbox,
)
from services.printify_service import (
    CACHE_DIR,
    PrintifyClient,
    PrintifyError,
    SWEATSHIRT_PROFILE,
    _load_json,
    _save_json,
)

PINNED_LEDGER = CACHE_DIR / "pinned_sweatshirt.json"
THUMBNAIL_DIR = Path("output/final_thumbnail")

# How long to wait for Printify→Etsy sync (stage 3) and Etsy activation (stage 7).
# Sync is usually seconds but has been known to take minutes under load; on
# timeout the design is left for the next run rather than failed outright.
SYNC_TIMEOUT_S  = int(os.environ.get("POD_SYNC_TIMEOUT", "600") or 600)
SYNC_POLL_S     = 15
ACTIVATE_POLL_S = 5
ACTIVATE_TRIES  = 12

# The two existing Pinterest boards. Gemini picks one per design: developer
# jokes go to Programming Vibes, everything else to AI Humor. To pin into a
# different board, set PINTEREST_BOARD_AI_HUMOR / PINTEREST_BOARD_PROGRAMMING in
# .env to that board's NAME. A board that doesn't exist yet is created.
DEFAULT_BOARD_NAMES = {
    BOARD_AI:  "AI Humor",
    BOARD_DEV: "Programming Vibes",
}
# Only used if the script has to CREATE a board.
BOARD_DESCRIPTIONS = {
    BOARD_AI:
        "Funny AI apparel for everyday life — students, office "
        "workers and anyone who leans on a chatbot a little too much.",
    BOARD_DEV:
        "Funny programming and developer apparel — bugs, git, localhost and "
        "coding alongside AI. Gift ideas for software engineers.",
}


# ══════════════════════════════════════════════════════════════════════════════
#  Ledger helpers (caching/pinned_sweatshirt.json — mirrors marketed_sweatshirt)
# ══════════════════════════════════════════════════════════════════════════════

def _load_pinned() -> dict:
    return _load_json(PINNED_LEDGER, {"products": []})


def _current_env() -> str:
    return "sandbox" if is_sandbox() else "production"


def _completed_names(ledger: dict, skip_pinterest: bool) -> set[str]:
    """Designs that need no further work THIS run.

    A design is finished when it is live on Etsy AND carries a pin made in the
    environment we are pinning to now. Sandbox pins are deliberately not final:
    they are invisible to everyone but you, so the moment PINTEREST_ENV flips to
    production the design goes pending again and the next run re-pins it for
    real. That is what lets the same bare command be correct both before and
    after Pinterest grants Standard access — no --only, no --forget.

    With --skip-pinterest we are not pinning at all, so any existing record
    counts as done rather than dragging every design back through stages 5-7.
    """
    env = _current_env()
    done = set()
    for p in ledger.get("products", []):
        if skip_pinterest or (p.get("pin_id") and p.get("pinterest_env") == env):
            done.add(p["design"])
    return done


def _record_pinned(ledger: dict, record: dict) -> None:
    """Insert or replace by design name.

    Re-runs are normal here (a sandbox→production re-pin, a repaired failure),
    and a blind append would leave two rows for one design — which would then
    print twice in the summary and make the ledger useless as a record of what
    is currently live.
    """
    products = ledger.setdefault("products", [])
    for i, existing in enumerate(products):
        if existing["design"] == record["design"]:
            products[i] = {**existing, **record}
            return
    products.append(record)


def clear_pinned() -> int:
    n = len(_load_pinned().get("products", []))
    _save_json(PINNED_LEDGER, {"products": []})
    print(f"🧹  Cleared {n} entr{'y' if n == 1 else 'ies'} from {PINNED_LEDGER.name}.")
    return n


def forget_pinned(names: list[str]) -> int:
    ledger = _load_pinned()
    before = len(ledger["products"])
    ledger["products"] = [p for p in ledger["products"] if p["design"] not in set(names)]
    removed = before - len(ledger["products"])
    _save_json(PINNED_LEDGER, ledger)
    print(f"🔄  Forgot {removed} design(s) from {PINNED_LEDGER.name}."
          if removed else
          f"ℹ️  None of {names} found in {PINNED_LEDGER.name} — nothing forgotten.")
    # The mockup-swap ledger gates stage 4, so forget there too or the design
    # would be skipped before it ever reaches Pinterest.
    marketing_sweatshirt.forget_marketed(names)
    return removed


# ══════════════════════════════════════════════════════════════════════════════
#  Preflight — fail before anything billable happens
# ══════════════════════════════════════════════════════════════════════════════

def preflight(skip_pinterest: bool,
              connect_pinterest: bool = False) -> tuple[EtsyClient, PinterestClient | None]:
    """Verify every credential up front. Stage 6 spends money; stage 2 creates
    products. Discovering a missing token halfway through is the worst outcome."""
    print("\n" + "═" * 60)
    print("  🚦 Pre-flight")
    print("═" * 60)

    problems = []
    if not os.environ.get("PRINTIFY_API_TOKEN", "").strip():
        problems.append("PRINTIFY_API_TOKEN is not set in .env")
    if not any(os.environ.get(f"GEMINI_API_KEY_{i}", "").strip() for i in range(1, 6)):
        problems.append("No GEMINI_API_KEY_1..5 set — every listing would be placeholder copy")
    if problems:
        for p in problems:
            print(f"     ❌ {p}")
        print()
        sys.exit(1)
    print("     ✓ Printify token, Gemini key(s)")

    try:
        etsy = EtsyClient()
    except EtsyAuthError as e:
        print(f"     ❌ Etsy: {e}\n")
        sys.exit(1)
    print(f"     ✓ Etsy token (shop {etsy.get_shop_id()})")

    pinterest = None
    if not skip_pinterest:
        if connect_pinterest:
            # Fresh login -> "Give access" -> redirect -> code-for-token exchange,
            # in the browser, before anything else runs. Used for the API review video.
            from scripts.auth.pinterest_auth import authorize
            authorize()
        try:
            pinterest = PinterestClient()
            acct = pinterest.get_user_account()
        except PinterestAuthError as e:
            print(f"     ❌ Pinterest: {e}\n")
            sys.exit(1)
        except PinterestError as e:
            print(f"     ❌ Pinterest: {e}\n")
            sys.exit(1)
        env = "SANDBOX" if is_sandbox() else "PRODUCTION"
        print(f"     ✓ Pinterest token — {acct.get('username', '?')} [{env}]")
        if is_sandbox():
            print("       ℹ️  Sandbox pins are visible only to you (Trial access).")
    else:
        print("     ⏭️  Pinterest skipped (--skip-pinterest)")

    return etsy, pinterest


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 3 — wait for Printify → Etsy
# ══════════════════════════════════════════════════════════════════════════════

def ensure_publishing(printify: PrintifyClient, entries: list[dict]) -> None:
    """Re-send the publish for any pending product that has no Etsy listing yet.

    Repairs products stranded by an earlier run — an interrupted run, or one
    whose publish job failed on Printify's side. Cheap and idempotent: Printify
    ignores a publish for a product that already carries an external id.
    """
    print("\n  🔎 Checking each product is really publishing…")
    for entry in entries:
        name = entry["design"]
        try:
            product = printify.get_product(entry["shop_id"], entry["product_id"])
        except PrintifyError as e:
            print(f"     ⚠️  {name}: could not read product ({e}).")
            continue

        if marketing_sweatshirt._etsy_listing_id(product):
            print(f"     ✓ {name}: already has an Etsy listing id.")
            continue
        if product.get("is_locked"):
            print(f"     ⏳ {name}: Printify is publishing it right now.")
            continue

        print(f"     📢 {name}: (re)publishing…")
        printify.publish_product(entry["shop_id"], entry["product_id"])


def wait_for_etsy_listings(printify: PrintifyClient, entries: list[dict],
                           timeout_s: int = SYNC_TIMEOUT_S) -> dict[str, int]:
    """{design: etsy_listing_id} for every entry Printify has finished syncing.

    Printify accepts a publish call immediately but writes external.id only once
    Etsy has actually created the listing, so this is the gate everything
    downstream waits on. Designs still unsynced when the clock runs out are
    simply absent from the result — they get picked up on the next run.
    """
    pending = {e["design"]: e for e in entries}
    found: dict[str, int] = {}
    start = time.time()
    deadline = start + timeout_s
    checks = 0

    print(f"\n  ⏳ Waiting for Printify → Etsy sync ({len(pending)} design(s), "
          f"up to {timeout_s // 60} min)…")
    print(f"     This is the slow stage — Printify accepts the publish instantly but")
    print(f"     writes the Etsy listing id only once Etsy has created the listing.")

    while pending and time.time() < deadline:
        checks += 1
        for name in list(pending):
            entry = pending[name]
            try:
                product = printify.get_product(entry["shop_id"], entry["product_id"])
            except PrintifyError as e:
                print(f"\r     ⚠️  {name}: Printify read failed ({e}) — will retry."
                      f"{' ' * 20}")
                continue
            listing_id = marketing_sweatshirt._etsy_listing_id(product)
            if listing_id:
                found[name] = listing_id
                del pending[name]
                print(f"\r     ✓ {name} → Etsy listing {listing_id}{' ' * 30}")
        if pending and time.time() < deadline:
            # Tick once a second rather than sleeping the whole interval, so the
            # terminal visibly counts down instead of looking hung for 15s at a time.
            for remaining in range(SYNC_POLL_S, 0, -1):
                if time.time() >= deadline:
                    break
                elapsed = int(time.time() - start)
                left = max(0, int(deadline - time.time()))
                print(f"\r     ⏳ {elapsed // 60}m{elapsed % 60:02d}s elapsed · "
                      f"{len(pending)} pending · check #{checks} done · "
                      f"next in {remaining:2d}s · timeout in {left // 60}m{left % 60:02d}s",
                      end="", flush=True)
                time.sleep(1)
    print(f"\r{' ' * 100}\r", end="")   # wipe the heartbeat line

    for name in pending:
        print(f"     ⏱️  {name}: still no Etsy listing id after "
              f"{int(time.time() - start)}s — leaving for the next run.")
    return found


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 5 — tag alignment
# ══════════════════════════════════════════════════════════════════════════════

def align_tags(etsy: EtsyClient, printify: PrintifyClient, shop_id: int,
               listing_id: int, entry: dict, dry_run: bool = False) -> bool:
    """Make Etsy's tags match the Printify product's. Returns True if repaired.

    Source of truth order: the live Printify product first (it is what Etsy was
    supposed to receive), then the ledger's own copy as a fallback. Never a fresh
    Gemini call — that would generate different tags and cause the very drift
    this exists to prevent.
    """
    want: list[str] = []
    try:
        product = printify.get_product(entry["shop_id"], entry["product_id"])
        want = [str(t).strip().lower() for t in (product.get("tags") or [])]
    except PrintifyError as e:
        print(f"     ⚠️  Could not read Printify tags ({e}) — falling back to ledger.")
    if not want:
        want = [str(t).strip().lower() for t in (entry.get("tags") or [])]
    if not want:
        print("     ⚠️  No tags on the Printify product or in the ledger — skipping alignment.")
        return False

    have = etsy.get_listing_tags(listing_id)
    if set(have) == set(want):
        print(f"     🏷️  Tags already aligned ({len(have)}).")
        return False

    missing = [t for t in want if t not in have]
    detail = "  missing: " + ", ".join(missing[:5]) if missing else ""
    print(f"     🏷️  Tag drift: Etsy has {len(have)}, Printify has {len(want)}.{detail}")
    if dry_run:
        print(f"     🔍 Would PATCH Etsy tags → {', '.join(want)}")
        return False
    etsy.update_listing_tags(shop_id, listing_id, want)
    print(f"     ✓ Repaired Etsy tags ({len(want)}).")
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 6/7 — activate and confirm live
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_auto_renew(etsy: EtsyClient, shop_id: int, listing_id: int,
                       listing: dict, dry_run: bool = False) -> None:
    """Put the listing on "Automatic" renewal unless it is already there.

    Costs nothing today: Etsy bills the $0.20 when a listing actually RENEWS,
    four months out, not when the flag is flipped. A failure here is logged and
    swallowed — the listing is live and pinned either way, and renew.py sweeps
    up anything this misses.
    """
    if listing.get("should_auto_renew") is True:
        return
    if dry_run:
        print(f"     🔍 Would switch renewal Manual → Automatic.")
        return
    try:
        etsy.set_auto_renew(shop_id, listing_id, True)
        print(f"     ♻️  Renewal switched to Automatic.")
    except EtsyError as exc:
        print(f"     ⚠️  Could not set auto-renew on {listing_id}: {exc}")
        print(f"        Run `python -m scripts.etsy.renew --apply` to catch it.")


def activate_and_wait(etsy: EtsyClient, shop_id: int, listing_id: int,
                      dry_run: bool = False) -> tuple[str | None, bool]:
    """(public_url, charged) — take the listing live and confirm Etsy agrees.

    url is None if the listing never reaches 'active'; the caller must not pin
    in that case, because the URL would 404.

    `charged` is True only when this call made a draft→active transition, which
    is the ONLY billable event. A listing that was already live costs nothing to
    revisit, so re-pin runs must not be reported as spending money.

    Renewal is set here rather than in the caller because THIS is the moment a
    listing starts its four-month clock. Printify-created drafts arrive on
    "Manual", and a Manual listing silently expires and takes its accumulated
    views and search history with it. Seven live tees were lost to exactly that
    gap — the flag was only ever set during the sweatshirt mockup swap
    (marketing_sweatshirt.py), so anything that skipped that step stayed Manual.
    """
    listing = etsy.get_listing(listing_id)
    state = listing.get("state", "?")

    if state == "active":
        print(f"     ✅ Listing {listing_id} is already active (no fee).")
        _ensure_auto_renew(etsy, shop_id, listing_id, listing, dry_run)
        return f"https://www.etsy.com/listing/{listing_id}", False
    if state != "draft":
        print(f"     ⚠️  Listing {listing_id} is '{state}' — not activating.")
        return None, False

    if dry_run:
        print(f"     🔍 Would activate listing {listing_id}  (💸 $0.20 Etsy fee)")
        return None, False

    print(f"     📢 Activating listing {listing_id}…  💸 $0.20 Etsy listing fee")
    etsy.activate_listing(shop_id, listing_id)
    _ensure_auto_renew(etsy, shop_id, listing_id, listing, dry_run)

    for attempt in range(1, ACTIVATE_TRIES + 1):
        state = etsy.get_listing(listing_id).get("state", "?")
        if state == "active":
            url = f"https://www.etsy.com/listing/{listing_id}"
            print(f"\r     ✅ Live: {url}{' ' * 20}")
            return url, True
        for remaining in range(ACTIVATE_POLL_S, 0, -1):
            print(f"\r     ⏳ Etsy still reports '{state}' · check {attempt}/"
                  f"{ACTIVATE_TRIES} · next in {remaining}s", end="", flush=True)
            time.sleep(1)
    print(f"\r{' ' * 100}\r", end="")

    # The PATCH went through, so Etsy has almost certainly billed us even though
    # it hasn't flipped state yet. Report the fee rather than under-count it.
    print(f"     ⏱️  Listing {listing_id} still '{state}' after "
          f"{ACTIVATE_TRIES * ACTIVATE_POLL_S}s — skipping Pinterest this run.")
    return None, True


def preview_fees(etsy: EtsyClient, listing_ids: dict[str, int],
                 pause_s: int = 10) -> None:
    """Print what this run will really cost, then leave a moment to abort.

    Called only once the Etsy listing ids exist, because the cost depends
    entirely on each listing's CURRENT state and nothing before stage 3 knows
    it. Counting every pending design as billable — the earlier guess — turned a
    free sandbox→production re-pin run into a scary "$1.20 in Etsy fees" and
    pushed you toward --only for a run that was never going to charge anything.

    Only drafts are billable. The pause is skipped entirely when nothing is.
    """
    drafts, live = [], []
    for name, listing_id in listing_ids.items():
        try:
            state = etsy.get_listing(listing_id).get("state", "?")
        except EtsyError:
            state = "?"          # unknown: assume billable, never under-warn
        (drafts if state != "active" else live).append(name)

    if live:
        print(f"\n  ✅ {len(live)} listing(s) already live — revisited free of charge.")
    if not drafts:
        print("  💸 Nothing to activate: this run will not incur any Etsy fees.")
        return

    print(f"\n  💸 {len(drafts)} listing(s) will be taken LIVE — "
          f"${0.20 * len(drafts):.2f} in Etsy fees:")
    for name in drafts:
        print(f"        • {name}")
    print(f"     Ctrl+C within {pause_s}s to abort.")
    time.sleep(pause_s)


# ══════════════════════════════════════════════════════════════════════════════
#  Shipping guard — runs after every Printify publish
# ══════════════════════════════════════════════════════════════════════════════

def repair_shipping(when: str) -> None:
    """Put the Etsy shipping rates back into pesos if Printify overwrote them.

    Printify owns the shipping profiles and, on sync, writes its USD postage
    straight into this PHP shop (US tee "P4.95" instead of ~P495), so the buyer
    gets near-free shipping and we pay the difference. Called twice per run:
    before listings go live, and once more at the end in case a later sync
    overwrote them again. Only writes when something is actually broken, and
    never stops the pipeline — a failure here is reported, not fatal.
    """
    from scripts.etsy.fix_shipping import repair
    print(f"\n  🚚 Shipping guard ({when}) — checking Etsy rates are in pesos…")
    try:
        r = repair(apply=True, verbose=False)
    except Exception as exc:
        print(f"     ⚠️  Shipping check failed: {type(exc).__name__}: {str(exc)[:160]}")
        print(f"        Run by hand: python -m scripts.etsy.fix_shipping --apply")
        return
    if not r["needed"]:
        print("     ✅ All shipping rates OK.")
    else:
        print(f"     🔧 Printify had overwritten {r['needed']} rate(s) — fixed {r['written']}.")
        if r["failed"]:
            print(f"     ❌ {r['failed']} rate(s) could not be written — "
                  f"run: python -m scripts.etsy.fix_shipping --apply")
        print(f"        Undo: python -m scripts.etsy.fix_shipping --restore {r['backup']}")


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 8 — Pinterest
# ══════════════════════════════════════════════════════════════════════════════

def _board_name(board_key: str) -> str:
    env_key = f"PINTEREST_BOARD_{board_key.upper()}"
    return (os.environ.get(env_key, "").strip()
            or DEFAULT_BOARD_NAMES.get(board_key)
            or DEFAULT_BOARD_NAMES[BOARD_AI])


def _pin_image(design_name: str) -> Path:
    """The single image this design's pin is made from: its final_thumbnail file.

    A pin carries exactly one image, and that image is always
    output/final_thumbnail/<design>_Black_Sweatshirt.png — the flat black hero render.
    The other five colour mockups belong on the Etsy listing, not here.

    Sourced from the local file on purpose. Pulling the listing's rank-1 image
    off Etsy's CDN would usually show the same picture, but it is Etsy's
    re-encode of it and it silently changes if the listing's image order ever
    does. The local file is the render we actually made, so what gets pinned is
    never in question.
    """
    path = THUMBNAIL_DIR / f"{Path(design_name).stem}_Black_Sweatshirt.png"
    if not path.is_file():
        raise PinterestError(
            f"No pin image: {path} is missing. It is written by stage 1 — "
            f"run `python -m scripts.create.sweatshirt` to re-render it.")
    return path


def _design_image_for_copy(design_name: str) -> Path:
    """The image Gemini reads to WRITE the pin copy — not the pin image itself.

    The raw art in assets/designs_final/ is the better input here: no garment in the
    way, so the model reads the joke rather than a sweatshirt. A run only ever
    covers designs present in that folder, so this file is there by definition;
    the thumbnail is a belt-and-braces fallback for a half-cleaned folder.
    """
    design = Path("assets/designs_final") / design_name
    if design.is_file():
        return design
    return THUMBNAIL_DIR / f"{Path(design_name).stem}_Black_Sweatshirt.png"


def create_pin_for(pinterest: PinterestClient, etsy: EtsyClient,
                   design_path: Path, design_name: str, listing_id: int,
                   etsy_url: str, dry_run: bool = False,
                   allow_placeholder: bool = False) -> dict | None:
    title, description, alt_text, board_key, ai_used = generate_pin_content(
        _design_image_for_copy(design_name),
        fallback_title=f"Funny {Path(design_name).stem.replace('_', ' ').title()} Sweatshirt",
        fallback_description="A funny unisex crewneck sweatshirt. Tap to see it on Etsy.",
    )
    board_name = _board_name(board_key)
    print(f"     🤖 Pin copy {'(AI)' if ai_used else '(PLACEHOLDER)'} → board '{board_name}'")
    print(f"        {title}")

    # Same reasoning as the Etsy placeholder guard: a pin is public marketing,
    # and "Funny Spaghetti Sweatshirt" pinned forever is worse than no pin this
    # run. Refusing leaves no ledger entry, so the next run simply retries it.
    if not ai_used and not allow_placeholder:
        print(f"     🛑 Placeholder pin copy — NOT pinning this design.")
        print(f"        Restore the design file or fix the Gemini quota and "
              f"re-run, or force it with --allow-placeholder.")
        return None

    if dry_run:
        print(f"     🔍 Would pin to '{board_name}' linking {etsy_url}")
        return None

    board_id = pinterest.ensure_board(board_name, BOARD_DESCRIPTIONS.get(board_key, ""))
    image_path = _pin_image(design_name)

    pin = pinterest.create_pin(
        board_id=board_id,
        title=title,
        description=description,
        link=etsy_url,
        alt_text=alt_text,
        image_path=image_path,
    )
    print(f"     📌 Pin {pin.get('id')} created from {image_path.name}"
          f"{'  (sandbox — visible only to you)' if is_sandbox() else ''}")
    return {"pin_id": pin.get("id"), "board": board_name,
            "board_key": board_key, "pin_ai_used": ai_used}


# ══════════════════════════════════════════════════════════════════════════════
#  Orchestration
# ══════════════════════════════════════════════════════════════════════════════

def _designs_in_scope(skip_upscale: bool, only: list[str] | None) -> list[str]:
    """designs_final/ file names for the raw designs in assets/designs/.
    The upscaler writes <stem>.png; with --no-upscale a raw file that has no
    final copy yet is copied over as-is. Shared with tshirt_automation.py."""
    names = []
    for raw in gather_files(DESIGNS_DIR):
        final = FINAL_DESIGNS_DIR / f"{raw.stem}.png"
        if skip_upscale and not final.exists():
            FINAL_DESIGNS_DIR.mkdir(parents=True, exist_ok=True)
            final = FINAL_DESIGNS_DIR / raw.name
            shutil.copy2(raw, final)
        names.append(final.name)
    if only:
        wanted = {n.lower() for n in only}
        names = [n for n in names if n.lower() in wanted]
    return names


def run(dry_run: bool = False, skip_upscale: bool = False,
        skip_pinterest: bool = False, allow_placeholder: bool = False,
        only: list[str] | None = None, process_all: bool = False,
        connect_pinterest: bool = False, skip_video: bool = False,
        skip_info_images: bool = False):

    print("\n" + "═" * 60)
    print("  🚀  SWEATSHIRT AUTOMATION — Printify → Etsy → Pinterest")
    print("═" * 60)
    if dry_run:
        print("  🔍 DRY RUN — nothing is created, changed, pinned or charged.")

    # ── Scope: ONLY what is in assets/designs/ right now ─────────────────────
    # assets/designs_final/ keeps every design ever made, so it is never the
    # scope — otherwise a cleared ledger re-creates all of them as duplicates.
    scope = _designs_in_scope(skip_upscale, only)
    if not scope and not process_all:
        print(f"\n  ℹ️  Nothing to do — put the design(s) to process in {DESIGNS_DIR}/.\n")
        return
    if scope:
        print(f"\n  🎯 In scope ({DESIGNS_DIR}/): {', '.join(scope)}")

    etsy, pinterest = preflight(skip_pinterest, connect_pinterest)

    # ── Stages 1 & 2: render mockups + create/publish on Printify ────────────
    if scope:
        sweatshirt.run(skip_upscale=skip_upscale, publish=not dry_run, dry_run=dry_run,
                       only=scope)

    if dry_run:
        print("\n  🔍 Dry run stops here — the remaining stages all need a real "
              "Printify product.\n")
        return

    published = _load_json(SWEATSHIRT_PROFILE.ledger_file, {"products": []}).get("products", [])
    if not published:
        print(f"\n  ℹ️  {SWEATSHIRT_PROFILE.ledger_file.name} is empty — nothing to do.\n")
        return

    # Only the designs currently in assets/designs/ (unless --all).
    in_scope = set(scope)
    pinned_ledger = _load_pinned()
    already = _completed_names(pinned_ledger, skip_pinterest)
    if process_all:
        print(f"\n  🌐 --all: ignoring folder scope, using the whole ledger.")
        todo = [e for e in published if e["design"] not in already]
    else:
        todo = [e for e in published
                if e["design"] in in_scope and e["design"] not in already]

    if only:
        wanted = {n.lower() for n in only}
        todo = [e for e in todo if e["design"].lower() in wanted]
        missing = wanted - {e["design"].lower() for e in todo}
        if missing:
            print(f"  ⚠️  --only named design(s) not pending: {', '.join(sorted(missing))}")

    if not todo:
        print(f"\n  ✅ Nothing to do — every design in scope is already completed "
              f"for {_current_env().upper()} (in {PINNED_LEDGER.name}).\n")
        return

    print(f"\n  📋 {len(todo)} design(s) to process this run:")
    for e in todo:
        print(f"        • {e['design']}")

    printify = PrintifyClient(os.environ["PRINTIFY_API_TOKEN"].strip())

    # ── Stage 3: wait for the Etsy listings to exist ────────────────────────
    ensure_publishing(printify, todo)
    listing_ids = wait_for_etsy_listings(printify, todo)
    if not listing_ids:
        print("\n  ⏱️  No listings synced in time. Re-run in a few minutes.\n")
        return

    # ── Stage 4: swap Printify's mockups for ours ───────────────────────────
    # Delegated wholesale: marketing_sweatshirt already handles Etsy's awkward
    # image-rank rules (never 0 images, max 10, async re-ranking after deletes).
    print("\n" + "═" * 60)
    print("  🖼️   Stage 4 — swapping in our mockups")
    print("═" * 60)
    marketing_sweatshirt.run(dry_run=False, only={e["design"] for e in todo})

    # Printify has synced by now, which is when it overwrites shipping with USD
    # figures. Fix it BEFORE anything goes live.
    repair_shipping("before going live")

    # Now that every listing exists we can say what this actually costs.
    preview_fees(etsy, listing_ids)

    # ── Stages 5-8, per design ──────────────────────────────────────────────
    live, held, failed, charged = [], [], [], []

    for entry in todo:
        name = entry["design"]
        listing_id = listing_ids.get(name)
        if not listing_id:
            continue

        print(f"\n  ──────── {name} ────────")

        # The placeholder guard: publishing lorem-ipsum to a live storefront is
        # worse than publishing nothing.
        if not entry.get("ai_used", True) and not allow_placeholder:
            print(f"     🛑 Gemini fell back to placeholder copy for this design — "
                  f"leaving it in DRAFTS.")
            print(f"        Fix the Gemini quota and re-run, or force it with "
                  f"--allow-placeholder.")
            held.append(name)
            continue

        try:
            listing = etsy.get_listing(listing_id)
            shop_id = int(listing["shop_id"])

            align_tags(etsy, printify, shop_id, listing_id, entry)

            etsy_url, was_charged = activate_and_wait(etsy, shop_id, listing_id)
            if was_charged:
                charged.append(name)
            if not etsy_url:
                held.append(name)
                continue

            add_extras(etsy, shop_id, listing_id, "sweatshirt", name,
                       images=not skip_info_images, video=not skip_video)

            record = {
                "design": name,
                "product_id": entry["product_id"],
                "etsy_listing_id": listing_id,
                "etsy_url": etsy_url,
                "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

            if skip_pinterest or pinterest is None:
                print("     ⏭️  Pinterest skipped.")
            else:
                design_path = Path("assets/designs_final") / name
                pin = create_pin_for(pinterest, etsy, design_path, name,
                                     listing_id, etsy_url,
                                     allow_placeholder=allow_placeholder)
                if pin is None:
                    # Refused (placeholder copy). Writing the record anyway
                    # would mark the design done and it would never be pinned.
                    held.append(name)
                    continue
                record.update(pin)
                record["pinterest_env"] = _current_env()

            # Written per design, immediately — a crash on design 7 never loses 1-6.
            _record_pinned(pinned_ledger, record)
            _save_json(PINNED_LEDGER, pinned_ledger)
            live.append(name)

        except EtsyAuthError as e:
            print(f"\n❌  Etsy auth problem: {e}\n")
            sys.exit(1)
        except PinterestAuthError as e:
            print(f"\n❌  Pinterest auth problem: {e}\n")
            sys.exit(1)
        except (EtsyError, PrintifyError, PinterestError) as e:
            print(f"     ❌ FAILED: {e}")
            failed.append(name)
            continue

    # A late Printify sync can overwrite the rates again — check once more.
    repair_shipping("final check")

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print(f"  🎉 Done!  {len(live)} live, {len(held)} held, {len(failed)} failed")
    print("═" * 60)
    if live:
        print(f"\n  ✅ Live on Etsy{' + pinned' if not skip_pinterest else ''}:")
        for p in pinned_ledger["products"]:
            if p["design"] in live:
                print(f"     • {p['design']}  →  {p['etsy_url']}")
                if p.get("pin_id"):
                    print(f"       📌 pin {p['pin_id']} on '{p['board']}'"
                          f"{'  (sandbox)' if p.get('pinterest_env') == 'sandbox' else ''}")
        if charged:
            print(f"\n  💸 Etsy listing fees charged this run: "
                  f"${0.20 * len(charged):.2f}  ({len(charged)} × $0.20)")
        else:
            print(f"\n  💸 No Etsy listing fees this run — every listing was "
                  f"already live.")
        if not skip_pinterest and is_sandbox():
            print(f"\n  🧪 Those pins are SANDBOX pins — visible only to you.")
            print(f"     Once Pinterest grants Standard access, set "
                  f"PINTEREST_ENV=production in .env and re-run the SAME command:")
            print(f"     every design above goes pending again and is re-pinned "
                  f"for real, at no extra Etsy cost.")
    if held:
        print(f"\n  ⏸️  Held in DRAFTS (not charged, retried next run): {', '.join(held)}")
    if failed:
        print(f"\n  ❌ Failed (retried next run): {', '.join(failed)}")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    if "--clear-ledger" in args:
        clear_pinned()
        sys.exit(0)

    if "--forget" in args:
        names = [a for a in args[args.index("--forget") + 1:] if not a.startswith("--")]
        if not names:
            print("⚠️  --forget needs at least one design file name, e.g.:")
            print('    python -m scripts.create.sweatshirt_automation --forget "ctrl_alt.png"')
        else:
            forget_pinned(names)
        sys.exit(0)

    only = None
    if "--only" in args:
        only = [a for a in args[args.index("--only") + 1:] if not a.startswith("--")]
        if not only:
            print("⚠️  --only needs at least one design file name, e.g.:")
            print('    python -m scripts.create.sweatshirt_automation --only "ctrl_alt.png"')
            sys.exit(1)

    if "--extras-only" in args:
        records = _load_pinned().get("products", [])
        if only:
            records = [r for r in records if r["design"].lower() in {n.lower() for n in only}]
        backfill("sweatshirt", records, dry_run="--dry-run" in args,
                 images="--skip-info-images" not in args, video="--skip-video" not in args)
        sys.exit(0)

    run(
        dry_run="--dry-run" in args,
        skip_upscale="--no-upscale" in args or "--skip-upscale" in args,
        skip_pinterest="--skip-pinterest" in args,
        allow_placeholder="--allow-placeholder" in args,
        only=only,
        process_all="--all" in args,
        connect_pinterest="--connect-pinterest" in args,
        skip_video="--skip-video" in args,
        skip_info_images="--skip-info-images" in args,
    )
