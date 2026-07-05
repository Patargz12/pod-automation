"""
check_gemini.py — Gemini API key health + t-shirt capacity estimator
=====================================================================
USAGE:
  python check_gemini.py

WHAT IT DOES:
  • Reads GEMINI_API_KEY_1 … GEMINI_API_KEY_5 from your .env / environment.
  • Pings each key (a free `count_tokens` call — does NOT spend generation
    quota) to confirm it is valid and authorized.
  • Estimates how many t-shirt listings you can automate, since each t-shirt
    = exactly ONE Gemini request in this pipeline (see services/ai_content.py).

IMPORTANT — why there is no "tokens remaining" number:
  Google's Gemini API does NOT expose a token balance. Free-tier keys are
  limited by REQUESTS PER DAY (RPD), a rate limit that RESETS every day
  (~midnight US Pacific), not a depleting pool of tokens. So the honest,
  useful metric is "requests/day", which this script converts into a
  per-day t-shirt estimate.

  If you are on a different plan, update FREE_TIER_REQUESTS_PER_DAY_PER_KEY
  below to match your actual limit (see the rate-limits page for your model):
    https://ai.google.dev/gemini-api/docs/rate-limits
"""

from __future__ import annotations

import sys

# Ensure UTF-8 output so emoji / box-drawing chars don't crash Windows consoles.
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

import os

# Reuse the SAME keys + model the real pipeline uses, so this check reflects
# exactly what generate_listing_content() will do.
from services.ai_content import KEY_ENV_NAMES, MODEL

# ── Capacity assumptions (EDIT IF YOUR PLAN DIFFERS) ──────────────────────────
# Free-tier requests/day per key for gemini-2.5-flash. Google changes these
# periodically — adjust to match https://ai.google.dev/gemini-api/docs/rate-limits
FREE_TIER_REQUESTS_PER_DAY_PER_KEY = 250

# Each t-shirt listing costs exactly ONE Gemini request (1 image + 1 prompt
# → title/description/tags). See services/ai_content.py.
REQUESTS_PER_TSHIRT = 1


def _classify_error(e: Exception) -> str:
    """Map an exception to a short human status."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    msg = str(e).upper()
    if code in (401, 403) or any(s in msg for s in
                                 ("API_KEY_INVALID", "PERMISSION_DENIED", "UNAUTHENTICATED")):
        return "invalid / unauthorized"
    if code == 429 or any(s in msg for s in ("RESOURCE_EXHAUSTED", "QUOTA", "RATE LIMIT")):
        return "rate-limited right now (quota in use)"
    return f"error ({type(e).__name__}: {str(e)[:80]})"


def _check_key(key: str):
    """Return (ok: bool, status: str). Uses count_tokens — free, no gen quota."""
    try:
        from google import genai
    except ImportError:
        return False, "google-genai not installed (pip install -r requirements.txt)"
    try:
        client = genai.Client(api_key=key)
        # count_tokens validates the key/auth without consuming generation RPD.
        client.models.count_tokens(model=MODEL, contents="ping")
        return True, "valid"
    except Exception as e:  # noqa: BLE001 — we want to report any failure
        # A rate-limit here still means the key is real & working, just busy.
        status = _classify_error(e)
        return ("rate-limited" in status), status


def main() -> int:
    print("\n" + "═" * 56)
    print("  🔑 Gemini API Key Check + T-Shirt Capacity")
    print("═" * 56)
    print(f"  Model: {MODEL}\n")

    working = 0
    any_set = False
    for name in KEY_ENV_NAMES:
        key = os.environ.get(name, "").strip()
        if not key:
            print(f"  {name}  —  not set")
            continue
        any_set = True
        ok, status = _check_key(key)
        icon = "✓" if ok else "✗"
        print(f"  {name}  {icon}  {status}")
        if ok:
            working += 1

    total_keys = len(KEY_ENV_NAMES)
    print(f"\n  Working keys: {working} / {total_keys}")

    if not any_set:
        print("\n  ⚠️  No GEMINI_API_KEY_* set. Copy .env.example → .env and paste")
        print("      keys from https://aistudio.google.com → Get API key.\n")
        return 1

    daily_requests = working * FREE_TIER_REQUESTS_PER_DAY_PER_KEY
    capacity = daily_requests // REQUESTS_PER_TSHIRT
    low = int(capacity * 0.9)  # small buffer for retries / failed parses

    if working:
        print("\n  " + "─" * 52)
        print(f"  ** With your currently available keys you can automate")
        print(f"     around {low:,}–{capacity:,} T-shirts per day. **")
        print("  " + "─" * 52 + "\n")
    else:
        print("\n  ❌ No working keys — listings will fall back to placeholder text.\n")

    return 0 if working else 1


if __name__ == "__main__":
    sys.exit(main())
