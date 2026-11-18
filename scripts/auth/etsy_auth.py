"""
etsy_auth.py — One-time Etsy OAuth 2.0 authorization (PKCE)
===========================================================
Etsy API v3 needs a *user* token to read drafts and edit listing images —
the app keystring alone only unlocks public endpoints. This script gets that
token with a single browser click:

  1. Generates a PKCE code verifier + challenge.
  2. Starts a tiny local server on http://localhost:8080/callback.
  3. Opens your browser to Etsy's consent page — log in as the SHOP OWNER
     account and click "Grant access". (The only manual step.)
  4. Exchanges the returned code for an access token (lives 1 hour) and a
     refresh token (lives ~90 days), fetches your Etsy shop id, and saves
     everything to caching/.etsy_tokens.json (gitignored — treat it like a
     password).

After this, marketing_sweatshirt.py silently refreshes the token on every
run and rolls the 90-day refresh expiry forward. You only ever need to run
this script again if no script touches Etsy for ~90 straight days.

BEFORE THE FIRST RUN (one-time, ~2 minutes):
  1. Go to https://www.etsy.com/developers/your-apps → open your app.
  2. Add EXACTLY this to the app's Callback URLs:  http://localhost:8080/callback
     (Etsy refuses the OAuth flow if the redirect URI isn't pre-registered.)
  3. Make sure ETSY_KEYSTRING and ETSY_SHARED_SECRET are set in .env (both are
     on the app page — since Feb 9 2026 Etsy requires the shared secret in the
     x-api-key header of every API request).

USAGE:
  python -m scripts.auth.etsy_auth
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

# Imported purely for its import-time side effect: etsy_service pins outbound
# connections to IPv4, without which every call to api.etsy.com can stall ~21 s
# on a synthesised NAT64 address that goes nowhere. See the note in that module.
try:
    import services.etsy_service  # noqa: F401
except ImportError:
    pass

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

CONNECT_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL   = "https://api.etsy.com/v3/public/oauth/token"
ME_URL      = "https://api.etsy.com/v3/application/users/me"
TOKEN_FILE  = Path("caching") / ".etsy_tokens.json"   # kept in sync with services/etsy_service.py

# listings_r = read drafts · listings_w = upload images + set auto-renew
# listings_d = delete images
# images · shops_r = resolve the shop id via /users/me.
# shops_w   = edit shipping profiles (fix_shipping.py). Printify publishes its
#             carrier rates as bare USD numbers; in a PHP shop Etsy stamps them
#             "PHP", so P8.49 gets charged for P525 of postage. Rewriting those
#             rates needs write access to the shop, not just the listings.
SCOPES = "listings_r listings_w listings_d shops_r shops_w"


def _auth_port() -> int:
    return int(os.environ.get("ETSY_AUTH_PORT", "").strip() or 8080)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches Etsy's one redirect to /callback and stores code + state."""

    result: dict = {}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        _CallbackHandler.result = params
        ok = "code" in params
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("✅ Authorization received — you can close this tab and return "
               "to the terminal." if ok else
               f"❌ Authorization failed: {params.get('error_description') or params.get('error', 'unknown error')}")
        self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode("utf-8"))

    def log_message(self, *args):  # silence the default per-request logging
        pass


def main():
    keystring = os.environ.get("ETSY_KEYSTRING", "").strip()
    if not keystring:
        print("❌  ETSY_KEYSTRING is not set in .env — copy the 'Keystring' from")
        print("    https://www.etsy.com/developers/your-apps and add it to .env.")
        sys.exit(1)

    port = _auth_port()
    redirect_uri = f"http://localhost:{port}/callback"

    # PKCE: verifier (43–128 url-safe chars) + S256 challenge, no secret needed.
    verifier  = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(16)

    consent_url = CONNECT_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": keystring,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    print("\n" + "═" * 56)
    print("  🔑  Etsy one-time authorization (OAuth 2.0 + PKCE)")
    print("═" * 56)
    print(f"\n  Listening on {redirect_uri} …")
    print("  Opening your browser — log in as the SHOP OWNER account and")
    print("  click “Grant access”. If no browser opens, visit this URL:\n")
    print(f"  {consent_url}\n")

    try:
        server = HTTPServer(("localhost", port), _CallbackHandler)
    except OSError as e:
        print(f"❌  Could not listen on localhost:{port} ({e}).")
        print("    Close whatever uses that port, or set ETSY_AUTH_PORT in .env")
        print("    (and register the matching callback URL in the Etsy app).")
        sys.exit(1)

    webbrowser.open(consent_url)
    while "code" not in _CallbackHandler.result and "error" not in _CallbackHandler.result:
        server.handle_request()          # one request at a time until /callback
    server.server_close()
    result = _CallbackHandler.result

    if "error" in result:
        print(f"\n❌  Etsy refused authorization: "
              f"{result.get('error_description') or result['error']}")
        sys.exit(1)
    if result.get("state") != state:
        print("\n❌  State mismatch in the OAuth redirect — possible tampering; aborting.")
        sys.exit(1)

    print("  ✓ Authorization code received — exchanging it for tokens…")
    resp = requests.post(TOKEN_URL, json={
        "grant_type": "authorization_code",
        "client_id": keystring,
        "redirect_uri": redirect_uri,
        "code": result["code"],
        "code_verifier": verifier,
    }, timeout=30)
    if not resp.ok:
        print(f"❌  Token exchange failed ({resp.status_code}): {resp.text[:300]}")
        sys.exit(1)
    tok = resp.json()

    tokens = {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": time.time() + int(tok.get("expires_in", 3600)),
    }

    # Resolve the Etsy shop id once so later scripts never have to.
    # x-api-key must be "keystring:sharedsecret" since Feb 9 2026.
    shared_secret = os.environ.get("ETSY_SHARED_SECRET", "").strip()
    me = requests.get(ME_URL, headers={
        "x-api-key": f"{keystring}:{shared_secret}" if shared_secret else keystring,
        "Authorization": f"Bearer {tokens['access_token']}",
    }, timeout=30)
    if me.ok:
        tokens["user_id"] = me.json().get("user_id")
        tokens["shop_id"] = me.json().get("shop_id")
        print(f"  ✓ Connected to Etsy shop id {tokens['shop_id']}")
    else:
        print(f"  ⚠️  Could not fetch shop id ({me.status_code}) — will be "
              f"resolved automatically on the first marketing run.")

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    print(f"\n  💾 Tokens saved to {TOKEN_FILE}  (gitignored — do NOT commit or share)")
    print("  🎉 Done! You can now run:  python -m scripts.create.marketing_sweatshirt --dry-run\n")


if __name__ == "__main__":
    main()
