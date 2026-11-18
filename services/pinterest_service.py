"""
pinterest_service.py — Pinterest API v5 client (boards & pins)
==============================================================
Used by sweatshirt_automation.py to publish a Pin for every design once its
Etsy listing is live. Auth is OAuth 2.0 user tokens obtained ONCE via
`python -m scripts.auth.pinterest_auth`; this module then self-manages them.

Endpoints wrapped here:
  GET    /v5/user_account          whoami / token smoke-test
  GET    /v5/boards                list boards (to resolve a board by name)
  POST   /v5/boards                create a board (sandbox needs its own)
  POST   /v5/pins                  create the Pin
  GET    /v5/pins/{pin_id}         read back a Pin

Tokens live in caching/.pinterest_tokens.json (gitignored) and are never printed.

── Three ways this differs from etsy_service.py (all of them footguns) ────────
  1. Token exchange uses HTTP Basic auth — base64(app_id:app_secret) in the
     Authorization header, form-encoded body. Etsy uses secret-less PKCE + JSON.
  2. Access tokens live 30 DAYS (Etsy: 1 hour), refresh tokens 60 days rolling.
     So we do NOT refresh on every construction the way EtsyClient does — that
     would burn a call per run for nothing. We refresh only near expiry.
  3. Scopes are COMMA-separated in the consent URL, not space-separated.
     See pinterest_auth.py.

── Access tier ───────────────────────────────────────────────────────────────
An app on TRIAL access cannot create Pins in production: they must go to the
sandbox host, where they are visible only to you. PINTEREST_ENV switches hosts:
    sandbox     -> https://api-sandbox.pinterest.com/v5   (default)
    production  -> https://api.pinterest.com/v5
Sandbox is a SEPARATE universe: boards created in the Pinterest web UI do not
exist there, which is why ensure_board() can create one on demand.
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import time
from pathlib import Path

try:
    from PIL import Image
except ImportError:                              # base64 uploads still work, just fatter
    Image = None

import requests

from services.brand_guard import scrub_text

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


PROD_BASE    = "https://api.pinterest.com/v5"
SANDBOX_BASE = "https://api-sandbox.pinterest.com/v5"

CACHE_DIR  = Path("caching")
TOKEN_FILE = CACHE_DIR / ".pinterest_tokens.json"
USER_AGENT = "pod-automation/1.0 (Pinterest marketing)"

# Board ids we have resolved before, keyed "<env>:<lowercased name>".
#
# GET /boards is EVENTUALLY CONSISTENT: a board created seconds ago is not in
# the list yet, while the create endpoint's name-uniqueness check sees it
# immediately. Resolving a name purely by listing therefore creates the same
# board over and over inside one run, and then starts failing outright with
# 400 code 58 "You already have a board with this name!". Remembering the id
# locally makes the lag irrelevant — we never have to ask twice.
BOARD_CACHE_FILE = CACHE_DIR / "pinterest_boards.json"

# Refresh when the access token has less than this left. Tokens live 30 days, so
# a 3-day cushion means a daily-ish run never races the expiry, and a machine
# left off for a fortnight still recovers on its next run.
REFRESH_MARGIN_S = 3 * 24 * 3600

# Pinterest caps base64 pin payloads well below this, but our sweatshirt renders
# are ~2.4 MB PNGs — re-encoding to JPEG keeps us comfortably clear either way.
UPLOAD_AS_JPEG = True
JPEG_QUALITY   = 92

# Pinterest's own field limits. Copy longer than this is rejected outright.
PIN_TITLE_MAX = 100
PIN_DESC_MAX  = 800
PIN_ALT_MAX   = 500


class PinterestError(RuntimeError):
    """Any Pinterest API failure."""


class PinterestAuthError(PinterestError):
    """Credentials/token problem — the fix is always `python -m scripts.auth.pinterest_auth`."""


def api_base() -> str:
    """Base URL for the configured environment. Defaults to sandbox on purpose:
    a Trial-access app cannot create Pins in production, so sandbox is the only
    setting that actually works until Standard access is granted."""
    env = os.environ.get("PINTEREST_ENV", "").strip().lower() or "sandbox"
    return PROD_BASE if env in ("prod", "production", "live") else SANDBOX_BASE


def is_sandbox() -> bool:
    return api_base() == SANDBOX_BASE


def is_verbose() -> bool:
    """PINTEREST_VERBOSE=1 → echo every API call as `METHOD /path → status`.

    Exists for the Standard-access demo video: Pinterest accepts terminal screen
    recordings, and reviewers are looking for proof that the app really calls
    their API. Friendly emoji output alone doesn't show that; a line per request
    does. Deliberately logs ONLY method, URL and status — never the
    Authorization header, token or request body — because the same review checks
    that the app isn't mishandling credentials.
    """
    return os.environ.get("PINTEREST_VERBOSE", "").strip().lower() in (
        "1", "true", "yes", "on")


def app_credentials() -> tuple[str, str]:
    """(app_id, app_secret) from the environment.

    PINTEREST_SECRET_KEY is accepted as an alias for PINTEREST_APP_SECRET — the
    Pinterest dashboard labels the field "App secret key", so both names get
    written into .env in practice.
    """
    app_id = os.environ.get("PINTEREST_APP_ID", "").strip()
    app_secret = (os.environ.get("PINTEREST_APP_SECRET", "").strip()
                  or os.environ.get("PINTEREST_SECRET_KEY", "").strip())
    return app_id, app_secret


def _load_json(path: Path, default):
    """Local copy rather than an import from printify_service — this module is
    also imported by pinterest_auth.py, which has no business pulling in the
    Printify client just to read a small cache file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _basic_auth_header(app_id: str, app_secret: str) -> str:
    raw = f"{app_id}:{app_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _image_payload(path: Path) -> tuple[str, str]:
    """(content_type, base64_data) for a local mockup.

    Re-encodes to JPEG when that is both possible and actually smaller; falls
    back to the raw bytes on any doubt. Mirrors _upload_payload() in
    etsy_service.py, but Pinterest wants base64 rather than multipart.
    """
    raw = path.read_bytes()
    fallback = (mimetypes.guess_type(path.name)[0] or "image/png", raw)
    ctype, data = fallback
    if UPLOAD_AS_JPEG and Image is not None:
        try:
            with Image.open(io.BytesIO(raw)) as im:
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY,
                                       optimize=True, subsampling=0)
            if buf.tell() < len(raw):
                ctype, data = "image/jpeg", buf.getvalue()
        except Exception:                        # unreadable/exotic → send as-is
            pass
    return ctype, base64.b64encode(data).decode("ascii")


