"""
printify_service.py — Upload designs to Printify, create & publish products
============================================================================
This is Step 3 of the pipeline (after upscale + mockup). For every design in
`assets/designs_final/`, it:

  1. Uploads the image to Printify        (POST /uploads/images.json)
  2. Discovers the blueprint + provider    (GET  /catalog/...)
  3. Creates the product                    (POST /shops/{id}/products.json)
  4. Publishes it — ONLY with publish=True  (POST .../publish.json)

so it appears under "My Products" in your Printify dashboard.

By default (publish=False) the product is created but NOT published, so it waits
in Printify for you to finalize and click "Publish" yourself — only then does it
sync to Etsy's Drafts tab. Pass publish=True (execute.py --publish) to auto-publish.

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
import io
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from PIL import Image

from services.ai_content import generate_listing_content, SWEATSHIRT_USER_PROMPT
from services.brand_guard import scrub_tags, scrub_text, warn_if_scrubbed

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

# Fallback tags — used only when AI generation fails. Normally each product gets
# design-specific tags from Gemini (see services/ai_content.py).
#
# These target Nullish's audience: people who USE AI daily (students, office
# workers, teachers, everyday AI users), and the person buying them a gift.
# Deliberately NOT the engineer-facing terms the shop used to run on — those are
# in ai_content.BANNED_TAGS and would be stripped anyway. No AI brand names
# (ChatGPT, Claude, …) — trademark risk, see services/brand_guard.py.
TAGS = [
    "funny ai sweatshirt", "ai chatbot shirt", "ai humor gift",
    "college student gift", "grad student gift", "gift for coworker",
    "teacher gift idea", "office humor shirt", "study humor tee",
    "secret santa gift", "back to school gift", "gift for boyfriend",
    "gift for best friend",
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

# ── Sweatshirt artwork box (Gildan 18000 · Printful DTF) ──────────────────────
# Target taken from the manual Printify placement:
#     Width 7.06in · Height 7.06in · Rotate 0° · Position top 0% · left 20.58%
# The DTF print area is 3600x4110px = 12.00in x 13.70in @300 DPI, so:
#     scale = 7.06 / 12.00        = 0.5883   (58.83% of print-area WIDTH)
#     left  = (1 - 0.5883) / 2    = 0.2058   → 20.58%  ✅ horizontally centered
#     y     = (7.06 / 13.70) / 2  = 0.2577   → top edge at 0% of the print area
# Nudge TOP_OFFSET_IN if Printify's editor reports a top % other than 0.
SWEATSHIRT_DESIGN_WIDTH_IN  = 7.06
SWEATSHIRT_DESIGN_HEIGHT_IN = 7.06
SWEATSHIRT_TOP_OFFSET_IN    = 0.0

# ──────────────────────────────────────────────────────────────────────────────

API_BASE      = "https://api.printify.com/v1"
USER_AGENT    = "pod-automation/1.0 (Printify uploader)"   # Printify rejects requests w/o a UA
# All generated local state (catalog caches, ledgers, tokens) lives in one
# gitignored folder so the repo root stays clean.
CACHE_DIR     = Path("caching")
CACHE_FILE    = CACHE_DIR / "printify_cache.json"
LEDGER_FILE   = CACHE_DIR / "published.json"


# ══════════════════════════════════════════════════════════════════════════════
#  Product profiles — one per garment pipeline
# ══════════════════════════════════════════════════════════════════════════════
# All product-specific knobs live in a ProductProfile so the same upload code
# serves both pipelines. execute.py uses TEE_PROFILE (the defaults above);
# sweatshirt.py passes SWEATSHIRT_PROFILE. Each profile has its own catalog
# cache and ledger so tee and sweatshirt runs never collide (the same design
# can exist as both a tee and a sweatshirt product).

@dataclass(frozen=True)
class ProductProfile:
    label: str                       # printed in headers/logs
    blueprint_query: dict            # {"brand": ..., "model": ...}
    provider_name: str
    enabled_colors: tuple[str, ...]
    price_by_size: dict              # size name -> display number (× 100 = cents)
    print_position: str              # placeholder position, e.g. "front" / "front_dtf"
    design_width_in: float
    design_height_in: float
    cache_file: Path
    ledger_file: Path
    vertical_anchor: str = "center"  # "center" = middle of the print area (Printify's
                                     #   default), "top" = flush with the print area's
                                     #   top edge (Printify UI "Position top: 0%")
    top_offset_in: float = 0.0       # only for vertical_anchor="top": push the artwork
                                     #   DOWN from the top edge by this many inches
                                     #   (negative = above the top edge)
    listing_prompt: str | None = None  # None -> ai_content's default (tee) prompt
    description_suffix: str = ""       # static block appended after the AI description
                                       # (e.g. EU GPSR compliance info), "" = none


TEE_PROFILE = ProductProfile(
    label="T-Shirt (Bella+Canvas 3001 · Monster Digital)",
    blueprint_query=BLUEPRINT_QUERY,
    provider_name=PROVIDER_NAME,
    enabled_colors=tuple(ENABLED_COLORS),
    price_by_size=PRICE_BY_SIZE,
    print_position=PRINT_POSITION,
    design_width_in=DESIGN_WIDTH_IN,
    design_height_in=DESIGN_HEIGHT_IN,
    cache_file=CACHE_FILE,
    ledger_file=LEDGER_FILE,
)

# EU GPSR compliance block — appended verbatim to EVERY sweatshirt description,
# separated by a blank line (Etsy/EU General Product Safety Regulation info).
SWEATSHIRT_DESC_SUFFIX = (
    "EU representative: HONSON VENTURES LIMITED, gpsr@honsonventures.com, "
    "3, Gnaftis House flat 102, Limassol, Mesa Geitonia, 4003, CY\n"
    "Product information: Gildan 18000, 2 year warranty in EU and Northern "
    "Ireland as per Directive 1999/44/EC\n"
    "Warnings, Hazard: Made in Nicaragua, For adults\n"
    "Care instructions: Machine wash: cold (max 30C or 90F), Non-chlorine: "
    "bleach as needed, Tumble dry: low heat, , Do not dryclean"
)

# Sweatshirt: Gildan 18000 Unisex Heavy Blend™ Crewneck, fulfilled by Printful,
# DTF decoration (the "front_dtf" placeholder — DTG uses plain "front").
# Printful size gaps (Maroon tops out at 2XL, Sand at 3XL) are skipped
# automatically since only variants that exist in the catalog are enabled.
SWEATSHIRT_PROFILE = ProductProfile(
    label="Sweatshirt (Gildan 18000 · Printful · DTF)",
    blueprint_query={"brand": "Gildan", "model": "18000"},
    provider_name="Printful",
    enabled_colors=("Sport Grey", "Black", "Maroon", "Sand", "Navy", "Dark Chocolate"),
    # Set so EVERY destination clears P500 net, not just the US. The binding
    # constraint is Australia: Printful charges $21.99 to ship there and Etsy
    # takes its 11% on the shipping charge too, so an AU order nets ~P92 less
    # than a US one at the same price. Priced for the US alone (P2,085) an
    # Australian sale would land at P408.
    #   net = 0.89*(price + ship) - P25 - P12.37 - cost - ship
    price_by_size={
        "S":   2190,
        "M":   2190,
        "L":   2190,
        "XL":  2190,
        "2XL": 2340,
        "3XL": 2492,
        "4XL": 2645,
        "5XL": 2796,
    },
    print_position="front_dtf",
    # Sweatshirt artwork is a 7.06in SQUARE pinned to the top of the DTF print
    # area — matches the manual Printify setup (W 7.06in · H 7.06in · 0° ·
    # top 0% · left 20.58% = horizontally centered).
    design_width_in=SWEATSHIRT_DESIGN_WIDTH_IN,
    design_height_in=SWEATSHIRT_DESIGN_HEIGHT_IN,
    vertical_anchor="top",
    top_offset_in=SWEATSHIRT_TOP_OFFSET_IN,
    cache_file=CACHE_DIR / "printify_cache_sweatshirt.json",
    ledger_file=CACHE_DIR / "published_sweatshirt.json",
    listing_prompt=SWEATSHIRT_USER_PROMPT,
    description_suffix=SWEATSHIRT_DESC_SUFFIX,
)


def _finalize_description(description: str, profile: ProductProfile) -> str:
    """Append the profile's static suffix (if any) after a blank line.
    Idempotent: won't duplicate the block if it's already present."""
    description = description.strip()
    suffix = profile.description_suffix.strip()
    if not suffix or suffix in description:
        return description
    return f"{description}\n\n{suffix}"


# ══════════════════════════════════════════════════════════════════════════════
#  Printify API client
# ══════════════════════════════════════════════════════════════════════════════

class PrintifyError(RuntimeError):
    pass


# Printify rejects large upload requests with 413 ("The POST data is too
# large") — the image is sent base64-encoded, inflating it by ~33%. Files
# above this size are downscaled in memory before upload. 4500px covers the
# largest print placeholder we target at 300 DPI, so print quality is
# unaffected (a 7.06in-wide print only needs ~2100px).
MAX_UPLOAD_FILE_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_EDGE_PX    = 4500


def _upload_bytes(path: Path) -> bytes:
    """File bytes to upload; oversized designs are downscaled to fit the API limit."""
    data = path.read_bytes()
    if len(data) <= MAX_UPLOAD_FILE_BYTES:
        return data

    with Image.open(path) as im:
        im.load()
        w, h = im.size
        edge = min(MAX_UPLOAD_EDGE_PX, max(w, h))
        while True:
            scale   = edge / max(w, h)
            resized = im if scale >= 1 else im.resize(
                (max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, "PNG", optimize=True)
            data = buf.getvalue()
            if len(data) <= MAX_UPLOAD_FILE_BYTES or edge <= 1024:
                print(f"        📉 Design too large for the upload API "
                      f"({path.stat().st_size / 1e6:.1f} MB, {w}x{h}px) — "
                      f"downscaled to {resized.size[0]}x{resized.size[1]}px "
                      f"({len(data) / 1e6:.1f} MB). Print quality is unaffected.")
                return data
            edge //= 2


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
        contents = base64.b64encode(_upload_bytes(path)).decode("ascii")
        return self.post("/uploads/images.json", {
            "file_name": path.name,
            "contents": contents,
        })

    # ── Products ───────────────────────────────────────────────────────────
    @staticmethod
    def _without_brands(body: dict) -> dict:
        """Copy of `body` with AI brand names (ChatGPT, Claude, …) replaced by
        "AI" in title/description/tags. Printify syncs these straight to Etsy,
        so this is the last chance to keep a trademark off the listing."""
        body = dict(body)
        for field in ("title", "description"):
            if isinstance(body.get(field), str):
                warn_if_scrubbed(f"Printify {field}", body[field])
                body[field] = scrub_text(body[field])
        if isinstance(body.get("tags"), list):
            warn_if_scrubbed("Printify tags", body["tags"])
            body["tags"] = scrub_tags(body["tags"])
        return body

    def create_product(self, shop_id: int, body: dict) -> dict:
        return self.post(f"/shops/{shop_id}/products.json", self._without_brands(body))

    def get_product(self, shop_id: int, product_id: str) -> dict:
        return self.get(f"/shops/{shop_id}/products/{product_id}.json")

    def update_product(self, shop_id: int, product_id: str, body: dict) -> dict:
        return self.put(f"/shops/{shop_id}/products/{product_id}.json", self._without_brands(body))

    def product_exists(self, shop_id: int, product_id: str) -> bool:
        try:
            self.get_product(shop_id, product_id)
            return True
        except PrintifyError as e:
            if "404" in str(e) or "not_found" in str(e).lower():
                return False
            raise

    def publish_product(self, shop_id: int, product_id: str,
                        images: bool = True) -> dict:
        """Push the product to the connected store (Etsy).

        The booleans map 1:1 to the checkboxes in Printify's publish dialog.
        `images` is the one that matters after the first publish: Printify treats
        its OWN generated mockups as the source of truth for images, so a
        republish with images=True silently reasserts them over the custom
        mockups marketing_sweatshirt.py uploaded straight to Etsy.

        Keep images=True on the FIRST publish — Etsy needs at least one image to
        create the listing, and Printify's mockups are the only ones that exist
        at that moment. Use republish_product() for every publish after that.
        """
        return self.post(f"/shops/{shop_id}/products/{product_id}/publish.json", {
            "title": True, "description": True, "images": images,
            "variants": True, "tags": True, "keyFeatures": True,
            "shipping_template": True,
        })

    def republish_product(self, shop_id: int, product_id: str) -> dict:
        """Re-sync a product to Etsy WITHOUT touching the listing's images.

        This is what you want after fixing print positioning in Printify: the
        corrected product data reaches Etsy, and the mockups we uploaded there
        survive. Without it, a positioning fix costs you your mockups on a live
        listing that customers are looking at.
        """
        return self.publish_product(shop_id, product_id, images=False)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
#  Catalog discovery (cached — catalog rarely changes, 100 req/min limit)
# ══════════════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def discover_catalog(client: PrintifyClient, profile: ProductProfile = TEE_PROFILE) -> dict:
    """Resolve blueprint_id, provider_id, and the enabled variant set.

    Returns a dict: {blueprint_id, provider_id, variant_ids (sorted),
                     variants: [{id, color, size, price_cents}], missing: [...]}.
    Cached to the profile's cache file keyed by the config so changes invalidate it.
    """
    cache_key = _norm(json.dumps([profile.blueprint_query, profile.provider_name,
                                  profile.print_position,
                                  sorted(profile.enabled_colors),
                                  sorted(profile.price_by_size)]))
    cache = _load_json(profile.cache_file, {})
    if cache.get("key") == cache_key and "front_width_px" in cache.get("data", {}):
        print(f"  📦 Using cached catalog discovery (delete {profile.cache_file} to refresh).")
        return cache["data"]

    # 1) Blueprint
    print(f"  🔎 Finding blueprint  brand='{profile.blueprint_query['brand']}' "
          f"model='{profile.blueprint_query['model']}'…")
    blueprint = None
    for bp in client.get_blueprints():
        if (_norm(bp.get("brand", "")) == _norm(profile.blueprint_query["brand"])
                and _norm(str(bp.get("model", ""))) == _norm(str(profile.blueprint_query["model"]))):
            blueprint = bp
            break
    if not blueprint:
        raise PrintifyError(
            f"Blueprint not found for {profile.blueprint_query}. "
            "Check the brand/model strings in the ProductProfile."
        )
    bp_id = blueprint["id"]
    print(f"     ✓ blueprint #{bp_id}: {blueprint.get('title')}")

    # 2) Provider
    print(f"  🔎 Finding provider '{profile.provider_name}'…")
    provider = None
    providers = client.get_providers(bp_id)
    for p in providers:
        if _norm(p.get("title", "")) == _norm(profile.provider_name):
            provider = p
            break
    if not provider:
        names = ", ".join(p.get("title", "?") for p in providers)
        raise PrintifyError(
            f"Provider '{profile.provider_name}' not available for blueprint #{bp_id}. "
            f"Available: {names}"
        )
    pp_id = provider["id"]
    print(f"     ✓ provider #{pp_id}: {provider.get('title')}")

    # 3) Variants → filter to enabled colors × priced sizes
    print(f"  🔎 Resolving variants ({len(profile.enabled_colors)} colors × "
          f"{len(profile.price_by_size)} sizes)…")
    raw = client.get_variants(bp_id, pp_id)
    all_variants = raw.get("variants", raw if isinstance(raw, list) else [])

    # Print-area dimensions (px) for the profile's placeholder position —
    # used to convert inches -> scale. The position also selects the
    # decoration method (e.g. "front" = DTG, "front_dtf" = DTF).
    front_w_px = front_h_px = None
    for v in all_variants:
        for ph in v.get("placeholders", []):
            if ph.get("position") == profile.print_position:
                front_w_px, front_h_px = ph["width"], ph["height"]
                break
        if front_w_px:
            break
    if not front_w_px:
        raise PrintifyError(
            f"No '{profile.print_position}' print placeholder found for "
            f"blueprint #{bp_id} / provider #{pp_id}."
        )
    print(f"     ✓ {profile.print_position} print area: {front_w_px}x{front_h_px}px "
          f"= {front_w_px/PRINT_DPI:.2f}in x {front_h_px/PRINT_DPI:.2f}in @ {PRINT_DPI} DPI")

    wanted_colors = {_norm(c) for c in profile.enabled_colors}
    wanted_sizes  = {_norm(s) for s in profile.price_by_size}

    chosen, found_colors, found_sizes = [], set(), set()
    for v in all_variants:
        opts  = v.get("options", {})
        color = opts.get("color", "")
        size  = opts.get("size", "")
        if _norm(color) in wanted_colors and _norm(size) in wanted_sizes:
            found_colors.add(_norm(color))
            found_sizes.add(_norm(size))
            # map normalized size back to the price_by_size key for the price
            price_key = next(k for k in profile.price_by_size if _norm(k) == _norm(size))
            chosen.append({
                "id": v["id"],
                "color": color,
                "size": size,
                "price_cents": profile.price_by_size[price_key] * 100,  # display number × 100 (cents)
            })

    missing = []
    for c in profile.enabled_colors:
        if _norm(c) not in found_colors:
            missing.append(f"color '{c}'")
    for s in profile.price_by_size:
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
    _save_json(profile.cache_file, {"key": cache_key, "data": data})
    return data


# ══════════════════════════════════════════════════════════════════════════════
#  Product payload
# ══════════════════════════════════════════════════════════════════════════════

def compute_placement(catalog: dict, design_w_px: int, design_h_px: int,
                      profile: ProductProfile = TEE_PROFILE) -> dict:
    """Scale the design to fit inside the profile's design_width_in x
    design_height_in box (aspect preserved) and position it in the print area.
    Returns {placement, display_w_in, display_h_in, design_aspect, top_pct, left_pct}.

    Printify's API takes the image CENTER as (x, y) — both fractions of the print
    area — plus `scale` = fraction of print-area WIDTH. The design is "contained"
    in the target box, so width is limited by both the target width and
    (target height x aspect).

    Horizontally always centered (x = 0.5). Vertically:
      • vertical_anchor="center" → y = 0.5 (Printify's default)
      • vertical_anchor="top"    → the artwork's TOP edge sits top_offset_in
        below the print area's top edge, i.e. the editor's "Position top" field.

    top_pct/left_pct are the same numbers Printify's editor shows, so the log can
    be compared 1:1 with the dashboard.
    """
    pa_w_in = catalog["front_width_px"] / PRINT_DPI
    pa_h_in = catalog["front_height_px"] / PRINT_DPI
    design_aspect = design_w_px / design_h_px                       # w / h
    display_w_in = min(profile.design_width_in, profile.design_height_in * design_aspect)
    display_h_in = display_w_in / design_aspect
    scale = display_w_in / pa_w_in

    h_frac = display_h_in / pa_h_in                                 # of print-area height
    if profile.vertical_anchor == "top":
        y = (profile.top_offset_in + display_h_in / 2) / pa_h_in
    else:
        y = 0.5

    return {
        "placement": {"x": 0.5, "y": round(y, 4), "scale": round(scale, 4), "angle": 0},
        "display_w_in": display_w_in,
        "display_h_in": display_h_in,
        "design_aspect": design_aspect,
        "top_pct": (y - h_frac / 2) * 100,       # editor "Position top"
        "left_pct": (0.5 - scale / 2) * 100,     # editor "Position left"
    }


def build_product_body(catalog: dict, image_id: str, title: str,
                       description: str, tags: list[str], placement: dict,
                       profile: ProductProfile = TEE_PROFILE,
                       visible: bool | None = None) -> dict:
    """Product creation payload.

    `visible` overrides the HIDE_IN_STORE default; None keeps it.

    NOTE: visible=False does NOT block publishing to Etsy — every product in this
    shop that published successfully has visible=False. Don't "fix" a failing
    publish by flipping this.
    """
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
        "visible": (not HIDE_IN_STORE) if visible is None else visible,
        "variants": variants,
        "print_areas": [
            {
                "variant_ids": catalog["variant_ids"],
                "placeholders": [
                    {
                        "position": profile.print_position,
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
        print(f"      → Fix the names in printify_service.py CONFIG and delete {profile.cache_file}.")


def print_manual_checklist(results: list[dict], publish: bool = False):
    print("\n" + "═" * 56)
    print("  📋 MANUAL STEPS — not possible via the Printify API")
    print("═" * 56)
    if publish:
        print("  Do these in the Printify/Etsy dashboard for each product:")
    else:
        print("  Finalize each product in Printify, THEN click \"Publish\" to send it")
        print("  to Etsy's Drafts tab:")
    print("    1. Set the mockup to 'Hanging 1' (FRONT side only, no back).")
    print("    2. Toggle ON 'Free shipping'.")
    print("    3. Toggle ON 'Etsy off-site ads'.")
    print("    4. Verify the currency & prices (these are NOT in PHP — Printify")
    print("       supports USD/EUR/GBP/CAD/AUD only).")
    print("    5. Review the title, description & tags (Gemini-generated if any")
    print("       GEMINI_API_KEY_1..5 is set; otherwise placeholder).")
    if not publish:
        print("    6. Center/adjust the design, then click 'Publish' in Printify.")
    if results:
        print("\n  Products created this run:")
        for r in results:
            print(f"    • {r['design']:<28} → product {r['product_id']}")


# ══════════════════════════════════════════════════════════════════════════════
#  Ledger maintenance (caching/published.json)
# ══════════════════════════════════════════════════════════════════════════════

def reconcile_ledger(profile: ProductProfile = TEE_PROFILE) -> int:
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

    ledger = _load_json(profile.ledger_file, {"products": []})
    entries = ledger.get("products", [])
    if not entries:
        print(f"  ℹ️  {profile.ledger_file.name} is empty — nothing to reconcile.\n")
        return 0

    client = PrintifyClient(token)
    kept, removed = [], []
    for p in entries:
        exists = client.product_exists(p["shop_id"], p["product_id"])
        (kept if exists else removed).append(p)
        mark = "✓ exists" if exists else "✗ gone — forgetting"
        print(f"     {mark}: {p['design']}  ({p['product_id']})")

    ledger["products"] = kept
    _save_json(profile.ledger_file, ledger)
    print(f"\n  🧹 Removed {len(removed)} stale entr{'y' if len(removed)==1 else 'ies'}; "
          f"{len(kept)} still live. Forgotten designs will be re-created next run.\n")
    return len(removed)


def update_listings(designs_dir: Path = Path("assets/designs_final"),
                    profile: ProductProfile = TEE_PROFILE) -> int:
    """Regenerate title/description/tags for every product already in the ledger.

    For each entry in the ledger: look at the design image with Gemini,
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

    entries = _load_json(profile.ledger_file, {"products": []}).get("products", [])
    if not entries:
        print(f"  ℹ️  {profile.ledger_file.name} is empty — nothing to update.\n")
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
                design, TITLE_PLACEHOLDER, DESC_PLACEHOLDER, fallback_tags=TAGS,
                user_prompt=profile.listing_prompt)
            description = _finalize_description(description, profile)
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


