"""
etsy_service.py — Etsy Open API v3 client (listings & images)
=============================================================
Used by marketing_sweatshirt.py to swap an Etsy draft listing's images for
our own mockups. Auth is OAuth 2.0 user tokens obtained ONCE via
`python -m scripts.auth.etsy_auth` (see that file); this module then self-manages them:
every EtsyClient() construction trades the stored refresh token for a fresh
1-hour access token and saves the rotated refresh token back, rolling the
~90-day refresh expiry forward on every run.

Endpoints wrapped here:
  GET    /application/listings/{listing_id}                      state check
  PATCH  /application/shops/{s}/listings/{l}                     auto-renew toggle
  GET    /application/listings/{listing_id}/images               current images
  POST   /application/shops/{s}/listings/{l}/images              upload (rank 1 = thumbnail)
  DELETE /application/shops/{s}/listings/{l}/images/{image_id}   remove old mockups

Tokens live in caching/.etsy_tokens.json (gitignored) and are never printed.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import time
from pathlib import Path

try:
    from PIL import Image
except ImportError:                              # uploads still work, just fatter
    Image = None

import requests
import socket
import urllib3.util.connection

from services.brand_guard import scrub_tags, scrub_text, warn_if_scrubbed

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ── Why this module forces IPv4 ───────────────────────────────────────────────
# api.etsy.com publishes NO AAAA record. On networks whose resolver does DNS64
# (many home routers, mobile hotspots and VPNs), the resolver *synthesises* one
# from the NAT64 well-known prefix — api.etsy.com then "resolves" to
# 64:ff9b::924b:15e0, which is just 146.75.21.224 in hex. If that network has no
# working NAT64 gateway, the address is a black hole: requests/urllib3 has no
# Happy Eyeballs, so it tries the IPv6 candidate first and blocks for the full
# ~21 s TCP timeout before falling back to IPv4 (which connects in ~3 ms).
#
# That toll is paid on every NEW connection, so a run that makes ~60 Etsy calls
# burned ~21 minutes doing nothing. Pinning the address family to IPv4 skips the
# dead candidate entirely. Etsy is IPv4-only anyway, so nothing is lost.
#
# NOTE: urllib3 exposes this as one process-wide hook, so importing this module
# also pins Printify (and anything else using requests) to IPv4. Both APIs are
# reachable over IPv4, so this is safe. Set POD_ALLOW_IPV6=1 to opt out.
if os.environ.get("POD_ALLOW_IPV6", "").strip() not in ("1", "true", "True"):
    urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET


API_BASE   = "https://api.etsy.com/v3"
TOKEN_URL  = "https://api.etsy.com/v3/public/oauth/token"
# Local state (tokens) lives alongside the Printify caches/ledgers in caching/.
CACHE_DIR  = Path("caching")
TOKEN_FILE = CACHE_DIR / ".etsy_tokens.json"
USER_AGENT = "pod-automation/1.0 (Etsy marketing)"

# Etsy listing-title constraints, enforced client-side so a bad title fails here
# with a readable message instead of as an opaque 400 from the API.
ETSY_TITLE_MAX = 140
ETSY_TITLE_BANNED_CHARS = set('$%^*<>|";')
ETSY_SECTION_TITLE_MAX = 24

# Our renders are ~2.4 MB PNGs; 24 of them is ~52 MB pushed up a home upstream
# every full run. Etsy re-encodes everything it receives anyway, so shipping PNG
# only buys us upload time. Re-encoding to high-quality JPEG first cuts the
# payload ~70% with no visible difference in the listing.
# subsampling=0 (4:4:4) is deliberate: these designs are TEXT on fabric, and
# chroma subsampling is exactly what smears coloured text edges. Set
# UPLOAD_AS_JPEG = False to go back to uploading the files byte-for-byte.
UPLOAD_AS_JPEG = True
JPEG_QUALITY   = 95


class EtsyError(RuntimeError):
    """Any Etsy API failure."""


class EtsyAuthError(EtsyError):
    """Credentials/token problem — the fix is always `python -m scripts.auth.etsy_auth`."""


def _upload_payload(path: Path) -> tuple[str, bytes, str]:
    """(filename, bytes, mime) to POST for a local mockup.

    Re-encodes to JPEG when that is both possible and actually smaller; falls
    back to the raw file on any doubt. Images carrying real transparency are
    never converted — flattening them would change how the listing looks.
    """
    raw = path.read_bytes()
    fallback = (path.name, raw, mimetypes.guess_type(path.name)[0] or "image/png")
    if not UPLOAD_AS_JPEG or Image is None:
        return fallback
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.mode in ("RGBA", "LA", "P") and "transparency" in im.info:
                return fallback
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY,
                                   optimize=True, subsampling=0)
    except Exception:                            # unreadable/exotic → send as-is
        return fallback
    if buf.tell() >= len(raw):                   # already well compressed
        return fallback
    return (f"{path.stem}.jpg", buf.getvalue(), "image/jpeg")


class EtsyClient:
    """Authenticated Etsy v3 client. Constructing it refreshes the token."""

    def __init__(self):
        # One pooled session for the whole run (mirrors PrintifyClient). Without
        # it every call re-does DNS + TCP + the TLS handshake against Etsy —
        # ~250 ms of pure overhead per request on a good network, and far worse
        # on a broken one (see the IPv4 note at the top of this module).
        self.session = requests.Session()
        self.keystring = os.environ.get("ETSY_KEYSTRING", "").strip()
        if not self.keystring:
            raise EtsyAuthError("ETSY_KEYSTRING is not set in .env.")
        # Since Feb 9 2026 Etsy requires the shared secret in every request:
        # the x-api-key header must be "keystring:sharedsecret".
        # https://github.com/etsy/open-api/discussions/1529
        self.shared_secret = os.environ.get("ETSY_SHARED_SECRET", "").strip()
        if not self.shared_secret:
            raise EtsyAuthError(
                "ETSY_SHARED_SECRET is not set in .env — Etsy requires it in every\n"
                "           API request since Feb 9, 2026. Copy it from\n"
                "           https://www.etsy.com/developers/your-apps (same page as the keystring).")
        if not TOKEN_FILE.exists():
            raise EtsyAuthError(
                f"{TOKEN_FILE} not found — run the one-time authorization first:\n"
                f"           python -m scripts.auth.etsy_auth")
        try:
            self.tokens = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise EtsyAuthError(f"{TOKEN_FILE} is unreadable ({e}) — re-run: python -m scripts.auth.etsy_auth")
        self._refresh()

    # ── Token lifecycle ─────────────────────────────────────────────────────
    def _refresh(self):
        """Always refresh on start: keeps the access token fresh for the whole
        run AND rotates the refresh token (rolling the 90-day expiry forward)."""
        resp = self.session.post(TOKEN_URL, json={
            "grant_type": "refresh_token",
            "client_id": self.keystring,
            "refresh_token": self.tokens.get("refresh_token", ""),
        }, timeout=30)
        if not resp.ok:
            raise EtsyAuthError(
                f"Etsy token refresh failed ({resp.status_code}: {resp.text[:200]}).\n"
                f"           The refresh token has likely expired (~90 days unused) or was\n"
                f"           revoked — re-authorize with:  python -m scripts.auth.etsy_auth")
        tok = resp.json()
        self.tokens["access_token"]  = tok["access_token"]
        self.tokens["refresh_token"] = tok.get("refresh_token", self.tokens["refresh_token"])
        self.tokens["expires_at"]    = time.time() + int(tok.get("expires_in", 3600))
        self._save_tokens()

    def _save_tokens(self):
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(self.tokens, indent=2), encoding="utf-8")

    def _headers(self) -> dict:
        return {
            "x-api-key": f"{self.keystring}:{self.shared_secret}",
            "Authorization": f"Bearer {self.tokens['access_token']}",
            "User-Agent": USER_AGENT,
        }

    # ── Core request with retry (mirrors PrintifyClient conventions) ────────
    def _request(self, method: str, path: str, **kwargs):
        url = f"{API_BASE}/{path.lstrip('/')}"
        last = "no response"
        for attempt in range(4):
            try:
                resp = self.session.request(method, url, headers=self._headers(),
                                            timeout=60, **kwargs)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as e:
                # A dropped TCP connection carries no status code, so none of the
                # checks below ever see it and it escapes the loop. Left unhandled
                # it aborts a whole batch mid-run — which is exactly what happened
                # three listings into a redescribe batch of 15.
                wait = 2 ** attempt
                last = type(e).__name__
                print(f"     ⚠️  Etsy connection error ({last}). Retrying in {wait}s…")
                time.sleep(wait)
                continue
            last = f"status {resp.status_code}"
            if resp.status_code == 401:
                raise EtsyAuthError(
                    "401 Unauthorized from Etsy — token invalid or missing scopes "
                    "(listings_r/listings_w/listings_d/shops_r/shops_w). "
                    "Re-run: python -m scripts.auth.etsy_auth")
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2 ** attempt) or 2 ** attempt)
                print(f"     ⏳ Etsy rate limit (429). Waiting {wait}s…")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = 2 ** attempt
                print(f"     ⚠️  Etsy server {resp.status_code}. Retrying in {wait}s…")
                time.sleep(wait)
                continue
            # Not a client mistake: after deleting images Etsy renumbers the
            # remaining ranks asynchronously, and anything uploaded before that
            # settles is rejected with this 400. It clears on its own — wait.
            if resp.status_code == 400 and "inconsistent order" in resp.text:
                wait = 3 * (attempt + 1)
                print(f"     ⏳ Etsy image ranks still settling. Retrying in {wait}s…")
                time.sleep(wait)
                continue
            if not resp.ok:
                raise EtsyError(f"{method} {path} → {resp.status_code}: {resp.text[:500]}")
            return resp.json() if resp.text else {}
        raise EtsyError(f"{method} {path} failed after retries (last: {last}).")

    # ── Shop id ─────────────────────────────────────────────────────────────
    def get_shop_id(self) -> int:
        """The token owner's Etsy shop id (cached in the token file)."""
        if self.tokens.get("shop_id"):
            return int(self.tokens["shop_id"])
        me = self._request("GET", "/application/users/me")
        self.tokens["user_id"] = me.get("user_id")
        self.tokens["shop_id"] = me.get("shop_id")
        self._save_tokens()
        if not self.tokens.get("shop_id"):
            raise EtsyError("Etsy /users/me returned no shop_id — is this account a seller?")
        return int(self.tokens["shop_id"])

    # ── Listings ────────────────────────────────────────────────────────────
    def get_listing(self, listing_id: int) -> dict:
        """Full listing record — includes `state` ('draft'/'active'/…) and `shop_id`."""
        return self._request("GET", f"/application/listings/{listing_id}")

    def get_active_listings(self, shop_id: int) -> list[dict]:
        """Every active listing in the shop. Etsy pages at 100 max per call."""
        out, offset = [], 0
        while True:
            r = self._request("GET", f"/application/shops/{shop_id}/listings/active",
                              params={"limit": 100, "offset": offset})
            page = r.get("results", []) or []
            out.extend(page)
            offset += len(page)
            if not page or offset >= int(r.get("count", 0)):
                return out

    def listing_exists(self, listing_id: int) -> bool:
        try:
            self.get_listing(listing_id)
            return True
        except EtsyError as e:
            if isinstance(e, EtsyAuthError):
                raise
            if "404" in str(e):
                return False
            raise

    def set_auto_renew(self, shop_id: int, listing_id: int, enabled: bool = True) -> dict:
        """Flip the listing's renewal option to Automatic (or back to Manual).

        `should_auto_renew` is Etsy's name for the "Automatic"/"Manual" radio in
        the listing's Renewal options — when true Etsy re-lists the item for
        another four months on expiry (and charges the $0.20 listing fee again).

        updateListing is form-encoded, NOT JSON, and PATCHing a single field
        leaves everything else — including `state` — untouched, so a draft
        stays a draft.
        """
        return self._request(
            "PATCH",
            f"/application/shops/{shop_id}/listings/{listing_id}",
            data={"should_auto_renew": "true" if enabled else "false"},
        )

    def activate_listing(self, shop_id: int, listing_id: int) -> dict:
        """Take a draft listing LIVE.

        Printify drops products into Etsy's Drafts tab and leaves them there
        forever — nothing publishes them but this call (or your click).

        ⚠️  COSTS MONEY: Etsy charges the $0.20 listing fee the moment a listing
        goes active, and again on every four-month auto-renew. Callers must have
        a reason to believe the listing is correct before calling this.

        Etsy requires the listing to already carry at least one image and all its
        required fields, so this must run AFTER the mockup swap — otherwise the
        listing goes live wearing Printify's default mockups.
        """
        return self._request(
            "PATCH",
            f"/application/shops/{shop_id}/listings/{listing_id}",
            data={"state": "active"},
        )

    def get_listing_tags(self, listing_id: int) -> list[str]:
        """The listing's current tags, lowercased for comparison."""
        return [str(t).strip().lower()
                for t in (self.get_listing(listing_id).get("tags") or [])]

    def update_listing_tags(self, shop_id: int, listing_id: int,
                            tags: list[str]) -> dict:
        """Overwrite the listing's tags.

        Used to repair Printify→Etsy tag sync, which is gated by shop-level
        publishing settings and cannot be relied on in an unattended run. The
        tags passed here come from the Printify product itself, never from a
        fresh Gemini call — regenerating would produce a DIFFERENT set and make
        the two platforms drift, which is the exact failure this prevents.

        updateListing is form-encoded and takes tags as one comma-separated
        string. Etsy's limits (13 tags, 20 chars each) are already enforced
        upstream by _sanitize_tags() in services/ai_content.py.

        AI brand names (ChatGPT, Claude, …) are replaced with "ai" on the way
        out — see services/brand_guard.py.
        """
        warn_if_scrubbed(f"listing {listing_id} tags", list(tags))
        tags = scrub_tags(tags)
        return self._request(
            "PATCH",
            f"/application/shops/{shop_id}/listings/{listing_id}",
            data={"tags": ",".join(tags)},
        )

    def update_listing_title(self, shop_id: int, listing_id: int,
                             title: str) -> dict:
        """Overwrite the listing's title.

        Etsy caps titles at 140 characters and rejects a few characters outright
        (`$ % ^ * < > | " ;`) — note that PIPE is on that list, which is why the
        old `A | B | C` titles cannot simply be re-saved as-is once touched.

        Read-side gotcha worth knowing before you diff a title you just wrote:
        getListing returns the title HTML-ESCAPED, so an apostrophe comes back as
        `&#39;` and inflates the string by 4 characters. Compare with
        html.unescape() or you will "find" phantom length violations.

        Like the other updateListing calls this is form-encoded and patches one
        field, leaving state, price, images and tags untouched.

        AI brand names (ChatGPT, Claude, …) are replaced with "AI" on the way
        out — see services/brand_guard.py.
        """
        title = " ".join(str(title).split())
        warn_if_scrubbed(f"listing {listing_id} title", title)
        title = scrub_text(title)
        if not title:
            raise EtsyError("Refusing to write an empty title.")
        if len(title) > ETSY_TITLE_MAX:
            raise EtsyError(
                f"Title is {len(title)} chars, Etsy's limit is {ETSY_TITLE_MAX}: {title!r}")
        bad = sorted(set(title) & set(ETSY_TITLE_BANNED_CHARS))
        if bad:
            raise EtsyError(
                f"Title contains characters Etsy rejects ({' '.join(bad)}): {title!r}")
        return self._request(
            "PATCH",
            f"/application/shops/{shop_id}/listings/{listing_id}",
            data={"title": title},
        )

    # ── Shop sections ───────────────────────────────────────────────────────
    def update_listing_description(self, shop_id: int, listing_id: int,
                                   description: str) -> dict:
        """Overwrite the listing's description.

        Same one-field PATCH as the title and tag calls, so state, price, images
        and tags are left untouched.

        Etsy renders the description as PLAIN TEXT — newlines are preserved, but
        markdown and HTML are not, so "**bold**" ships literally as asterisks.
        _strip_markdown() in services/ai_content.py already handles the markers
        the model slips in; nothing else should be sent.

        Read-side gotcha, same as the title: getListing returns the description
        HTML-ESCAPED, so an apostrophe comes back as `&#39;`. Compare with
        html.unescape() before deciding a listing still needs rewriting, or
        every listing looks different from what you just wrote.

        AI brand names (ChatGPT, Claude, …) are replaced with "AI" on the way
        out — see services/brand_guard.py.
        """
        description = (description or "").strip()
        warn_if_scrubbed(f"listing {listing_id} description", description)
        description = scrub_text(description)
        if not description:
            raise EtsyError("Refusing to write an empty description.")
        return self._request(
            "PATCH",
            f"/application/shops/{shop_id}/listings/{listing_id}",
            data={"description": description},
        )

    def get_shop_sections(self, shop_id: int) -> list[dict]:
        """Existing sections, each with `shop_section_id` and `title`."""
        r = self._request("GET", f"/application/shops/{shop_id}/sections")
        return r.get("results", []) or []

    def create_shop_section(self, shop_id: int, title: str) -> dict:
        """Create one shop section and return it (carries `shop_section_id`).

        Etsy caps section titles at 24 characters — shorter than you would guess,
        and the API rejects an over-long title with a bare 400. Checked here so
        the failure names the problem.

        Etsy does NOT dedupe by title: calling this twice makes two sections with
        the same name and no way to tell them apart in the UI. Callers must look
        the title up in get_shop_sections() first — section.py does.
        """
        title = " ".join(str(title).split())
        if not title:
            raise EtsyError("Refusing to create a section with an empty title.")
        if len(title) > ETSY_SECTION_TITLE_MAX:
            raise EtsyError(
                f"Section title is {len(title)} chars, Etsy's limit is "
                f"{ETSY_SECTION_TITLE_MAX}: {title!r}")
        return self._request(
            "POST", f"/application/shops/{shop_id}/sections",
            data={"title": title},
        )

    def set_listing_section(self, shop_id: int, listing_id: int,
                            section_id: int) -> dict:
        """File a listing under a shop section.

        `shop_section_id` is a single integer, not a list: Etsy allows exactly
        ONE section per listing, so this replaces whatever section the listing
        was in rather than adding to it.

        Same form-encoded updateListing as the title/tag writes — one field
        patched, state and everything else left alone.
        """
        return self._request(
            "PATCH",
            f"/application/shops/{shop_id}/listings/{listing_id}",
            data={"shop_section_id": int(section_id)},
        )

    # ── Listing images ──────────────────────────────────────────────────────
    def get_listing_images(self, listing_id: int) -> list[dict]:
        """Current images, each with `listing_image_id` and `rank`.

        NOTE the Etsy v3 asymmetry: reading images is listing-scoped
        (no /shops/{id}/ in the path) while upload/delete are shop-scoped.
        """
        data = self._request("GET", f"/application/listings/{listing_id}/images")
        return data.get("results", []) or []

    def upload_listing_image(self, shop_id: int, listing_id: int,
                             path: Path, rank: int) -> dict:
        """Upload a local image; rank 1 makes it the listing's main thumbnail."""
        name, payload, mime = _upload_payload(path)
        # Bytes (not a file handle) so the internal 429/5xx retry can resend.
        return self._request(
            "POST",
            f"/application/shops/{shop_id}/listings/{listing_id}/images",
            files={"image": (name, payload, mime)},
            data={"rank": str(rank), "overwrite": "true"},
        )

    def delete_listing_image(self, shop_id: int, listing_id: int, image_id: int):
        self._request(
            "DELETE",
            f"/application/shops/{shop_id}/listings/{listing_id}/images/{image_id}")

    # ── Listing video (Etsy allows one per listing, 5–15s, sound stripped) ──
    def get_listing_videos(self, listing_id: int) -> list[dict]:
        data = self._request("GET", f"/application/listings/{listing_id}/videos")
        return data.get("results", []) or []

    def upload_listing_video(self, shop_id: int, listing_id: int, path: Path) -> dict:
        """Upload a local MP4. Etsy processes it for a few minutes before it shows."""
        return self._request(
            "POST",
            f"/application/shops/{shop_id}/listings/{listing_id}/videos",
            files={"video": (path.name, path.read_bytes(), "video/mp4")},
            data={"name": path.name},
        )


    # ── Shipping profiles ───────────────────────────────────────────────────
    # Reading needs shops_r; writing needs shops_w. If the token predates the
    # shops_w scope these raise 401 — re-run `python -m scripts.auth.etsy_auth` to re-consent.

    def get_shipping_profiles(self, shop_id: int) -> list[dict]:
        """Every shipping profile in the shop, each with its destinations."""
        data = self._request("GET", f"/application/shops/{shop_id}/shipping-profiles")
        return data.get("results", [])

    def set_shipping_profile(self, shop_id: int, listing_id: int,
                             profile_id: int) -> dict:
        """Point a listing at a different shipping profile.

        `shipping_profile_id` is one integer on the listing, so this swaps which
        saved rate card the listing uses. It does NOT edit any rates, touch the
        listing's state, images or text, or involve Printify — it is the same
        one-field form-encoded updateListing as set_listing_section().

        NOTE Printify keeps its own copy of this id in the product's `external`
        block, so re-publishing the product from Printify reasserts the OLD
        profile. Re-read with get_listing() after any Printify sync.
        """
        return self._request(
            "PATCH",
            f"/application/shops/{shop_id}/listings/{listing_id}",
            data={"shipping_profile_id": int(profile_id)},
        )

    # ── Inventory / per-variation prices ────────────────────────────────────

    def get_listing_inventory(self, listing_id: int) -> dict:
        """The full variation matrix: every product, its property values and
        its per-variation offerings (price, quantity, is_enabled)."""
        return self._request("GET", f"/application/listings/{listing_id}/inventory")

    # Etsy sends these on a GET and rejects them on the PUT.
    _INV_READ_ONLY = ("product_id", "is_deleted", "offering_id",
                      "scale_name", "value_pairs")

    def update_listing_inventory(self, listing_id: int, inventory: dict) -> dict:
        """Write the whole variation matrix back.

        Etsy has no per-variation price endpoint. The inventory PUT REPLACES
        the entire matrix, so the only safe edit is read-modify-write: GET it,
        change the prices you want, hand the whole structure back. Anything
        omitted is destroyed — which is why this takes the full inventory dict
        and not a list of prices.

        Two shape differences between the GET and the PUT, both of which Etsy
        rejects rather than ignores:
          · a GET price is {amount, divisor, currency_code}; a PUT price is a
            plain decimal in the shop's currency
          · product_id / offering_id / is_deleted are read-only echoes

        Unlike the single-field listing PATCHes (see set_shipping_profile),
        this endpoint is JSON, not form-encoded.
        """
        products = []
        for prod in inventory.get("products", []):
            offerings = []
            for off in prod.get("offerings", []):
                price = off.get("price")
                if isinstance(price, dict):          # GET shape -> PUT shape
                    price = price.get("amount", 0) / (price.get("divisor") or 1)
                row = {
                    "price": round(float(price), 2),
                    "quantity": int(off.get("quantity", 0)),
                    "is_enabled": bool(off.get("is_enabled", True)),
                }
                # Listings with a processing profile carry one of these per
                # offering. Drop it and the PUT fails with
                # "All offerings need readiness state".
                if off.get("readiness_state_id") is not None:
                    row["readiness_state_id"] = int(off["readiness_state_id"])
                offerings.append(row)
            values = []
            for pv in prod.get("property_values", []):
                values.append({k: v for k, v in pv.items()
                               if k not in self._INV_READ_ONLY and v is not None})
            products.append({
                "sku": prod.get("sku") or "",
                "property_values": values,
                "offerings": offerings,
            })

        body = {"products": products}
        for key in ("price_on_property", "quantity_on_property", "sku_on_property",
                    "readiness_state_on_property"):
            body[key] = [int(x) for x in (inventory.get(key) or [])]
        return self._request(
            "PUT", f"/application/listings/{listing_id}/inventory", json=body)

    def update_shipping_destination(self, shop_id: int, profile_id: int,
                                    destination_id: int, primary_cost: float,
                                    secondary_cost: float) -> dict:
        """Rewrite one destination's first-item / each-additional-item price.

        Etsy's v3 write endpoints are form-encoded, not JSON, and costs go over
        as plain decimals in the SHOP's currency — the very field that currently
        holds Printify's raw USD figures (see fix_shipping.py for why).
        """
        return self._request(
            "PUT",
            f"/application/shops/{shop_id}/shipping-profiles/{profile_id}"
            f"/destinations/{destination_id}",
            data={"primary_cost": f"{primary_cost:.2f}",
                  "secondary_cost": f"{secondary_cost:.2f}"})