class PinterestClient:
    """Authenticated Pinterest v5 client. Refreshes the token only near expiry."""

    def __init__(self):
        self.session = requests.Session()
        self.base = api_base()
        # Board ids resolved during THIS run; the disk cache backs it up so the
        # lag in GET /boards never costs us a duplicate create.
        self._board_memo: dict[str, str] = {}

        self.app_id, self.app_secret = app_credentials()
        if not self.app_id or not self.app_secret:
            raise PinterestAuthError(
                "PINTEREST_APP_ID / PINTEREST_APP_SECRET are not set in .env — copy them\n"
                "           from https://developers.pinterest.com/apps/ (your app page).")

        if not TOKEN_FILE.exists():
            raise PinterestAuthError(
                f"{TOKEN_FILE} not found — run the one-time authorization first:\n"
                f"           python -m scripts.auth.pinterest_auth")
        try:
            self.tokens = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise PinterestAuthError(
                f"{TOKEN_FILE} is unreadable ({e}) — re-run: python -m scripts.auth.pinterest_auth")

        # A token granted against sandbox is not valid against production (and
        # vice versa). Catching it here beats a baffling 401 six stages later.
        granted_env = self.tokens.get("env")
        current_env = "sandbox" if is_sandbox() else "production"
        if granted_env and granted_env != current_env:
            raise PinterestAuthError(
                f"The saved token was granted for '{granted_env}' but PINTEREST_ENV is "
                f"'{current_env}'.\n           Sandbox and production tokens are not "
                f"interchangeable — re-run: python -m scripts.auth.pinterest_auth")

        self._refresh_if_stale()

    # ── Token lifecycle ─────────────────────────────────────────────────────
    def _refresh_if_stale(self):
        """Refresh only when the access token is close to expiring.

        Unlike Etsy (1-hour tokens, refreshed every run), Pinterest access
        tokens live 30 days. Refreshing unconditionally would waste a call and
        needlessly rotate the refresh token on every single run.
        """
        expires_at = float(self.tokens.get("expires_at") or 0)
        if expires_at - time.time() > REFRESH_MARGIN_S:
            return
        self.refresh()

    def refresh(self):
        resp = self.session.post(
            f"{self.base}/oauth/token",
            headers={
                "Authorization": _basic_auth_header(self.app_id, self.app_secret),
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.tokens.get("refresh_token", ""),
            },
            timeout=30,
        )
        if not resp.ok:
            raise PinterestAuthError(
                f"Pinterest token refresh failed ({resp.status_code}: {resp.text[:200]}).\n"
                f"           The refresh token has likely expired (60 days unused) or was\n"
                f"           revoked — re-authorize with:  python -m scripts.auth.pinterest_auth")
        tok = resp.json()
        self.tokens["access_token"] = tok["access_token"]
        # On continuous refresh Pinterest usually returns the SAME refresh token
        # and may omit it entirely — never clobber a good one with None.
        self.tokens["refresh_token"] = tok.get("refresh_token") or self.tokens["refresh_token"]
        self.tokens["expires_at"] = time.time() + int(tok.get("expires_in", 2592000))
        self._save_tokens()

    def _save_tokens(self):
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(self.tokens, indent=2), encoding="utf-8")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.tokens['access_token']}",
            "User-Agent": USER_AGENT,
        }

    # ── Core request with retry (mirrors EtsyClient conventions) ────────────
    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base}/{path.lstrip('/')}"
        resp = None
        for attempt in range(4):
            resp = self.session.request(method, url, headers=self._headers(),
                                        timeout=60, **kwargs)
            if is_verbose():
                mark = "✓" if resp.ok else "✗"
                print(f"     🌐 {mark} {method} {url} → {resp.status_code}")
            if resp.status_code == 401:
                raise PinterestAuthError(
                    "401 Unauthorized from Pinterest — token invalid or missing scopes "
                    "(boards:read/boards:write/pins:read/pins:write). "
                    "Re-run: python -m scripts.auth.pinterest_auth")
            if resp.status_code == 403:
                raise PinterestError(
                    f"403 Forbidden from Pinterest: {resp.text[:300]}\n"
                    f"           On TRIAL access this usually means the call is not allowed "
                    f"in production.\n           Set PINTEREST_ENV=sandbox in .env, or apply "
                    f"for Standard access.")
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2 ** attempt) or 2 ** attempt)
                print(f"     ⏳ Pinterest rate limit (429). Waiting {wait}s…")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = 2 ** attempt
                print(f"     ⚠️  Pinterest server {resp.status_code}. Retrying in {wait}s…")
                time.sleep(wait)
                continue
            if not resp.ok:
                raise PinterestError(f"{method} {path} → {resp.status_code}: {resp.text[:500]}")
            return resp.json() if resp.text else {}
        raise PinterestError(
            f"{method} {path} failed after retries "
            f"(last status {resp.status_code if resp is not None else '?'}).")

    # ── Account ─────────────────────────────────────────────────────────────
    def get_user_account(self) -> dict:
        """Whoami. Cheapest possible proof that the token actually works."""
        return self._request("GET", "/user_account")

    # ── Boards ──────────────────────────────────────────────────────────────
    def list_boards(self) -> list[dict]:
        data = self._request("GET", "/boards", params={"page_size": 250})
        return data.get("items", []) or []

    def create_board(self, name: str, description: str = "") -> dict:
        return self._request("POST", "/boards", json={
            "name": name,
            "description": description[:500],
            "privacy": "PUBLIC",
        })

    def _board_cache_key(self, name: str) -> str:
        # Sandbox and production are separate universes with separate ids, so a
        # cached id is only valid for the env it was minted in.
        env = "sandbox" if is_sandbox() else "production"
        return f"{env}:{name.strip().lower()}"

    def _sandbox_board_ids(self) -> set[str]:
        """Ids of boards created in sandbox — never valid targets in production.

        Production's GET /boards also returns sandbox boards, with no field
        marking them as such, so a name match alone can pick one up. Pinning to
        it then fails with 400 code 15 "Cannot add non-sandbox pins on sandbox
        boards". The cache's sandbox: entries are the only record of which is which.
        """
        if is_sandbox():
            return set()
        return {v for k, v in _load_json(BOARD_CACHE_FILE, {}).items()
                if k.startswith("sandbox:")}

    def _cached_board_id(self, name: str) -> str | None:
        key = self._board_cache_key(name)
        if key in self._board_memo:
            return self._board_memo[key]
        cached = _load_json(BOARD_CACHE_FILE, {}).get(key)
        return cached if cached not in self._sandbox_board_ids() else None

    def _remember_board(self, name: str, board_id: str) -> None:
        key = self._board_cache_key(name)
        self._board_memo[key] = board_id
        cache = _load_json(BOARD_CACHE_FILE, {})
        cache[key] = board_id
        _save_json(BOARD_CACHE_FILE, cache)

    def _find_board_by_name(self, name: str) -> str | None:
        wanted = name.strip().lower()
        sandbox_ids = self._sandbox_board_ids()
        for b in self.list_boards():
            if (str(b.get("name", "")).strip().lower() == wanted
                    and b["id"] not in sandbox_ids):
                return b["id"]
        return None

    def ensure_board(self, name: str, description: str = "",
                     create_if_missing: bool = True) -> str:
        """Board id for `name`, creating the board if it isn't there.

        Resolution order — cache, then list, then create — exists because
        GET /boards lags behind writes (see BOARD_CACHE_FILE). Listing alone
        re-created the same board on every design in a run until the list caught
        up, and then hard-failed the run with 400 code 58.

        Sandbox also starts empty: boards you made in the Pinterest web UI live
        in production and are invisible here.
        """
        cached = self._cached_board_id(name)
        if cached:
            return cached

        found = self._find_board_by_name(name)
        if found:
            self._remember_board(name, found)
            return found

        if not create_if_missing:
            raise PinterestError(
                f"No Pinterest board named '{name}' "
                f"({'sandbox' if is_sandbox() else 'production'}).")

        try:
            board = self.create_board(name, description)
        except PinterestError as e:
            # "You already have a board with this name" (code 58) means the name
            # is taken by a board the list endpoint will not show us. Give the
            # list a moment to catch up — that resolves the common case, where
            # the board was created seconds ago by this very run.
            if "already have a board with this name" not in str(e).lower():
                raise
            print(f"     ⏳ Board '{name}' exists but isn't listed yet — waiting…")
            for delay in (2, 5, 10):
                time.sleep(delay)
                found = self._find_board_by_name(name)
                if found:
                    self._remember_board(name, found)
                    print(f"     ✓ Resolved board '{name}' (id {found})")
                    return found
            env_var = "PINTEREST_BOARD_AI_HUMOR / PINTEREST_BOARD_PROGRAMMING"
            if not is_sandbox():
                # In production the name is held by a leftover sandbox board.
                raise PinterestError(
                    f"The board name '{name}' is taken by a SANDBOX board, which "
                    f"production pins cannot use.\n"
                    f"           Fix: delete that empty board on Pinterest, or pick "
                    f"a different name via {env_var} in .env, then re-run."
                ) from e
            # Still invisible. A name can also be reserved by a board on the
            # PRODUCTION side of the account, which sandbox refuses to reuse but
            # never lists — no amount of waiting fixes that one.
            raise PinterestError(
                f"Pinterest says a board named '{name}' already exists, but it "
                f"never appears in GET /boards, so its id cannot be resolved.\n"
                f"           This happens when the name is taken on the "
                f"production side of the account while you are pinning in "
                f"sandbox.\n"
                f"           Fix: pick a different name via {env_var} in .env "
                f"(e.g. '{name} Sweatshirts') and re-run."
            ) from e

        self._remember_board(name, board["id"])
        print(f"     🆕 Created Pinterest board '{name}' (id {board['id']})")
        return board["id"]

    # ── Pins ────────────────────────────────────────────────────────────────
    def create_pin(self, board_id: str, title: str, description: str,
                   link: str, alt_text: str = "",
                   image_url: str | None = None,
                   image_path: Path | None = None) -> dict:
        """Create a Pin from either a public image URL or a local file.

        `image_url` is preferred: by the time we pin, the Etsy listing is live
        and its CDN URL holds the exact same render we uploaded, so Pinterest
        fetches it directly and we skip shipping the bytes twice. `image_path`
        (base64) is the fallback for when that URL can't be resolved.
        """
        if image_url:
            media = {"source_type": "image_url", "url": image_url}
        elif image_path is not None:
            ctype, data = _image_payload(image_path)
            media = {"source_type": "image_base64", "content_type": ctype, "data": data}
        else:
            raise PinterestError("create_pin needs either image_url or image_path.")

        # AI brand names (ChatGPT, Claude, …) never go out — services/brand_guard.py.
        body = {
            "board_id": board_id,
            "title": scrub_text(title or "")[:PIN_TITLE_MAX],
            "description": scrub_text(description or "")[:PIN_DESC_MAX],
            "link": link,
            "media_source": media,
        }
        if alt_text:
            body["alt_text"] = scrub_text(alt_text)[:PIN_ALT_MAX]
        return self._request("POST", "/pins", json=body)

    def get_pin(self, pin_id: str) -> dict:
        return self._request("GET", f"/pins/{pin_id}")
