"""
printify_service.py — Upload designs to Printify, create & publish products
============================================================================
This is Step 3 of the pipeline (after upscale + mockup). For every design in
`Final Designs/`, it:

  1. Uploads the image to Printify        (POST /uploads/images.json)
  2. Discovers the blueprint + provider    (GET  /catalog/...)
  3. Creates the product                    (POST /shops/{id}/products.json)
  4. Publishes it                           (POST .../publish.json)

so it appears under "My Products" in your Printify dashboard.

------------------------------------------------------------------------------
WHAT THE PRINTIFY PUBLIC API *CANNOT* DO (printed as a manual checklist):
  • Select a specific mockup ("Hanging 1", front-only) — dashboard only.
  • Toggle Etsy "Free shipping"            — Etsy/Printify dashboard only.
  • Toggle Etsy "Off-site ads"             — Etsy dashboard only.
  • (Title/description/tags ARE automated — Gemini 2.5 Flash via
     GEMINI_API_KEY_1..5; placeholders only if no key works.)
  • Store prices in PHP (₱)                — Printify only supports
                                             USD/EUR/GBP/CAD/AUD. Numbers are
                                             stored as-is in the shop currency.
------------------------------------------------------------------------------

SETUP:
  1. pip install -r requirements.txt
  2. Copy .env.example -> .env and paste your PRINTIFY_API_TOKEN
     (Printify Dashboard -> My Profile -> Connections -> Generate).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import requests
from PIL import Image

from services.ai_content import generate_listing_content

# Ensure UTF-8 output (emoji / box chars) when run/imported on a Windows
# cp1252 console or with redirected stdout.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv not installed — env vars must be set manually
    pass


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG — edit everything here
# ══════════════════════════════════════════════════════════════════════════════

# Catalog target. Blueprint id is discovered at runtime by matching brand+model
# (never hard-coded, since ids are not stable across accounts).
BLUEPRINT_QUERY = {"brand": "Bella+Canvas", "model": "3001"}
PROVIDER_NAME   = "Monster Digital"

# Only these colors are enabled. Matched case-insensitively against Printify's
# exact catalog color names. If a name doesn't match, the dry run warns you.
ENABLED_COLORS = ["Black", "Dark Grey", "Maroon", "Navy", "Brown"]

# Retail price per size, as the literal number you want displayed.
# (Printify cannot store PHP; these are stored in your shop's currency.)
# NOTE: Printify names the XXL size "2XL". Monster Digital tops out at 3XL for
# this blueprint (no 4XL), so 4XL is intentionally omitted.
# Also note: "Dark Grey" is only offered in M/XL/2XL/3XL — the missing S/L combos
# are skipped automatically.
PRICE_BY_SIZE = {
    "S":   1604,
    "M":   1604,
    "L":   1604,
    "XL":  1604,
    "2XL": 1789,
    "3XL": 1974,
}

# Fallback tags — used only when AI generation fails. Normally each product
# gets design-specific tags from Gemini (see services/ai_content.py).
TAGS = [
    "programmer", "coding", "software engineer", "software developer",
    "developer gift", "coding humor", "tech humor", "artificial intelligence",
    "AI shirt", "prompt engineering", "computer science", "geek gift", "nerd gift",
]

# Title / description fallbacks — used only when every GEMINI_API_KEY fails.
TITLE_PLACEHOLDER = "Lorem ipsum dolor sit amet"
DESC_PLACEHOLDER  = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, "
    "quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat."
)

HIDE_IN_STORE  = True       # -> product.visible = False
PRINT_POSITION = "front"    # front-only print (no back artwork)

# Physical print size on the shirt, CENTERED. The design is scaled to fit inside
# this box with its aspect ratio preserved (so it never gets stretched).
#
# How it maps to Printify: DTG print files are 300 DPI, so the print area's
# physical width = placeholder_width_px / 300. Printify's image `scale` is the
# fraction of the print-area WIDTH the design occupies, so:
#     scale = desired_display_width_in / print_area_width_in
# This is computed at runtime from the live print-area size — never hardcoded.
DESIGN_WIDTH_IN  = 7.06
DESIGN_HEIGHT_IN = 10.62
PRINT_DPI        = 300

# ──────────────────────────────────────────────────────────────────────────────

API_BASE      = "https://api.printify.com/v1"
USER_AGENT    = "pod-automation/1.0 (Printify uploader)"   # Printify rejects requests w/o a UA
CACHE_FILE    = Path("printify_cache.json")
LEDGER_FILE   = Path("published.json")


# ══════════════════════════════════════════════════════════════════════════════
#  Printify API client
# ══════════════════════════════════════════════════════════════════════════════

class PrintifyError(RuntimeError):
    pass


class PrintifyClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json;charset=utf-8",
        })

    def _request(self, method: str, path: str, **kwargs):
        url = f"{API_BASE}/{path.lstrip('/')}"
        for attempt in range(4):
            resp = self.session.request(method, url, timeout=60, **kwargs)

            if resp.status_code == 401:
                raise PrintifyError(
                    "401 Unauthorized — token missing/invalid/expired or lacks the "
                    "required scopes (uploads.write, products.write, shops.read, catalog.read)."
                )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                print(f"     ⏳ Rate limited (429). Waiting {wait}s…")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = 2 ** attempt
                print(f"     ⚠️  Server {resp.status_code}. Retrying in {wait}s…")
                time.sleep(wait)
                continue
            if not resp.ok:
                raise PrintifyError(f"{method} {path} → {resp.status_code}: {resp.text[:500]}")

            return resp.json() if resp.text else {}

        raise PrintifyError(f"{method} {path} failed after retries (last status {resp.status_code}).")

    def get(self, path):           return self._request("GET", path)
    def post(self, path, body):    return self._request("POST", path, data=json.dumps(body))
    def put(self, path, body):     return self._request("PUT", path, data=json.dumps(body))

    # ── Shops ──────────────────────────────────────────────────────────────
    def get_shops(self) -> list[dict]:
        return self.get("/shops.json")

    # ── Catalog ────────────────────────────────────────────────────────────
    def get_blueprints(self) -> list[dict]:
        return self.get("/catalog/blueprints.json")

    def get_providers(self, blueprint_id: int) -> list[dict]:
        return self.get(f"/catalog/blueprints/{blueprint_id}/print_providers.json")

    def get_variants(self, blueprint_id: int, provider_id: int) -> dict:
        return self.get(
            f"/catalog/blueprints/{blueprint_id}/print_providers/{provider_id}/variants.json"
        )

    # ── Uploads ────────────────────────────────────────────────────────────
    def upload_image(self, path: Path) -> dict:
        contents = base64.b64encode(path.read_bytes()).decode("ascii")
        return self.post("/uploads/images.json", {
            "file_name": path.name,
            "contents": contents,
        })

    # ── Products ───────────────────────────────────────────────────────────
    def create_product(self, shop_id: int, body: dict) -> dict:
        return self.post(f"/shops/{shop_id}/products.json", body)

    def get_product(self, shop_id: int, product_id: str) -> dict:
        return self.get(f"/shops/{shop_id}/products/{product_id}.json")

    def update_product(self, shop_id: int, product_id: str, body: dict) -> dict:
        return self.put(f"/shops/{shop_id}/products/{product_id}.json", body)

    def product_exists(self, shop_id: int, product_id: str) -> bool:
        try:
            self.get_product(shop_id, product_id)
            return True
        except PrintifyError as e:
            if "404" in str(e) or "not_found" in str(e).lower():
                return False
            raise

    def publish_product(self, shop_id: int, product_id: str) -> dict:
        return self.post(f"/shops/{shop_id}/products/{product_id}/publish.json", {
            "title": True, "description": True, "images": True,
            "variants": True, "tags": True, "keyFeatures": True,
            "shipping_template": True,
        })


# ══════════════════════════════════════════════════════════════════════════════
#  Cache & ledger helpers
# ══════════════════════════════════════════════════════════════════════════════

def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
#  Catalog discovery (cached — catalog rarely changes, 100 req/min limit)
# ══════════════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def discover_catalog(client: PrintifyClient) -> dict:
    """Resolve blueprint_id, provider_id, and the enabled variant set.

    Returns a dict: {blueprint_id, provider_id, variant_ids (sorted),
                     variants: [{id, color, size, price_cents}], missing: [...]}.
    Cached to printify_cache.json keyed by the config so changes invalidate it.
    """
    cache_key = _norm(json.dumps([BLUEPRINT_QUERY, PROVIDER_NAME,
                                  sorted(ENABLED_COLORS), sorted(PRICE_BY_SIZE)]))
    cache = _load_json(CACHE_FILE, {})
    if cache.get("key") == cache_key and "front_width_px" in cache.get("data", {}):
        print("  📦 Using cached catalog discovery (delete printify_cache.json to refresh).")
        return cache["data"]

    # 1) Blueprint
    print(f"  🔎 Finding blueprint  brand='{BLUEPRINT_QUERY['brand']}' model='{BLUEPRINT_QUERY['model']}'…")
    blueprint = None
    for bp in client.get_blueprints():
        if (_norm(bp.get("brand", "")) == _norm(BLUEPRINT_QUERY["brand"])
                and _norm(str(bp.get("model", ""))) == _norm(str(BLUEPRINT_QUERY["model"]))):
            blueprint = bp
            break
    if not blueprint:
        raise PrintifyError(
            f"Blueprint not found for {BLUEPRINT_QUERY}. "
            "Check the brand/model strings in printify_service.py CONFIG."
        )
    bp_id = blueprint["id"]
    print(f"     ✓ blueprint #{bp_id}: {blueprint.get('title')}")

    # 2) Provider
    print(f"  🔎 Finding provider '{PROVIDER_NAME}'…")
    provider = None
    providers = client.get_providers(bp_id)
    for p in providers:
        if _norm(p.get("title", "")) == _norm(PROVIDER_NAME):
            provider = p
            break
    if not provider:
        names = ", ".join(p.get("title", "?") for p in providers)
        raise PrintifyError(
            f"Provider '{PROVIDER_NAME}' not available for blueprint #{bp_id}. "
            f"Available: {names}"
        )
    pp_id = provider["id"]
    print(f"     ✓ provider #{pp_id}: {provider.get('title')}")

    # 3) Variants → filter to enabled colors × priced sizes
    print(f"  🔎 Resolving variants ({len(ENABLED_COLORS)} colors × {len(PRICE_BY_SIZE)} sizes)…")
    raw = client.get_variants(bp_id, pp_id)
    all_variants = raw.get("variants", raw if isinstance(raw, list) else [])

    # Front print-area dimensions (px) — used to convert inches -> scale.
    front_w_px = front_h_px = None
    for v in all_variants:
        for ph in v.get("placeholders", []):
            if ph.get("position") == PRINT_POSITION:
                front_w_px, front_h_px = ph["width"], ph["height"]
                break
        if front_w_px:
            break
    if not front_w_px:
        raise PrintifyError(
            f"No '{PRINT_POSITION}' print placeholder found for blueprint #{bp_id} / provider #{pp_id}."
        )
    print(f"     ✓ {PRINT_POSITION} print area: {front_w_px}x{front_h_px}px "
          f"= {front_w_px/PRINT_DPI:.2f}in x {front_h_px/PRINT_DPI:.2f}in @ {PRINT_DPI} DPI")

    wanted_colors = {_norm(c) for c in ENABLED_COLORS}
    wanted_sizes  = {_norm(s) for s in PRICE_BY_SIZE}

    chosen, found_colors, found_sizes = [], set(), set()
    for v in all_variants:
        opts  = v.get("options", {})
        color = opts.get("color", "")
        size  = opts.get("size", "")
        if _norm(color) in wanted_colors and _norm(size) in wanted_sizes:
            found_colors.add(_norm(color))
            found_sizes.add(_norm(size))
            # map normalized size back to the PRICE_BY_SIZE key for the price
            price_key = next(k for k in PRICE_BY_SIZE if _norm(k) == _norm(size))
            chosen.append({
                "id": v["id"],
                "color": color,
                "size": size,
                "price_cents": PRICE_BY_SIZE[price_key] * 100,  # display number × 100 (cents)
            })

    missing = []
    for c in ENABLED_COLORS:
        if _norm(c) not in found_colors:
            missing.append(f"color '{c}'")
    for s in PRICE_BY_SIZE:
        if _norm(s) not in found_sizes:
            missing.append(f"size '{s}'")

    data = {
        "blueprint_id": bp_id,
        "provider_id": pp_id,
        "front_width_px": front_w_px,
        "front_height_px": front_h_px,
        "variants": sorted(chosen, key=lambda x: (x["color"], x["size"])),
        "variant_ids": sorted(v["id"] for v in chosen),
        "missing": missing,
    }
    _save_json(CACHE_FILE, {"key": cache_key, "data": data})
    return data


# ══════════════════════════════════════════════════════════════════════════════
#  Product payload
# ══════════════════════════════════════════════════════════════════════════════

def compute_placement(catalog: dict, design_w_px: int, design_h_px: int) -> dict:
    """Center the design and scale it to fit inside DESIGN_WIDTH_IN x DESIGN_HEIGHT_IN
    (aspect preserved). Returns {placement, display_w_in, display_h_in, design_aspect}.

    Printify `scale` = fraction of print-area WIDTH. The design is "contained" in the
    target box, so width is limited by both the target width and (target height x aspect).
    """
    pa_w_in = catalog["front_width_px"] / PRINT_DPI
    design_aspect = design_w_px / design_h_px                       # w / h
    display_w_in = min(DESIGN_WIDTH_IN, DESIGN_HEIGHT_IN * design_aspect)
    display_h_in = display_w_in / design_aspect
    scale = display_w_in / pa_w_in
    return {
        "placement": {"x": 0.5, "y": 0.5, "scale": round(scale, 4), "angle": 0},
        "display_w_in": display_w_in,
        "display_h_in": display_h_in,
        "design_aspect": design_aspect,
    }


def build_product_body(catalog: dict, image_id: str, title: str,
                       description: str, tags: list[str], placement: dict) -> dict:
    variants = [
        {"id": v["id"], "price": v["price_cents"], "is_enabled": True}
        for v in catalog["variants"]
    ]
    return {
        "title": title,
        "description": description,
        "blueprint_id": catalog["blueprint_id"],
        "print_provider_id": catalog["provider_id"],
        "tags": tags,
        "visible": not HIDE_IN_STORE,   # HIDE_IN_STORE -> visible: False
        "variants": variants,
        "print_areas": [
            {
                "variant_ids": catalog["variant_ids"],
                "placeholders": [
                    {
                        "position": PRINT_POSITION,
                        "images": [{"id": image_id, **placement}],
                    }
                ],
            }
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Pretty-printing
# ══════════════════════════════════════════════════════════════════════════════

def _print_shop(shop: dict):
    print(f"  🏪 Shop #{shop['id']}: {shop.get('title')}  "
          f"(channel: {shop.get('sales_channel', 'unknown')})")


def _print_price_preview(catalog: dict):
    print("\n  💲 Price preview (number stored as-is in your SHOP currency, not PHP):")
    seen = set()
    for v in catalog["variants"]:
        key = v["size"]
        if key in seen:
            continue
        seen.add(key)
        display = v["price_cents"] / 100
        print(f"       {v['size']:>4} → {display:,.2f}   (sent as {v['price_cents']} cents)")
    colors = sorted({v["color"] for v in catalog["variants"]})
    print(f"  🎨 Colors enabled: {', '.join(colors)}")
    print(f"  📦 Total variants: {len(catalog['variants'])}")
    if catalog["missing"]:
        print(f"  ⚠️  NOT FOUND in catalog (skipped): {', '.join(catalog['missing'])}")
        print(f"      → Fix the names in printify_service.py CONFIG and delete printify_cache.json.")


def print_manual_checklist(results: list[dict]):
    print("\n" + "═" * 56)
    print("  📋 MANUAL STEPS — not possible via the Printify API")
    print("═" * 56)
    print("  Do these in the Printify/Etsy dashboard for each product:")
    print("    1. Set the mockup to 'Hanging 1' (FRONT side only, no back).")
    print("    2. Toggle ON 'Free shipping'.")
    print("    3. Toggle ON 'Etsy off-site ads'.")
    print("    4. Verify the currency & prices (these are NOT in PHP — Printify")
    print("       supports USD/EUR/GBP/CAD/AUD only).")
    print("    5. Review the title, description & tags (Gemini-generated if any")
    print("       GEMINI_API_KEY_1..5 is set; otherwise placeholder).")
    if results:
        print("\n  Products created this run:")
        for r in results:
            print(f"    • {r['design']:<28} → product {r['product_id']}")


# ══════════════════════════════════════════════════════════════════════════════
#  Ledger maintenance (published.json)
# ══════════════════════════════════════════════════════════════════════════════

def reconcile_ledger() -> int:
    """Drop ledger entries whose Printify product no longer exists.

    Use after deleting products in the Printify dashboard — this re-checks each
    recorded product against Printify and forgets the ones that are gone, so they
    get re-created on the next run. Returns the number of stale entries removed.
    """
    print("\n" + "═" * 56)
    print("  🔄 Reconciling ledger with Printify")
    print("═" * 56)

    token = os.environ.get("PRINTIFY_API_TOKEN", "").strip()
    if not token:
        print("  ⏭️  Skipped: PRINTIFY_API_TOKEN is not set.\n")
        return 0

    ledger = _load_json(LEDGER_FILE, {"products": []})
    entries = ledger.get("products", [])
    if not entries:
        print(f"  ℹ️  {LEDGER_FILE.name} is empty — nothing to reconcile.\n")
        return 0

    client = PrintifyClient(token)
    kept, removed = [], []
    for p in entries:
        exists = client.product_exists(p["shop_id"], p["product_id"])
        (kept if exists else removed).append(p)
        mark = "✓ exists" if exists else "✗ gone — forgetting"
        print(f"     {mark}: {p['design']}  ({p['product_id']})")

    ledger["products"] = kept
    _save_json(LEDGER_FILE, ledger)
    print(f"\n  🧹 Removed {len(removed)} stale entr{'y' if len(removed)==1 else 'ies'}; "
          f"{len(kept)} still live. Forgotten designs will be re-created next run.\n")
    return len(removed)


def update_listings(designs_dir: Path = Path("Final Designs")) -> int:
    """Regenerate title/description/tags for every product already in the ledger.

    For each entry in published.json: look at the design image with Gemini,
    PUT the new title/description/tags onto the existing Printify product, and
    republish so the changes sync to Etsy. Products whose AI generation fails
    are left untouched (never overwritten with placeholders). Returns the
    number of products updated.
    """
    print("\n" + "═" * 56)
    print("  ✍️  Updating listings (AI title/description/tags)")
    print("═" * 56)

    token = os.environ.get("PRINTIFY_API_TOKEN", "").strip()
    if not token:
        print("  ⏭️  Skipped: PRINTIFY_API_TOKEN is not set.\n")
        return 0

    entries = _load_json(LEDGER_FILE, {"products": []}).get("products", [])
    if not entries:
        print(f"  ℹ️  {LEDGER_FILE.name} is empty — nothing to update.\n")
        return 0

    client = PrintifyClient(token)
    updated, skipped = 0, []
    for p in entries:
        name = p["design"]
        print(f"\n  ──────── {name} ────────")
        design = designs_dir / name
        if not design.exists():
            print(f"     ⚠️  Design file not found in '{designs_dir}/' — skipping.")
            skipped.append(name)
            continue

        try:
            if not client.product_exists(p["shop_id"], p["product_id"]):
                print(f"     ✗ Product {p['product_id']} no longer exists on Printify — "
                      f"skipping (run --reconcile to clean the ledger).")
                skipped.append(name)
                continue

            title, description, tags, ai_used = generate_listing_content(
                design, TITLE_PLACEHOLDER, DESC_PLACEHOLDER, fallback_tags=TAGS)
            if not ai_used:
                print(f"     ⚠️  AI generation failed — keeping the current listing untouched.")
                skipped.append(name)
                continue

            print(f"     📝 Title: {title[:70]}{'…' if len(title) > 70 else ''}")
            print(f"     🏷️  Tags ({len(tags)}): {', '.join(tags[:6])}{'…' if len(tags) > 6 else ''}")

            client.update_product(p["shop_id"], p["product_id"], {
                "title": title,
                "description": description,
                "tags": tags,
            })
            print(f"     ✓ product {p['product_id']} updated")

            client.publish_product(p["shop_id"], p["product_id"])
            print(f"     ✓ republish accepted (Etsy sync is async — may take a moment)")
            updated += 1

        except Exception as e:
            print(f"     ❌ FAILED: {e}")
            skipped.append(name)
            continue

    print(f"\n  🎉 Listings updated: {updated} of {len(entries)} product(s).")
    if skipped:
        print(f"  ⚠️  Skipped/failed: {', '.join(skipped)}")
    print()
    return updated


def clear_ledger() -> int:
    """Wipe the entire ledger — every design will be (re)created on the next run."""
    n = len(_load_json(LEDGER_FILE, {"products": []}).get("products", []))
    _save_json(LEDGER_FILE, {"products": []})
    print(f"🧹 Cleared {n} entr{'y' if n==1 else 'ies'} from {LEDGER_FILE.name}. "
          f"All designs in 'Final Designs/' will be (re)created on the next run.")
    return n


def forget_designs(names: list[str]) -> int:
    """Remove specific designs from the ledger by file name (no Printify call)."""
    ledger = _load_json(LEDGER_FILE, {"products": []})
    wanted = {n.lower() for n in names}
    before = ledger.get("products", [])
    kept = [p for p in before if p["design"].lower() not in wanted]
    ledger["products"] = kept
    _save_json(LEDGER_FILE, ledger)
    removed = len(before) - len(kept)
    print(f"🧹 Forgot {removed} design(s) from {LEDGER_FILE.name}: {', '.join(names)}")
    return removed


# ══════════════════════════════════════════════════════════════════════════════
#  Orchestration — called from execute.py
# ══════════════════════════════════════════════════════════════════════════════

def run_printify_upload(designs: list[Path], dry_run: bool = False) -> int:
    """Upload + create + publish each design on Printify. Returns # of products created.

    Skips designs already recorded in published.json (dedup across runs).
    With dry_run=True: does discovery + prints the payload/preview, creates nothing.
    """
    print("\n" + "═" * 56)
    print("  🚀 Step 3: Printify upload" + ("  (DRY RUN — nothing will be created)" if dry_run else ""))
    print("═" * 56)

    token = os.environ.get("PRINTIFY_API_TOKEN", "").strip()
    if not token:
        print("\n  ⏭️  Skipped: PRINTIFY_API_TOKEN is not set.")
        print("      1. Copy .env.example → .env")
        print("      2. Paste your token (Printify → My Profile → Connections → Generate)")
        print("      3. Re-run.  (Add --no-publish to skip this step intentionally.)\n")
        return 0

    client = PrintifyClient(token)

    # ── Pick the shop ────────────────────────────────────────────────────────
    shops = client.get_shops()
    if not shops:
        print("\n  ❌ No shops on this Printify account. Connect a store first.\n")
        return 0

    forced = os.environ.get("PRINTIFY_SHOP_ID", "").strip()
    if forced:
        shop = next((s for s in shops if str(s["id"]) == forced), None)
        if not shop:
            print(f"\n  ❌ PRINTIFY_SHOP_ID={forced} not found among your shops.\n")
            return 0
    else:
        # Prefer an Etsy-connected shop, else the first one.
        shop = next((s for s in shops if s.get("sales_channel") == "etsy"), shops[0])
    _print_shop(shop)
    shop_id = shop["id"]

    # ── Discover catalog once ─────────────────────────────────────────────────
    catalog = discover_catalog(client)
    _print_price_preview(catalog)
    if not catalog["variants"]:
        print("\n  ❌ No matching variants — nothing to create. Fix CONFIG and retry.\n")
        return 0

    # ── Ledger (dedup) ────────────────────────────────────────────────────────
    ledger = _load_json(LEDGER_FILE, {"products": []})
    already = {p["design"] for p in ledger["products"]}

    created, results, failures = 0, [], []
    for design in designs:
        name = design.name
        print(f"\n  ──────── {name} ────────")
        if name in already:
            print(f"     ⏭️  Already published (in {LEDGER_FILE.name}) — skipping.")
            continue

        try:
            # Compute centered placement at the requested physical size.
            with Image.open(design) as im:
                dw, dh = im.size
            place = compute_placement(catalog, dw, dh)
            pl = place["placement"]
            print(f"     📐 Placement: {dw}x{dh}px → scale {pl['scale']} → "
                  f"{place['display_w_in']:.2f}in x {place['display_h_in']:.2f}in, centered")
            if abs(place["display_w_in"] - DESIGN_WIDTH_IN) > 0.1 or \
               abs(place["display_h_in"] - DESIGN_HEIGHT_IN) > 0.1:
                target_aspect = DESIGN_WIDTH_IN / DESIGN_HEIGHT_IN
                print(f"     ⚠️  Design aspect {place['design_aspect']:.3f} ≠ target "
                      f"{target_aspect:.3f} ({DESIGN_WIDTH_IN}in:{DESIGN_HEIGHT_IN}in). "
                      f"Printify scales uniformly, so the design is FIT inside the box "
                      f"(not stretched). To hit both dimensions exactly, the design file "
                      f"must have that aspect ratio.")

            # AI-generated SEO title/description/tags from the design image
            # (Gemini 2.5 Flash, key rotation; falls back to placeholders).
            title, description, tags, ai_used = generate_listing_content(
                design, TITLE_PLACEHOLDER, DESC_PLACEHOLDER, fallback_tags=TAGS)
            src = "AI-generated" if ai_used else "placeholder (set GEMINI_API_KEY_1..5 for AI)"
            print(f"     📝 Title ({src}): {title[:70]}{'…' if len(title) > 70 else ''}")
            print(f"     🏷️  Tags ({len(tags)}): {', '.join(tags[:6])}{'…' if len(tags) > 6 else ''}")

            if dry_run:
                body = build_product_body(catalog, "<dry-run>", title, description, tags, pl)
                print(f"     🔍 Would upload '{name}', then create product:")
                print(f"        visible={body['visible']}, tags={len(body['tags'])}, "
                      f"variants={len(body['variants'])}, "
                      f"blueprint_id={body['blueprint_id']}, provider_id={body['print_provider_id']}")
                continue

            # 1. Upload
            print(f"     ⬆️  Uploading image…")
            up = client.upload_image(design)
            image_id = up["id"]
            print(f"        ✓ image id {image_id}")

            # 2. Create
            print(f"     🧵 Creating product…")
            body = build_product_body(catalog, image_id, title, description, tags, pl)
            product = client.create_product(shop_id, body)
            product_id = product["id"]
            print(f"        ✓ product id {product_id}")

            # 3. Publish
            print(f"     📢 Publishing…")
            client.publish_product(shop_id, product_id)
            print(f"        ✓ publish accepted (Etsy sync is async — may take a moment)")

            # 4. Record (saved immediately so a later failure never loses this one)
            ledger["products"].append({
                "design": name,
                "product_id": product_id,
                "shop_id": shop_id,
                "published_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            _save_json(LEDGER_FILE, ledger)
            results.append({"design": name, "product_id": product_id})
            created += 1

        except Exception as e:
            # Isolate failures so one bad design doesn't sink the whole batch.
            print(f"     ❌ FAILED: {e}")
            failures.append({"design": name, "error": str(e)})
            continue

    if dry_run:
        print("\n  ✅ Dry run complete — no products created.")
    else:
        print(f"\n  🎉 Printify: {created} product(s) created & published.")
        if failures:
            print(f"  ⚠️  {len(failures)} design(s) failed (not in ledger — re-run to retry):")
            for f in failures:
                print(f"       • {f['design']}: {f['error']}")
        print_manual_checklist(results)

    return created
