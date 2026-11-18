"""
pin_folder.py — pin every image in assets/to_pin/ to its live Etsy listing
============================================================================

    python -m scripts.pinterest.pin_folder --dry-run          # resolve listings + write copy, pin nothing
    python -m scripts.pinterest.pin_folder                    # create the pins
    python -m scripts.pinterest.pin_folder --only Ask_Me_Black_Shirt.png

For products that are ALREADY live on Etsy. Each image becomes one pin:
  1. find its Etsy listing (OVERRIDES → marketing ledgers → search the shop)
  2. confirm the listing is active (a draft would be a dead link)
  3. Gemini writes the Pinterest title / description / alt text and picks a board
  4. POST the pin, linking to https://www.etsy.com/listing/<id>
  5. record it in caching/pinned_folder.json so a re-run skips it

The listing lookup never guesses: if a shop search returns anything other than
exactly one match, the image is skipped and the candidates are printed so the
listing id can be added to OVERRIDES by hand.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

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

from services.ai_content import generate_pin_content
from services.etsy_service import EtsyClient, EtsyError, EtsyAuthError
from services.pinterest_service import (
    PinterestClient,
    PinterestError,
    PinterestAuthError,
    is_sandbox,
)
from services.printify_service import CACHE_DIR, _load_json, _save_json
from scripts.create.sweatshirt_automation import BOARD_DESCRIPTIONS, _board_name

PIN_DIR       = Path("assets/to_pin")
PINNED_LEDGER = CACHE_DIR / "pinned_folder.json"
IMAGE_EXTS    = {".png", ".jpg", ".jpeg", ".webp"}

# Marketing ledgers already record design → listing for products this repo
# pushed through the full pipeline.
MARKETED = {
    "sweatshirt": CACHE_DIR / "marketed_sweatshirt.json",
    "tee":        CACHE_DIR / "marketed_tshirt.json",
}

# Per-image hints. `garment` overrides the filename guess, `listing_id` skips
# the lookup entirely, `keywords` must ALL appear in the Etsy title for a match.
OVERRIDES: dict[str, dict] = {
    # Etsy title is "AI Model Loading Sweatshirt…" — the joke is only in the description.
    "Ask_Me_To_Think.png":          {"garment": "sweatshirt", "listing_id": 4584124074},
    "Ask_Me_Black_Shirt.png":       {"keywords": ["ask me", "think"]},
    "arguing_Black_Shirt.png":      {"keywords": ["arguing"]},
    "arguing_Black_Sweatshirt.png": {"keywords": ["arguing"]},
    "agress_Black_Sweatshirt.png":  {},
    # Etsy title is "I Don't Always Prompt Just One More…" — no "one more prompt" phrase.
    "one_more_prompt_Black_Shirt.png": {"listing_id": 4586911525},
    # Etsy title is "…I Wasn't Just Prompting I Was Training An AI…" — no "replace job".
    "Replace_Job_Black_Shirt.png":  {"listing_id": 4586910285},
    "Still Smarter Than AI Typography_Black_Shirt.png": {"listing_id": 4594706038},
}


def _garment(path: Path) -> str:
    forced = OVERRIDES.get(path.name, {}).get("garment")
    if forced:
        return forced
    return "sweatshirt" if "sweatshirt" in path.stem.lower() else "tee"


def _design_stem(path: Path) -> str:
    """'arguing_Black_Sweatshirt' → 'arguing' — the name the ledgers use."""
    stem = path.stem
    for suffix in ("_Black_Sweatshirt", "_Black_Shirt"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _title_is_garment(title: str, garment: str) -> bool:
    t = title.lower()
    is_sweat = "sweatshirt" in t or "crewneck" in t or "hoodie" in t
    return is_sweat if garment == "sweatshirt" else (not is_sweat and ("shirt" in t or "tee" in t))


def _active_listings(etsy: EtsyClient, shop_id: int) -> list[dict]:
    out, offset = [], 0
    while True:
        page = etsy._request("GET", f"/application/shops/{shop_id}/listings",
                             params={"state": "active", "limit": 100, "offset": offset})
        items = page.get("results", []) or []
        out.extend(items)
        if len(items) < 100:
            return out
        offset += 100


def resolve_listing(etsy: EtsyClient, shop_id: int, path: Path,
                    shop_cache: list[dict] | None) -> tuple[int | None, str, list[dict] | None]:
    """(listing_id or None, how it was found / why not, refreshed shop cache)."""
    hint = OVERRIDES.get(path.name, {})
    if hint.get("listing_id"):
        return int(hint["listing_id"]), "override", shop_cache

    garment = _garment(path)
    stem = _design_stem(path)
    for rec in _load_json(MARKETED[garment], {"products": []}).get("products", []):
        if Path(rec.get("design", "")).stem == stem and rec.get("etsy_listing_id"):
            return int(rec["etsy_listing_id"]), f"{MARKETED[garment].name}", shop_cache

    keywords = [k.lower() for k in hint.get("keywords") or [stem.replace("_", " ")]]
    if shop_cache is None:
        shop_cache = _active_listings(etsy, shop_id)
    matches = [l for l in shop_cache
               if all(k in l.get("title", "").lower() for k in keywords)
               and _title_is_garment(l.get("title", ""), garment)]
    if len(matches) == 1:
        return int(matches[0]["listing_id"]), "shop search", shop_cache
    if not matches:
        return None, f"no active {garment} listing with {keywords} in the title", shop_cache
    lines = "\n".join(f"           • {m['listing_id']}  {m['title'][:90]}" for m in matches)
    return None, f"{len(matches)} candidates — add the right one to OVERRIDES:\n{lines}", shop_cache


def main(dry_run: bool = False, only: str | None = None) -> int:
    env = "sandbox" if is_sandbox() else "production"
    print("\n" + "═" * 64)
    print(f"  📌  PIN FOLDER — {PIN_DIR}/ → Pinterest [{env.upper()}]"
          f"{'  (DRY RUN)' if dry_run else ''}")
    print("═" * 64)

    images = sorted(p for p in PIN_DIR.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    if only:
        images = [p for p in images if p.name == only]
    if not images:
        print("     Nothing to pin.\n")
        return 0

    try:
        etsy = EtsyClient()
        shop_id = etsy.get_shop_id()
        pinterest = PinterestClient()
        acct = pinterest.get_user_account()
    except (EtsyAuthError, PinterestAuthError) as e:
        print(f"     ❌ Auth: {e}\n")
        return 1
    print(f"     Etsy shop {shop_id} · Pinterest @{acct.get('username', '?')}")

    ledger = _load_json(PINNED_LEDGER, {"products": []})
    done = {p["image"] for p in ledger["products"]
            if p.get("pin_id") and p.get("pinterest_env") == env}

    shop_cache = None
    pinned, skipped = [], []
    for path in images:
        print(f"\n  ── {path.name}")
        if path.name in done:
            print("     ✓ already pinned in this environment — skipping")
            continue
        try:
            garment = _garment(path)
            listing_id, how, shop_cache = resolve_listing(etsy, shop_id, path, shop_cache)
            if listing_id is None:
                print(f"     🛑 Etsy listing not found: {how}")
                skipped.append((path.name, "listing not found"))
                continue
            listing = etsy.get_listing(listing_id)
            print(f"     🛍️  listing {listing_id} ({how}) · {listing.get('state')} · "
                  f"{listing.get('title', '')[:70]}")
            if listing.get("state") != "active":
                print("     🛑 listing is not active — a pin would be a dead link")
                skipped.append((path.name, f"listing {listing.get('state')}"))
                continue
            etsy_url = f"https://www.etsy.com/listing/{listing_id}"

            noun = "Sweatshirt" if garment == "sweatshirt" else "T-Shirt"
            title, description, alt_text, board_key, ai_used = generate_pin_content(
                path,
                fallback_title=f"Funny {_design_stem(path).replace('_', ' ').title()} {noun}",
                fallback_description=f"A funny unisex {noun.lower()}. Tap to see it on Etsy.",
                garment="tee" if garment == "tee" else "sweatshirt",
            )
            board_name = _board_name(board_key)
            print(f"     🤖 board '{board_name}'")
            print(f"        title: {title}")
            print(f"        desc : {description}")
            print(f"        alt  : {alt_text}")
            if not ai_used:
                print("     🛑 Gemini failed — placeholder copy, NOT pinning. Re-run later.")
                skipped.append((path.name, "Gemini unavailable"))
                continue
            if dry_run:
                print(f"     🔍 would pin → {etsy_url}")
                continue

            board_id = pinterest.ensure_board(board_name, BOARD_DESCRIPTIONS.get(board_key, ""))
            pin = pinterest.create_pin(board_id=board_id, title=title, description=description,
                                       link=etsy_url, alt_text=alt_text, image_path=path)
            print(f"     📌 pin {pin.get('id')} created")

            record = {
                "image": path.name, "garment": garment,
                "etsy_listing_id": listing_id, "etsy_url": etsy_url,
                "pin_id": pin.get("id"), "board": board_name, "board_key": board_key,
                "pin_title": title, "pinterest_env": env,
                "pinned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            ledger["products"] = [p for p in ledger["products"] if p["image"] != path.name]
            ledger["products"].append(record)
            _save_json(PINNED_LEDGER, ledger)
            pinned.append(record)
        except (EtsyError, PinterestError) as e:
            print(f"     ❌ {e}")
            skipped.append((path.name, str(e)[:120]))

    print("\n" + "═" * 64)
    print(f"  {len(pinned)} pinned · {len(skipped)} skipped")
    for r in pinned:
        print(f"     📌 {r['image']} → pin {r['pin_id']} on '{r['board']}' → {r['etsy_url']}")
    for name, why in skipped:
        print(f"     ⏭️  {name}: {why}")
    print()
    return 0 if not skipped else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)
    only = args[args.index("--only") + 1] if "--only" in args else None
    sys.exit(main(dry_run="--dry-run" in args, only=only))
