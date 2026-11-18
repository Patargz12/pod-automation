"""
pinterest_verify.py — prove the pins actually exist
===================================================

    python -m scripts.auth.pinterest_verify

WHY THIS EXISTS:
  On TRIAL access every pin goes to api-sandbox.pinterest.com. Those pins DO
  show up on your own Pinterest profile (pinterest.com or the mobile app) —
  visible to you alone, never in search, feeds or ads — so eyeballing them is a
  perfectly good check.

  This is the other half of that: it reads caching/pinned_sweatshirt.json, asks
  the API for every pin id recorded there, and reports what came back. Useful
  because the ledger is what the automation trusts, so a pin that exists on your
  profile but is missing here (or vice versa) is a drift worth knowing about.

WHAT A HEALTHY RUN LOOKS LIKE:
  Every design shows ✓ with a title, a live Etsy link, and a media URL you can
  paste into a browser to see the exact image Pinterest stored.

  A ✗ means the id in the ledger no longer resolves — the pin was deleted, or it
  was created in the OTHER environment (sandbox ids do not exist in production
  and vice versa; that case is called out separately rather than as an error).

USAGE:
  python -m scripts.auth.pinterest_verify            # check the current PINTEREST_ENV
  python -m scripts.auth.pinterest_verify --boards   # also dump every board in this env
"""

from __future__ import annotations

import sys

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
    PinterestClient,
    PinterestError,
    PinterestAuthError,
    api_base,
    is_sandbox,
)
from services.printify_service import CACHE_DIR, _load_json

PINNED_LEDGER = CACHE_DIR / "pinned_sweatshirt.json"


def main(show_boards: bool = False) -> int:
    env = "sandbox" if is_sandbox() else "production"

    print("\n" + "═" * 64)
    print("  📌  PINTEREST VERIFICATION")
    print("═" * 64)
    print(f"     env   : {env.upper()}   ({api_base()})")

    try:
        client = PinterestClient()
        acct = client.get_user_account()
    except PinterestAuthError as e:
        print(f"     ❌ Auth: {e}")
        print(f"        Fix with: python -m scripts.auth.pinterest_auth\n")
        return 1
    except PinterestError as e:
        print(f"     ❌ Pinterest: {e}\n")
        return 1

    print(f"     account: {acct.get('username', '?')} ({acct.get('account_type', '?')})")

    if show_boards:
        boards = client.list_boards()
        print(f"\n  📋 Boards in this environment: {len(boards)}")
        for b in boards:
            print(f"     • {b.get('name')}  ·  id {b.get('id')}  ·  "
                  f"{b.get('pin_count', '?')} pin(s)")

    records = _load_json(PINNED_LEDGER, {"products": []}).get("products", [])
    pinned = [r for r in records if r.get("pin_id")]

    print(f"\n  📒 {PINNED_LEDGER.name}: {len(records)} design(s), "
          f"{len(pinned)} with a pin id")
    if not pinned:
        print("     ℹ️  Nothing pinned yet — run `python -m scripts.create.sweatshirt_automation`.\n")
        return 0

    ok, missing, other_env = 0, [], []

    for rec in pinned:
        design = rec["design"]
        pin_id = rec["pin_id"]
        rec_env = rec.get("pinterest_env", "?")

        # A pin made in the other environment cannot resolve here. Report it as
        # a mismatch, not a failure — it is the expected state right after you
        # flip PINTEREST_ENV and before the next automation run re-pins.
        if rec_env != env:
            print(f"\n  ⏭️  {design}")
            print(f"      pin {pin_id} was created in {rec_env.upper()}, "
                  f"not {env.upper()} — skipping.")
            print(f"      Re-run `python -m scripts.create.sweatshirt_automation` to pin it here.")
            other_env.append(design)
            continue

        try:
            pin = client.get_pin(pin_id)
        except PinterestError as e:
            print(f"\n  ✗  {design}")
            print(f"      pin {pin_id} did not resolve: {e}")
            missing.append(design)
            continue

        media = pin.get("media") or {}
        image_url = (media.get("images", {}).get("600x", {}).get("url")
                     or media.get("images", {}).get("originals", {}).get("url")
                     or "")

        print(f"\n  ✓  {design}")
        print(f"      pin    : {pin_id}   (board '{rec.get('board', '?')}')")
        print(f"      title  : {pin.get('title') or '(none)'}")
        print(f"      link   : {pin.get('link') or '(none)'}")
        if image_url:
            print(f"      image  : {image_url}")
        if not rec.get("pin_ai_used", True):
            print(f"      ⚠️  placeholder copy — Gemini quota was exhausted for this pin.")
        ok += 1

    print("\n" + "═" * 64)
    print(f"  {ok} verified · {len(missing)} unresolved · "
          f"{len(other_env)} in the other environment")
    if is_sandbox() and ok:
        print("\n  🧪 These are SANDBOX pins — real objects, visible only to you.")
        print("     You CAN see them: open your own Pinterest profile on")
        print("     pinterest.com or in the mobile app and they appear there.")
        print("     Nobody else sees them, they never reach search or feeds, and")
        print("     they cannot be used for ads. That is Trial access working as")
        print("     designed, not a bug in the pipeline.")
        print("\n     After Pinterest grants Standard access:")
        print("       1. set PINTEREST_ENV=production in .env")
        print("       2. python -m scripts.create.sweatshirt_automation     (same command)")
        print("       3. python -m scripts.auth.pinterest_verify          (to confirm)")
    print()
    return 0 if not missing else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)
    sys.exit(main(show_boards="--boards" in args))
