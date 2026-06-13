"""
ai_content.py — AI-generated SEO titles, descriptions & tags (Gemini 2.5 Flash)
===============================================================================
Looks at the actual design image and writes an Etsy-optimized title,
description, and tag set for the product. Used by printify_service so every
product gets a unique, search-friendly listing instead of placeholder
lorem-ipsum.

Requires GEMINI_API_KEY_1 … GEMINI_API_KEY_5 in the environment (see .env).
Keys come from Google AI Studio (aistudio.google.com → Get API key). The keys
are used as a rotating pool: when one key hits its quota/rate limit (HTTP 429)
or is invalid, the next key takes over automatically — the batch never halts.
The "current key" is sticky across designs within a run, so once key 1 is
exhausted, all later designs start straight on key 2.

If every key fails, generate_listing_content() returns the provided fallbacks
so the pipeline never breaks.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from PIL import Image

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv not installed — env vars must be set manually
    pass

# ── Config ──────────────────────────────────────────────────────────────────
MODEL          = "gemini-2.5-flash"
KEY_ENV_NAMES  = [f"GEMINI_API_KEY_{i}" for i in range(1, 6)]
MAX_IMAGE_EDGE = 1024     # downscale the design before sending (cheaper, faster)
ETSY_TITLE_MAX = 140      # Etsy hard limit on listing title length
ETSY_TAG_MAX   = 13       # Etsy hard limit on number of tags
ETSY_TAG_LEN   = 20       # Etsy hard limit on characters per tag

SYSTEM_PROMPT = (
    "You are an expert Etsy SEO copywriter for print-on-demand apparel. You write "
    "listing titles, descriptions and tags that rank in Etsy search and convert "
    "browsers into buyers. You write naturally — never keyword-spam, ALL CAPS, or "
    "use emojis."
)

USER_PROMPT = """\
This design is printed on the FRONT (centered) of a unisex Bella+Canvas 3001 t-shirt.
Look at the design image and write an Etsy listing for it.

TITLE rules:
- Max 140 characters (aim for 120–140).
- Front-load the most-searched keywords. Include "Shirt" or "Tee" and "Unisex".
- Comma- or pipe-separated keyword phrases, readable and natural.
- Reflect the actual text / joke / theme visible in the design.
- Use Title Case. NEVER write whole words in ALL CAPS, even if the design's
  text is all-caps (Printify rejects titles with excessive caps). Short
  acronyms like AI are fine.

DESCRIPTION rules:
- 80–160 words, plain text only (Etsy does not render markdown).
- First sentence is a compelling, keyword-rich hook.
- Mention it's a soft, unisex Bella+Canvas 3001 tee and a great gift.
- Weave in relevant search terms naturally and note who it's perfect for.

