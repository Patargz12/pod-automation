"""
Pinterest_Market/prepare.py — build the pin queue (runs on YOUR PC)
====================================================================
For every design in Designs_To_Use/ × every shirt photo in Mockup/:

  1. Mockup corners: the first time a mockup photo is seen, a window opens and
     you click the 4 corners of the print area (saved to mockup_corners.json,
     never asked again for that photo).
  2. Etsy link: listing_map.json → marketing ledgers → shop search by filename.
     No exact ACTIVE listing → the pin links to the Etsy shop page instead
     (SHOP_URL in config.py), so every design in the folder still gets pinned.
  3. Render the design onto the photo → ready/<design>__<mockup>.jpg
  4. Gemini writes the pin title / description / alt text and picks a board.
  5. Board id resolved here (locally), so the cloud poster never looks one up.
  6. Append to queue.json as "pending". Re-running only adds what's new.

The cloud (GitHub Actions) then posts the queue 3 pins/day via post.py.
"""

from __future__ import annotations

from pathlib import Path

from Pinterest_Market.config import (
    CORNERS_FILE, DESIGNS_DIR, GARMENT, JPEG_QUALITY, LISTING_MAP, MOCKUP_DIR,
    PIN_WIDTH, QUEUE_FILE, READY_DIR, ROOT, SHOP_URL, image_files, load_json, load_queue,
    save_json,
)

REPO_ROOT = ROOT.parent


def _item_id(design: Path, mockup: Path) -> str:
    return f"{design.stem}__{mockup.stem}".replace(" ", "_")


def _ensure_corners(mockups: list[Path], repick: bool) -> dict:
    from services.mockup_service import pick_corners_interactive

    corners = {} if repick else load_json(CORNERS_FILE, {})
    for m in mockups:
        if m.name in corners:
            continue
        print(f"\n  🖱️  Draw the print area on {m.name} (window opening)…")
        picked = pick_corners_interactive(m)
        if picked is None:
            print(f"     ⏭️  skipped {m.name} — no corners picked")
            continue
        corners[m.name] = picked
        save_json(CORNERS_FILE, corners)
        print(f"     ✓ corners saved for {m.name}")
    return corners


def _render(design: Path, mockup: Path, corners: list, out: Path) -> None:
    from PIL import Image
    from services.mockup_service import warp_design_onto_shirt

    with Image.open(mockup) as shirt, Image.open(design) as art:
        result = warp_design_onto_shirt(shirt, art, corners)
    if result.width > PIN_WIDTH:
        h = round(result.height * PIN_WIDTH / result.width)
        result = result.resize((PIN_WIDTH, h), Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)


def _interleave(items: list[dict]) -> list[dict]:
    """Keep posted/failed history in place; reorder pending round-robin by
    design so the same design never goes out twice in a row."""
    done = [it for it in items if it.get("status") != "pending"]
    groups: dict[str, list[dict]] = {}
    for it in items:
        if it.get("status") == "pending":
            groups.setdefault(it["design"], []).append(it)
    pending: list[dict] = []
    while any(groups.values()):
        for key in list(groups):
            if groups[key]:
                pending.append(groups[key].pop(0))
    return done + pending


