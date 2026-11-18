"""
pinterest_auth.py — One-time Pinterest OAuth 2.0 authorization
==============================================================
Pinterest API v5 needs a *user* token to create boards and pins. This script
gets one with a single browser click:

  1. Starts a tiny local server on http://localhost:8081/callback.
  2. Opens your browser to Pinterest's consent page — log in as the BUSINESS
     account that owns the boards and click "Give access". (The only manual step.)
  3. Exchanges the returned code for an access token (lives 30 days) and a
     refresh token (60 days, rolling), then saves both to
     caching/.pinterest_tokens.json (gitignored — treat it like a password).

After this, sweatshirt_automation.py refreshes the token automatically when it
gets close to expiring. You only need to re-run this if nothing touches
Pinterest for 60 straight days, or if you switch PINTEREST_ENV.

BEFORE THE FIRST RUN (one-time, ~2 minutes):
  1. https://developers.pinterest.com/apps/ → open your app.
  2. Add EXACTLY this redirect URI:  http://localhost:8081/callback
     (Port 8081, NOT 8080 — etsy_auth.py already owns 8080.)
  3. Set PINTEREST_APP_ID and PINTEREST_APP_SECRET in .env.

SANDBOX vs PRODUCTION:
  PINTEREST_ENV in .env decides which host the token is minted against, and the
  token is stamped with it so a mismatch fails loudly instead of 401-ing later.
  A TRIAL-access app cannot create Pins in production — keep this on `sandbox`
  until Standard access is approved.

USAGE:
  python -m scripts.auth.pinterest_auth
"""

from __future__ import annotations

import base64
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

from services.pinterest_service import (
    TOKEN_FILE,
    USER_AGENT,
    api_base,
    app_credentials,
    is_sandbox,
    _basic_auth_header,
)

CONSENT_URL = "https://www.pinterest.com/oauth/"

SCOPES = "boards:read,boards:write,pins:read,pins:write,user_accounts:read"


def _auth_port() -> int:
    return int(os.environ.get("PINTEREST_AUTH_PORT", "").strip() or 8081)


def _mask(value: str, keep: int = 6) -> str:
    """First few chars only — the redirect page may be on camera."""
    return value[:keep] + "…" + "•" * 10 if value else "?"


def _exchange_code(code: str, redirect_uri: str) -> tuple[dict, dict | None]:
    """Swap the one-time code for tokens, then whoami. Returns (tokens, account)."""
    app_id, app_secret = app_credentials()

    resp = requests.post(
        f"{api_base()}/oauth/token",
        headers={
            "Authorization": _basic_auth_header(app_id, app_secret),
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    if not resp.ok:
        hint = ""
        if resp.status_code in (400, 401):
            hint = (f" — check that the redirect URI registered on the app EXACTLY "
                    f"matches {redirect_uri} (no trailing slash, http not https).")
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): "
                           f"{resp.text[:300]}{hint}")
    tok = resp.json()
    tokens = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": time.time() + int(tok.get("expires_in", 2592000)),
        "scope": tok.get("scope", SCOPES),
        # Stamped so PinterestClient can refuse a sandbox token in production
        # (and vice versa) instead of failing with a mystery 401 later.
        "env": "sandbox" if is_sandbox() else "production",
    }
    # Smoke-test the token immediately: far better to fail here than six stages
    # into an unattended run.
    me = requests.get(f"{api_base()}/user_account", headers={
        "Authorization": f"Bearer {tokens['access_token']}",
        "User-Agent": USER_AGENT,
    }, timeout=30)
    return tokens, (me.json() if me.ok else None)