TAGS rules:
- Exactly 13 tags (Etsy's maximum).
- Each tag is at most 20 characters INCLUDING spaces (Etsy's hard limit).
- Lowercase multi-word phrases buyers actually search (e.g. "coding humor shirt").
- No duplicates, no '#' symbols, no single generic words like "shirt" alone.
- Reflect the actual theme, text and audience of THIS design.
"""


# ══════════════════════════════════════════════════════════════════════════════
#  Key pool — sticky rotation across designs within a run
# ══════════════════════════════════════════════════════════════════════════════

_key_index = 0  # index into the key pool; advances permanently when a key dies


def _api_keys() -> list[str]:
    """All non-blank GEMINI_API_KEY_1..5 values, in order."""
    return [k for k in (os.environ.get(n, "").strip() for n in KEY_ENV_NAMES) if k]


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _design_to_jpeg_bytes(path: Path) -> bytes:
    """Downscale the design and flatten transparency onto white → JPEG bytes."""
    with Image.open(path) as im:
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im).convert("RGB")
        im.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _decap_title(title: str) -> str:
    """Title-case any fully-uppercase word of 3+ letters — Printify rejects
    titles with 'excessive caps' (error 61003). Short acronyms (AI, PC) stay."""
    return " ".join(
        w.capitalize() if w.isupper() and len(w.strip(",.|!-")) >= 3 else w
        for w in title.split(" ")
    )


def _sanitize_tags(tags: list[str]) -> list[str]:
    """Dedupe, strip, drop empties / over-20-char tags, cap at 13."""
    clean, seen = [], set()
    for t in tags or []:
        t = " ".join(str(t).split()).strip().lower().lstrip("#")
        if t and len(t) <= ETSY_TAG_LEN and t not in seen:
            seen.add(t)
            clean.append(t)
        if len(clean) == ETSY_TAG_MAX:
            break
    return clean


def _is_quota_or_auth_error(e: Exception) -> bool:
    """Errors that mean THIS KEY is done for → rotate to the next key."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code in (401, 403, 429):
        return True
    msg = str(e).upper()
    return any(s in msg for s in ("RESOURCE_EXHAUSTED", "QUOTA", "RATE LIMIT",
                                  "PERMISSION_DENIED", "API_KEY_INVALID", "UNAUTHENTICATED"))


def _is_transient_error(e: Exception) -> bool:
    """Server-side hiccups worth one retry on the same key."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if isinstance(code, int) and code >= 500:
        return True
    msg = str(e).upper()
    return any(s in msg for s in ("UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL", "OVERLOADED"))


# ══════════════════════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════════════════════

def generate_listing_content(
    design_path: Path,
    fallback_title: str,
    fallback_description: str,
    fallback_tags: list[str] | None = None,
) -> tuple[str, str, list[str], bool]:
    """Return (title, description, tags, ai_used).

    Looks at the design image with Gemini 2.5 Flash and generates an Etsy
    listing. Rotates through GEMINI_API_KEY_1..5 on quota/auth errors. Falls
    back to the provided placeholders (ai_used=False) if no keys are set or
    every key fails — the caller never has to handle exceptions.
    """
    global _key_index
    fallback_tags = fallback_tags or []

    keys = _api_keys()
    if not keys:
        return fallback_title, fallback_description, fallback_tags, False

    try:
        from google import genai
        from google.genai import types
        from pydantic import BaseModel
    except ImportError as e:
        print(f"     ⚠️  google-genai not installed ({e}); using placeholder. "
              f"Run: pip install -r requirements.txt")
        return fallback_title, fallback_description, fallback_tags, False

    class Listing(BaseModel):
        title: str
        description: str
        tags: list[str]

    try:
        img_bytes = _design_to_jpeg_bytes(design_path)
    except Exception as e:
        print(f"     ⚠️  Could not read design image ({type(e).__name__}: {e}); using placeholder.")
        return fallback_title, fallback_description, fallback_tags, False

    contents = [
        types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
        USER_PROMPT,
    ]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=Listing,
        max_output_tokens=2048,
    )

    # Walk the key pool starting from the current sticky index.
    while _key_index < len(keys):
        key_no = _key_index + 1  # 1-based, matches GEMINI_API_KEY_N
        for attempt in range(2):  # 2nd attempt only for transient 5xx errors
            try:
                client = genai.Client(api_key=keys[_key_index])
                response = client.models.generate_content(
                    model=MODEL, contents=contents, config=config)
                listing = response.parsed
                if listing is None:
                    raise ValueError(f"empty/unparseable response: {response.text!r:.200}")

                title = _decap_title(" ".join((listing.title or "").split()))[:ETSY_TITLE_MAX] or fallback_title
                description = (listing.description or "").strip() or fallback_description
                tags = _sanitize_tags(listing.tags) or fallback_tags
                return title, description, tags, True

            except Exception as e:
                if _is_quota_or_auth_error(e):
                    print(f"     🔑 GEMINI_API_KEY_{key_no} exhausted/rejected "
                          f"({type(e).__name__}); rotating to next key…")
                    break  # → advance to the next key
                if _is_transient_error(e) and attempt == 0:
                    print(f"     ⚠️  Transient Gemini error ({type(e).__name__}: {e}); retrying once…")
                    continue
                print(f"     🔑 GEMINI_API_KEY_{key_no} failed "
                      f"({type(e).__name__}: {e}); rotating to next key…")
                break  # → advance to the next key
        _key_index += 1

    print(f"     ⚠️  All {len(keys)} Gemini key(s) failed/exhausted; using placeholder.")
    return fallback_title, fallback_description, fallback_tags, False