def run(dry_run: bool = False, only: str | None = None, repick: bool = False) -> int:
    from services.ai_content import generate_pin_content
    from services.etsy_service import EtsyClient, EtsyAuthError, EtsyError
    from services.pinterest_service import PinterestClient, PinterestAuthError, PinterestError
    from scripts.pinterest.pin_folder import resolve_listing
    from scripts.create.sweatshirt_automation import BOARD_DESCRIPTIONS, _board_name

    print("\n" + "═" * 64)
    print(f"  📌  PINTEREST PREPARE — build the queue{'  (DRY RUN)' if dry_run else ''}")
    print("═" * 64)

    designs = image_files(DESIGNS_DIR)
    if only:
        designs = [d for d in designs if d.name == only]
    mockups = image_files(MOCKUP_DIR)
    if not designs or not mockups:
        print(f"     Need at least 1 design in {DESIGNS_DIR.name}/ and 1 photo in {MOCKUP_DIR.name}/.")
        return 1
    print(f"     {len(designs)} design(s) × {len(mockups)} mockup(s)")

    corners = _ensure_corners(mockups, repick)
    mockups = [m for m in mockups if m.name in corners]
    if not mockups:
        print("     No mockup has corners — nothing to render.")
        return 1

    queue = load_queue()
    known = {it["id"] for it in queue["items"]}
    todo = [(d, m) for d in designs for m in mockups if _item_id(d, m) not in known]
    if not todo:
        print("     Everything is already queued. Add designs or mockups, then re-run.")
        return 0

    try:
        etsy = EtsyClient()
        shop_id = etsy.get_shop_id()
        pinterest = None if dry_run else PinterestClient()
    except (EtsyAuthError, PinterestAuthError) as e:
        print(f"     ❌ Auth: {e}")
        return 1

    listing_map = {k: int(v) for k, v in load_json(LISTING_MAP, {}).items()}
    listing_cache: dict[str, int | None] = {}
    shop_cache = None
    added, skipped = [], []

    for design, mockup in todo:
        item_id = _item_id(design, mockup)
        print(f"\n  ── {design.name}  on  {mockup.name}")
        try:
            # ── 1. Etsy link (once per design) ──────────────────────────────
            if design.name not in listing_cache:
                listing_id = listing_map.get(design.name)
                how = "listing_map.json"
                if not listing_id:
                    listing_id, how, shop_cache = resolve_listing(etsy, shop_id, design, shop_cache)
                if listing_id:
                    listing = etsy.get_listing(listing_id)
                    if listing.get("state") != "active":
                        how, listing_id = f"listing {listing_id} is {listing.get('state')}", None
                    else:
                        print(f"     🛍️  {listing_id} ({how}) · {listing.get('title', '')[:70]}")
                if not listing_id:
                    print(f"     🏪 no exact Etsy listing ({how.splitlines()[0]}) → linking to the shop")
                    print(f'        (optional: add  "{design.name}": <listing_id>  to listing_map.json)')
                listing_cache[design.name] = listing_id
            listing_id = listing_cache[design.name]
            link = f"https://www.etsy.com/listing/{listing_id}" if listing_id else SHOP_URL

            # ── 2. Render ───────────────────────────────────────────────────
            out = READY_DIR / f"{item_id}.jpg"
            _render(design, mockup, corners[mockup.name], out)
            print(f"     🖼️  rendered → {out.relative_to(REPO_ROOT).as_posix()}")

            # ── 3. Copy ─────────────────────────────────────────────────────
            nice = design.stem.replace("_", " ").title()
            title, desc, alt, board_key, ai_used = generate_pin_content(
                out, fallback_title=f"Funny {nice} T-Shirt",
                fallback_description="A funny unisex t-shirt. Tap to see it on Etsy.",
                garment=GARMENT,
            )
            if not ai_used:
                print("     🛑 Gemini failed — not queueing placeholder copy. Re-run later.")
                skipped.append((item_id, "Gemini unavailable"))
                continue
            board_name = _board_name(board_key)
            print(f"     🤖 [{board_name}] {title}")

            if dry_run:
                print(f"        desc: {desc}")
                print(f"        → {link}")
                continue

            # ── 4. Board id + queue ─────────────────────────────────────────
            board_id = pinterest.ensure_board(board_name, BOARD_DESCRIPTIONS.get(board_key, ""))
            queue["items"].append({
                "id": item_id, "design": design.name, "mockup": mockup.name,
                "image": out.relative_to(REPO_ROOT).as_posix(),
                "title": title, "description": desc, "alt_text": alt, "link": link,
                "board_id": board_id, "board_name": board_name, "status": "pending",
            })
            queue["items"] = _interleave(queue["items"])
            save_json(QUEUE_FILE, queue)
            added.append(item_id)
            print("     ✅ queued")
        except (EtsyError, PinterestError) as e:
            print(f"     ❌ {str(e)[:300]}")
            skipped.append((item_id, str(e)[:120]))

    pending = sum(1 for it in queue["items"] if it.get("status") == "pending")
    print("\n" + "═" * 64)
    print(f"  {len(added)} queued · {len(skipped)} skipped · {pending} pending total "
          f"(≈{pending / 3:.0f} days at 3/day)")
    for name, why in skipped:
        print(f"     ⏭️  {name}: {why}")
    if added:
        print("\n  Next: commit + push Pinterest_Market/ so GitHub Actions can post them.")
    print()
    return 0