def clear_ledger(profile: ProductProfile = TEE_PROFILE) -> int:
    """Wipe the entire ledger — every design will be (re)created on the next run."""
    n = len(_load_json(profile.ledger_file, {"products": []}).get("products", []))
    _save_json(profile.ledger_file, {"products": []})
    print(f"🧹 Cleared {n} entr{'y' if n==1 else 'ies'} from {profile.ledger_file.name}. "
          f"All designs in 'assets/designs_final/' will be (re)created on the next run.")
    return n


def forget_designs(names: list[str], profile: ProductProfile = TEE_PROFILE) -> int:
    """Remove specific designs from the ledger by file name (no Printify call)."""
    ledger = _load_json(profile.ledger_file, {"products": []})
    wanted = {n.lower() for n in names}
    before = ledger.get("products", [])
    kept = [p for p in before if p["design"].lower() not in wanted]
    ledger["products"] = kept
    _save_json(profile.ledger_file, ledger)
    removed = len(before) - len(kept)
    print(f"🧹 Forgot {removed} design(s) from {profile.ledger_file.name}: {', '.join(names)}")
    return removed


# ══════════════════════════════════════════════════════════════════════════════
#  Orchestration — called from execute.py
# ══════════════════════════════════════════════════════════════════════════════

def run_printify_upload(designs: list[Path], dry_run: bool = False,
                        publish: bool = False,
                        profile: ProductProfile = TEE_PROFILE) -> int:
    """Upload + create each design on Printify. Returns # of products created.

    By default the product is created but NOT published to Etsy — it sits in your
    Printify "My Products" so you can finalize it (centering, mockups, etc.) and
    click "Publish" yourself. Pass publish=True to also push it to Etsy's Drafts
    tab automatically.

    `profile` selects the garment (TEE_PROFILE / SWEATSHIRT_PROFILE): blueprint,
    provider, colors, prices, print placeholder, and the per-garment ledger.

    Skips designs already recorded in the profile's ledger (dedup across runs).
    With dry_run=True: does discovery + prints the payload/preview, creates nothing.
    """
    print("\n" + "═" * 56)
    print(f"  🚀 Printify upload — {profile.label}"
          + ("  (DRY RUN — nothing will be created)" if dry_run else ""))
    print("═" * 56)
    if not dry_run and not publish:
        print("  ℹ️  Create-only mode: products stay in Printify (NOT published to Etsy).")
        print("      Finalize each in Printify, then click \"Publish\" yourself.")
        print("      (Pass --publish to auto-publish to Etsy's Drafts tab.)")

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
    catalog = discover_catalog(client, profile)
    _print_price_preview(catalog)
    if not catalog["variants"]:
        print("\n  ❌ No matching variants — nothing to create. Fix the ProductProfile and retry.\n")
        return 0

    # ── Ledger (dedup) ────────────────────────────────────────────────────────
    ledger = _load_json(profile.ledger_file, {"products": []})
    already = {p["design"] for p in ledger["products"]}

    created, results, failures = 0, [], []
    for design in designs:
        name = design.name
        print(f"\n  ──────── {name} ────────")
        if name in already:
            print(f"     ⏭️  Already published (in {profile.ledger_file.name}) — skipping.")
            continue

        try:
            # Compute centered placement at the requested physical size.
            with Image.open(design) as im:
                dw, dh = im.size
            place = compute_placement(catalog, dw, dh, profile)
            pl = place["placement"]
            anchor = ("top-anchored" if profile.vertical_anchor == "top"
                      else "centered")
            print(f"     📐 Placement: {dw}x{dh}px → scale {pl['scale']} → "
                  f"{place['display_w_in']:.2f}in x {place['display_h_in']:.2f}in, {anchor}")
            print(f"        Printify editor should read: W {place['display_w_in']:.2f}in · "
                  f"H {place['display_h_in']:.2f}in · rotate {pl['angle']}° · "
                  f"top {place['top_pct']:.2f}% · left {place['left_pct']:.2f}%")
            if abs(place["display_w_in"] - profile.design_width_in) > 0.1 or \
               abs(place["display_h_in"] - profile.design_height_in) > 0.1:
                target_aspect = profile.design_width_in / profile.design_height_in
                print(f"     ⚠️  Design aspect {place['design_aspect']:.3f} ≠ target "
                      f"{target_aspect:.3f} ({profile.design_width_in}in:{profile.design_height_in}in). "
                      f"Printify scales uniformly, so the design is FIT inside the box "
                      f"(not stretched). To hit both dimensions exactly, the design file "
                      f"must have that aspect ratio.")

            # AI-generated SEO title/description/tags from the design image
            # (Gemini 2.5 Flash, key rotation; falls back to placeholders).
            title, description, tags, ai_used = generate_listing_content(
                design, TITLE_PLACEHOLDER, DESC_PLACEHOLDER, fallback_tags=TAGS,
                user_prompt=profile.listing_prompt)
            description = _finalize_description(description, profile)
            src = "AI-generated" if ai_used else "placeholder (set GEMINI_API_KEY_1..5 for AI)"
            print(f"     📝 Title ({src}): {title[:70]}{'…' if len(title) > 70 else ''}")
            print(f"     🏷️  Tags ({len(tags)}): {', '.join(tags[:6])}{'…' if len(tags) > 6 else ''}")

            # Hard stop: never create a Printify product with placeholder SEO.
            # Not added to the ledger, so simply re-running retries this design.
            if not ai_used and not dry_run:
                print(f"     🛑 No AI title/description/tags — NOT creating this product. "
                      f"Re-run later to retry.")
                failures.append({"design": name,
                                 "error": "AI listing content failed (placeholder SEO)"})
                continue

            if dry_run:
                body = build_product_body(catalog, "<dry-run>", title, description, tags, pl, profile)
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
            body = build_product_body(catalog, image_id, title, description, tags, pl, profile)
            product = client.create_product(shop_id, body)
            product_id = product["id"]
            print(f"        ✓ product id {product_id}")

            # 3. Publish (only when explicitly requested — otherwise the product
            #    waits in Printify for you to finalize and publish manually).
            if publish:
                print(f"     📢 Publishing…")
                client.publish_product(shop_id, product_id)
                print(f"        ✓ publish accepted (Etsy sync is async — may take a moment)")
            else:
                print(f"     ⏸️  Created, NOT published — finalize in Printify, then publish there.")

            # 4. Record (saved immediately so a later failure never loses this one)
            ledger["products"].append({
                "design": name,
                "product_id": product_id,
                "shop_id": shop_id,
                "published": publish,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                # Recorded for sweatshirt_automation.py: when Gemini falls back to
                # placeholder copy this is False, and the orchestrator refuses to
                # take that listing live rather than publishing lorem-ipsum.
                "ai_used": ai_used,
                # A local copy of the tags we sent, so tag alignment survives even
                # if the Printify product is later deleted.
                "tags": tags,
            })
            _save_json(profile.ledger_file, ledger)
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
        if publish:
            print(f"\n  🎉 Printify: {created} product(s) created & published to Etsy.")
        else:
            print(f"\n  🎉 Printify: {created} product(s) created (NOT published — "
                  f"publish them in Printify when ready).")
        if failures:
            print(f"  ⚠️  {len(failures)} design(s) failed (not in ledger — re-run to retry):")
            for f in failures:
                print(f"       • {f['design']}: {f['error']}")
        print_manual_checklist(results, publish=publish)

    return created