_PAGE = """<html><head><meta charset="utf-8"><title>Nullish — Pinterest connected</title></head>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:60px auto;line-height:1.6">
<h2>{headline}</h2><ol>{steps}</ol><p style="color:#666">{footer}</p></body></html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches Pinterest's one redirect to /callback, exchanges the code right
    here, and shows each step on the page (this is what the review video films)."""

    result: dict = {}
    state: str = ""
    redirect_uri: str = ""

    def _page(self, headline: str, steps: list[str], footer: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_PAGE.format(
            headline=headline, footer=footer,
            steps="".join(f"<li>{s}</li>" for s in steps)).encode("utf-8"))

    def _fail(self, headline: str, steps: list[str], error: str):
        _CallbackHandler.result = {"error": error}
        self._page(headline, steps + [error], "Return to the terminal.")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if "code" not in params:
            self._fail("❌ Authorization failed", [],
                       params.get("error_description") or params.get("error", "unknown error"))
            return
        if params.get("state") != _CallbackHandler.state:
            self._fail("❌ Authorization failed", [],
                       "State mismatch in the OAuth redirect — possible tampering; aborting.")
            return

        steps = [f"Redirected back to <code>{_CallbackHandler.redirect_uri}</code>",
                 f"Authorization code received: <code>{_mask(params['code'])}</code>"]
        try:
            tokens, acct = _exchange_code(params["code"], _CallbackHandler.redirect_uri)
        except (RuntimeError, requests.RequestException) as e:
            self._fail("❌ Token exchange failed", steps, str(e))
            return

        steps.append(f"Code exchanged for an access token: "
                     f"<code>{_mask(tokens['access_token'])}</code>")
        if acct:
            steps.append(f"Connected as <b>{acct.get('username', '?')}</b> "
                         f"({acct.get('account_type', '?')})")
        _CallbackHandler.result = {"tokens": tokens, "account": acct}
        self._page("✅ Pinterest connected", steps,
                   "You can close this tab — the automation continues in the terminal.")

    def log_message(self, *args):  # silence the default per-request logging
        pass


def authorize() -> dict:
    """Run the full browser flow, save the tokens, return them. Exits on failure."""
    app_id, app_secret = app_credentials()
    if not app_id or not app_secret:
        print("❌  PINTEREST_APP_ID / PINTEREST_APP_SECRET are not set in .env —")
        print("    copy them from https://developers.pinterest.com/apps/ (your app page).")
        sys.exit(1)

    port = _auth_port()
    redirect_uri = f"http://localhost:{port}/callback"
    env_name = "sandbox" if is_sandbox() else "production"
    state = secrets.token_urlsafe(16)
    _CallbackHandler.state = state
    _CallbackHandler.redirect_uri = redirect_uri
    _CallbackHandler.result = {}

    consent_url = CONSENT_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    })

    print("\n" + "═" * 56)
    print("  📌  Pinterest authorization (OAuth 2.0)")
    print("═" * 56)
    print(f"\n  Environment: {env_name.upper()}  ({api_base()})")
    if env_name == "sandbox":
        print("  ℹ️  Sandbox pins are visible ONLY to you. That is expected on")
        print("      Trial access — production pins need Standard access.")
    print(f"\n  Listening on {redirect_uri} …")
    print("  Opening your browser — log in as the BUSINESS account and click")
    print("  “Give access”. If no browser opens, visit this URL:\n")
    print(f"  {consent_url}\n")

    try:
        server = HTTPServer(("localhost", port), _CallbackHandler)
    except OSError as e:
        print(f"❌  Could not listen on localhost:{port} ({e}).")
        print("    Close whatever uses that port, or set PINTEREST_AUTH_PORT in .env")
        print("    (and register the matching redirect URI in the Pinterest app).")
        sys.exit(1)

    webbrowser.open(consent_url)
    while not _CallbackHandler.result:
        server.handle_request()          # one request at a time until /callback
    server.server_close()
    result = _CallbackHandler.result

    if "error" in result:
        print(f"\n❌  Pinterest authorization failed: {result['error']}")
        sys.exit(1)

    tokens, acct = result["tokens"], result["account"]
    print("  ✓ Authorization code received — exchanged it for an access token")
    if acct:
        print(f"  ✓ Connected as '{acct.get('username', '?')}' "
              f"({acct.get('account_type', '?')})")
    else:
        print("  ⚠️  Token saved but /user_account did not answer — check the app.")
    if not tokens["refresh_token"]:
        print("  ⚠️  Pinterest returned no refresh token — the access token will expire")
        print("      in ~30 days and you'll need to re-run this script then.")

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    print(f"\n  💾 Tokens saved to {TOKEN_FILE}  (gitignored — do NOT commit or share)")
    return tokens


def main():
    authorize()
    print("  🎉 Done! You can now run:  python -m scripts.create.sweatshirt_automation --dry-run\n")


if __name__ == "__main__":
    main()
