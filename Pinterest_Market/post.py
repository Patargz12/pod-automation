"""
Pinterest_Market/post.py — post the next pin(s) from queue.json
=================================================================
This is what GitHub Actions runs 3×/day. It does NO thinking: the image, copy,
Etsy link and board id were all decided earlier by prepare.py. It only:

  1. takes the next "pending" item from Pinterest_Market/queue.json
  2. POSTs it to Pinterest (base64 upload of Pinterest_Market/ready/<file>.jpg)
  3. marks it "posted" (or "failed" + reason) and saves queue.json

Imports stay light on purpose (requests + pillow) — no OpenCV, Gemini or Etsy.
"""

from __future__ import annotations

import time
from pathlib import Path

from Pinterest_Market.config import POSTS_PER_RUN, ROOT, load_queue, save_json, QUEUE_FILE

REPO_ROOT = ROOT.parent
MAX_FAILURES_PER_RUN = 3     # a broken item is skipped, but don't burn through the queue


def _pending(queue: dict) -> list[dict]:
    return [it for it in queue["items"] if it.get("status") == "pending"]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


def run(count: int = POSTS_PER_RUN, dry_run: bool = False) -> int:
    queue = load_queue()
    pending = _pending(queue)
    print(f"📌 Pinterest autopost — {len(pending)} pending in queue"
          f"{'  (DRY RUN)' if dry_run else ''}")
    if not pending:
        print("   Queue empty — nothing to post. Add more with: "
              "python -m scripts.pinterest_marketing prepare")
        return 0

    if dry_run:
        for it in pending[:count]:
            print(f"\n   would post  {it['id']}")
            print(f"     image : {it['image']}")
            print(f"     board : {it.get('board_name', '?')} ({it['board_id']})")
            print(f"     title : {it['title']}")
            print(f"     desc  : {it['description']}")
            print(f"     alt   : {it.get('alt_text', '')}")
            print(f"     link  : {it['link']}")
        return 0

    # Imported here so --dry-run and `status` work without credentials.
    from services.pinterest_service import PinterestClient, PinterestError, PinterestAuthError

    try:
        client = PinterestClient()
    except PinterestAuthError as e:
        print(f"   ❌ Pinterest auth: {e}")
        return 1

    posted = failures = 0
    for it in pending:
        if posted >= count or failures >= MAX_FAILURES_PER_RUN:
            break
        image = REPO_ROOT / it["image"]
        print(f"\n   ── {it['id']}")
        if not image.exists():
            it.update(status="failed", error=f"image missing: {it['image']}", failed_at=_now())
            print(f"     ❌ image missing: {it['image']}")
            failures += 1
            save_json(QUEUE_FILE, queue)
            continue
        try:
            pin = client.create_pin(
                board_id=it["board_id"], title=it["title"], description=it["description"],
                link=it["link"], alt_text=it.get("alt_text", ""), image_path=image,
            )
        except PinterestAuthError as e:
            # Not this item's fault — leave it pending and fail the run loudly.
            print(f"     ❌ Pinterest auth: {e}")
            save_json(QUEUE_FILE, queue)
            return 1
        except PinterestError as e:
            it.update(status="failed", error=str(e)[:300], failed_at=_now())
            print(f"     ❌ {str(e)[:300]}")
            failures += 1
            save_json(QUEUE_FILE, queue)
            continue

        it.update(status="posted", pin_id=pin.get("id"), posted_at=_now())
        it.pop("error", None)
        save_json(QUEUE_FILE, queue)
        posted += 1
        print(f"     ✅ pin {pin.get('id')} → {it['link']}")

    left = len(_pending(queue))
    print(f"\n   {posted} posted · {failures} failed · {left} still pending")
    return 0 if posted or not failures else 1


def status() -> int:
    import json
    queue = load_queue()
    items = queue["items"]
    by = {s: sum(1 for it in items if it.get("status") == s)
          for s in ("pending", "posted", "failed")}
    per_day = 3 * POSTS_PER_RUN
    print("📊 Pinterest queue")
    print(f"   pending {by['pending']} · posted {by['posted']} · failed {by['failed']}")
    print(f"   runway  ≈ {by['pending'] / per_day:.0f} days at {per_day} pins/day")
    for it in items:
        if it.get("status") == "failed":
            print(f"   ⚠️  {it['id']}: {it.get('error', '')[:120]}")
    last = [it for it in items if it.get("status") == "posted"]
    if last:
        print(f"   last posted: {last[-1]['id']} at {last[-1].get('posted_at')}")

    tok = REPO_ROOT / "caching" / ".pinterest_tokens.json"
    try:
        exp = float(json.loads(tok.read_text(encoding="utf-8")).get("expires_at") or 0)
        days = (exp - time.time()) / 86400
        note = "OK" if days > 3 else "will auto-refresh on next Pinterest call"
        print(f"   local access token: {days:+.0f} days left ({note})")
    except (OSError, ValueError):
        print("   local access token: not found (fine on GitHub Actions)")
    return 0
