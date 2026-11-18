"""
video_pipeline.py — Designs folder → videos on BOTH Etsy listings (tee + sweatshirt)
=====================================================================================
Drop designs into assets/designs/ and run ONE command. For every design it
looks for its t-shirt listing AND its sweatshirt listing, then:

  1. MOCKUPS — renders the design on every color template of that garment:
       output/tshirt/<design>/   ·   output/sweatshirt/<design>/
  2. FINDS THE LISTING — two ways, both must end in a PICTURE MATCH (the
     listing's Etsy photos look near-exactly like the new mockups, and like
     no other design folder more):
       a) Printify (read only): the tee (blueprint 12) / sweatshirt (blueprint 49)
          product that prints a file with the SAME NAME → its Etsy listing.
       b) If (a) finds nothing or its photos don't match: every active Etsy
          listing of that garment is compared by photo. Exactly ONE listing
          may match — zero or several means it stops and asks you.
  3. VIDEO — always rebuilt from the fresh mockups, so an old video with the
     same name can never be reused (the old file goes to .../archive/):
       output/video/tshirt/<design>.mp4  ·  output/video/sweatshirt/<design>.mp4
  4. UPLOAD — only active listings with no video yet. Everything that could
     not be matched is printed under NEEDS YOUR HELP and left untouched.

Printify is never written: no duplicate products, no listing switched off.
Uploads are recorded in caching/added_video.json (never touched again), and
Etsy photo fingerprints in caching/video_fingerprints.json (so the photo
search only downloads new photos). Re-running is always safe.

USAGE
  python -m scripts.etsy.video_pipeline                    # do everything
  python -m scripts.etsy.video_pipeline --dry-run          # match table only, upload nothing
  python -m scripts.etsy.video_pipeline --only nap Opinion # just these designs
  python -m scripts.etsy.video_pipeline --use nap=4551234567
        # you found the listing yourself: it is used as-is (garment worked
        # out from Printify or the title); the picture distance is still shown
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
from PIL import Image

from services.etsy_service import EtsyClient, EtsyError
from services.printify_service import PrintifyClient
from services.upscale_service import upscale as upscale_designs
from services.mockup_service import generate_mockups
from scripts.create.execute import gather_files, load_config, DESIGNS_DIR, FINAL_DESIGNS_DIR, IMG_EXTS
from scripts.create import sweatshirt, tshirt
from scripts.create.slideshow import build as build_video
from scripts.etsy.add_guide import is_tee, is_sweatshirt
from scripts.etsy.add_video import fingerprint, distance, log, LEDGER, MATCH_MAX, PAUSE, CACHE_DIR
from scripts.printify.reprice import all_products, resolve_shop_id

GARMENTS = {
    "tshirt": {
        "blueprint": 12,                              # Bella+Canvas 3001
        "templates": tshirt.TSHIRT_THUMBS_DIR,
        "corners": tshirt.ensure_shared_corners,
        "is_title": is_tee,
    },
    "sweatshirt": {
        "blueprint": 49,                              # Gildan 18000
        "templates": sweatshirt.SWEATSHIRT_THUMBS_DIR,
        "corners": sweatshirt.ensure_shared_corners,
        "is_title": is_sweatshirt,
    },
}
OUTPUT = Path("output")
FP_CACHE = CACHE_DIR / "video_fingerprints.json"


# ── Printify: (garment, design file name) → Etsy listing ids ───────────────
def printify_listings() -> tuple[dict, dict]:
    client = PrintifyClient(os.environ["PRINTIFY_API_TOKEN"])
    by_name = {g: {} for g in GARMENTS}
    garment_of = {}                                   # listing id → garment
    blueprints = {cfg["blueprint"]: g for g, cfg in GARMENTS.items()}
    for p in all_products(client, resolve_shop_id(client)):
        garment = blueprints.get(p.get("blueprint_id"))
        listing_id = (p.get("external") or {}).get("id")
        if not garment or not listing_id:
            continue
        garment_of[int(listing_id)] = garment
        names = {Path(i["name"]).stem
                 for area in p.get("print_areas", [])
                 for ph in area.get("placeholders", [])
                 for i in ph.get("images", [])}
        for name in names:
            by_name[garment].setdefault(name, []).append(int(listing_id))
    return by_name, garment_of


# ── Mockups + video ────────────────────────────────────────────────────────
def render_mockups(garment: str, designs: list[str]):
    cfg = GARMENTS[garment]
    thumbnails = gather_files(cfg["templates"])
    shared = cfg["corners"](load_config(), thumbnails)
    if shared is None:
        sys.exit(f"No print-area corners saved for {garment} — run python -m scripts.create.{garment} --setup")
    ref = tuple(shared["ref_size"])
    corners = {}
    for t in thumbnails:
        with Image.open(t) as im:
            corners[t.name] = sweatshirt.scale_corners(shared["corners"], ref, im.size)
    generate_mockups(thumbnails=thumbnails,
                     designs=[FINAL_DESIGNS_DIR / f"{d}.png" for d in designs],
                     corners_map=corners, output_dir=OUTPUT / garment, group_by_design=True)


def make_video(garment: str, design: str) -> Path | None:
    out = OUTPUT / "video" / garment / f"{design}.mp4"
    if out.exists():                                  # never reuse — it may be another design's
        archive = out.parent / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        shutil.move(out, archive / f"{design}_{datetime.now():%Y%m%d_%H%M%S}.mp4")
    print(f"\n[{garment} · {design}]")
    return out if build_video(OUTPUT / garment / design, out, 1.5, 1.15) else None


# ── Picture check ──────────────────────────────────────────────────────────
def local_fingerprints(garment: str) -> dict[str, list]:
    out = {}
    for folder in sorted(p for p in (OUTPUT / garment).iterdir() if p.is_dir()):
        images = sorted(folder.glob("*.png")) + sorted(folder.glob("*.jpg"))
        if images:
            out[folder.name] = [fingerprint(Image.open(p)) for p in images]
    return out


class RemotePhotos:
    """Fingerprints of a listing's Etsy photos, cached by photo id."""

    def __init__(self, etsy: EtsyClient):
        self.etsy = etsy
        self.cache = json.loads(FP_CACHE.read_text(encoding="utf-8")) if FP_CACHE.exists() else {}

    def of(self, listing_id: int) -> list[list[bool]]:
        fps, new = [], False
        for img in self.etsy.get_listing_images(listing_id):
            key = str(img["listing_image_id"])
            if key not in self.cache:
                resp = requests.get(img["url_570xN"], timeout=30)
                resp.raise_for_status()
                self.cache[key] = "".join("1" if b else "0"
                                          for b in fingerprint(Image.open(io.BytesIO(resp.content))))
                new = True
            fps.append([c == "1" for c in self.cache[key]])
        if new:
            self.save()                               # an interrupted run keeps what it downloaded
        return fps

    def save(self):
        FP_CACHE.write_text(json.dumps(self.cache), encoding="utf-8")


def scores(remote: list, local: dict) -> dict[str, int]:
    """Distance from the listing's photos to every local design folder."""
    if not remote:
        return {}
    return {name: min(distance(a, b) for a in remote for b in fps) for name, fps in local.items()}


def verdict(sc: dict, design: str) -> tuple[bool, str]:
    if not sc:
        return False, "listing has no photos"
    best = min(sc, key=sc.get)
    mine = sc.get(design, 999)
    if best == design and mine <= MATCH_MAX:
        return True, f"photos match (distance {mine})"
    return False, f"photos look like '{best}' ({sc[best]}), not this design ({mine})"


def main():
    ap = argparse.ArgumentParser(description="Make videos for new designs and put them on their tee + sweatshirt Etsy listings.")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, upload nothing")
    ap.add_argument("--only", nargs="*", help="restrict to these design names")
    ap.add_argument("--use", nargs="*", default=[], metavar="DESIGN=LISTING_ID",
                    help="listings you found yourself, e.g. nap=4551234567")
    ap.add_argument("--existing", action="store_true",
                    help="also cover designs with no file in assets/designs/ but a mockup folder in output/<garment>/")
    args = ap.parse_args()

    has_file = {p.stem for p in gather_files(DESIGNS_DIR)}
    designs = sorted(has_file, key=str.lower)
    if args.existing:
        folders = {p.name for g in GARMENTS for p in (OUTPUT / g).iterdir() if p.is_dir()}
        designs += sorted(folders - has_file, key=str.lower)
    if args.only:
        designs = [d for d in designs if d in set(args.only)]
    if not designs:
        sys.exit(f"No designs in {DESIGNS_DIR}/")
    manual = {}
    for pair in args.use:
        design, _, lid = pair.partition("=")
        if not lid.isdigit():
            sys.exit(f"--use needs DESIGN=LISTING_ID, got '{pair}'")
        manual.setdefault(design, []).append(int(lid))

    ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {}
    print(f"{len(designs)} design(s): {', '.join(designs)}")
    print("Reading Printify products and Etsy listings…")
    by_name, garment_of = printify_listings()
    etsy = EtsyClient()
    shop_id = etsy.get_shop_id()
    active = {l["listing_id"]: l for l in etsy.get_active_listings(shop_id)}

    def garment_for(lid: int) -> str | None:
        if lid in garment_of:
            return garment_of[lid]
        title = (active.get(lid) or etsy.get_listing(lid)).get("title", "")
        return next((g for g, cfg in GARMENTS.items() if cfg["is_title"](title)), None)

    def title_of(lid: int) -> str:
        return html.unescape((active.get(lid) or {}).get("title", ""))[:60]

    # ── 1. Candidate listings per design × garment ─────────────────────────
    # rows: one per (design, garment, listing) — status "ok" / "done" / "no"
    rows, search = [], []
    for design in designs:
        picked = {g: [] for g in GARMENTS}
        for lid in manual.get(design, []):
            g = garment_for(lid)
            if g is None:
                sys.exit(f"--use {design}={lid}: can't tell if that listing is a tee or a sweatshirt")
            picked[g].append(lid)
        for g in GARMENTS:
            if picked[g]:
                rows += [{"design": design, "garment": g, "listing": lid, "how": "you picked it (--use)"}
                         for lid in picked[g]]
            elif by_name[g].get(design):
                rows += [{"design": design, "garment": g, "listing": lid, "how": "Printify name"}
                         for lid in by_name[g][design]]
            else:
                search.append((design, g))

    def precheck(r: dict) -> bool:
        """Mark listings that are done or can't take a video. True = still needs one."""
        lid = r["listing"]
        if str(lid) in ledger:
            r.update(status="done", why="video added earlier (ledger)")
        elif lid not in active:
            r.update(status="no", why=f"listing is {etsy.get_listing(lid).get('state')}, not active")
        elif etsy.get_listing_videos(lid):
            r.update(status="done", why="listing already has a video")
        else:
            return True
        return False

    pending = [r for r in rows if precheck(r)]
    covered = {(r["design"], r["garment"]) for r in rows if r.get("status") == "done"}

    # ── 2. Mockups for every design × garment still open ───────────────────
    # No design file → the saved mockups in output/<garment>/<design>/ are used as-is.
    # Neither → nothing to compare or film, so that pair needs help.
    blind = {(d, g) for d, g in {(r["design"], r["garment"]) for r in pending} | set(search)
             if d not in has_file and not (OUTPUT / g / d).is_dir()}
    for r in pending:
        if (r["design"], r["garment"]) in blind:
            r.update(status="no", why=f"no design file in {DESIGNS_DIR}/ and no mockups in output/{r['garment']}/")
    pending = [r for r in pending if (r["design"], r["garment"]) not in blind]
    for d, g in [p for p in search if p in blind]:
        rows.append({"design": d, "garment": g, "listing": "-", "status": "no", "how": "-",
                     "why": f"no design file in {DESIGNS_DIR}/ and no mockups in output/{g}/"})
    search = [p for p in search if p not in blind]

    open_pairs = {(r["design"], r["garment"]) for r in pending} | set(search)
    if any(d in has_file for d, _ in open_pairs):
        upscale_designs(designs_dir=DESIGNS_DIR, output_dir=FINAL_DESIGNS_DIR, img_exts=IMG_EXTS)
    photos = RemotePhotos(etsy)
    local = {}
    for g in GARMENTS:
        need = sorted(d for d, gg in open_pairs if gg == g)
        render = [d for d in need if d in has_file]
        if render:
            render_mockups(g, render)
        if need:
            local[g] = local_fingerprints(g)

    # ── 3a. Picture check on the Printify / --use candidates ───────────────
    for r in pending:
        ok, why = verdict(scores(photos.of(r["listing"]), local[r["garment"]]), r["design"])
        if r["how"].startswith("you picked"):
            r.update(status="ok", why=f"{r['how']} — {why}")
        else:
            r.update(status="ok" if ok else "no", why=why)
    for design, g in {(r["design"], r["garment"]) for r in pending}:
        mine = [r for r in rows if r["design"] == design and r["garment"] == g]
        if not any(r["status"] in ("ok", "done") for r in mine) and (design, g) not in covered:
            search.append((design, g))                # name pointed at the wrong listing

    # ── 3b. Photo search across every active listing of the garment ────────
    found = []
    for g in GARMENTS:
        wanted = sorted(d for d, gg in search if gg == g)
        if not wanted:
            continue
        pool = [lid for lid in active if garment_for(lid) == g]
        print(f"\nPhoto search: {len(wanted)} design(s) across {len(pool)} active {g} listings…")
        best_of = {}
        for lid in pool:
            sc = scores(photos.of(lid), local[g])
            if sc:
                best = min(sc, key=sc.get)
                if sc[best] <= MATCH_MAX:
                    best_of[lid] = (best, sc[best])
        for design in wanted:
            hits = [(lid, d) for lid, (b, d) in best_of.items() if b == design]
            if len(hits) != 1:
                why = ("no listing's photos match this design" if not hits else
                       "several listings match: " + ", ".join(str(l) for l, _ in hits))
                rows.append({"design": design, "garment": g, "listing": "-", "status": "no",
                             "how": "photo search", "why": why})
                continue
            lid, d = hits[0]
            r = {"design": design, "garment": g, "listing": lid, "how": "photo search"}
            rows.append(r)
            if precheck(r):
                r.update(status="ok", why=f"found by photo search (distance {d})")
    photos.save()

    # ── 4. Fresh videos for everything that will be uploaded ───────────────
    ready = [r for r in rows if r.get("status") == "ok"]
    videos = {}
    for key in sorted({(r["garment"], r["design"]) for r in ready}):
        videos[key] = make_video(*key)
    for r in ready:
        r["video"] = videos[(r["garment"], r["design"])]
        if r["video"] is None:
            r.update(status="no", why="video could not be made")
    ready = [r for r in ready if r["status"] == "ok"]

    # ── Report ─────────────────────────────────────────────────────────────
    print(f"\n{'DESIGN':28} {'GARMENT':11} {'LISTING':11} RESULT")
    mark = {"ok": "✅", "done": "✔️ ", "no": "⏭️ "}
    for r in sorted(rows, key=lambda r: (r["design"].lower(), r["garment"])):
        detail = f"{title_of(r['listing'])}  [{r['why']}]" if r["status"] == "ok" else r["why"]
        print(f"{mark[r['status']]} {r['design']:26} {r['garment']:11} {str(r['listing']):11} {detail}")

    done = []
    if args.dry_run:
        print(f"\nDRY RUN — would upload {len(ready)} video(s). Nothing was uploaded.")
    else:
        for r in ready:
            try:
                v = etsy.upload_listing_video(shop_id, r["listing"], r["video"])
            except EtsyError as e:
                log(f"  FAIL {r['listing']}  {r['garment']} {r['design']}  {e}")
                r.update(status="no", why=f"upload failed: {e}")
                continue
            ledger[str(r["listing"])] = {"design": r["design"], "garment": r["garment"],
                                         "video_id": v.get("video_id"),
                                         "uploaded_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"}
            LEDGER.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
            log(f"  added video {r['garment']}/{r['design']}.mp4 → {r['listing']}  — {title_of(r['listing'])}")
            done.append(r)
            time.sleep(PAUSE)
        print(f"\nUploaded {len(done)} video(s) (Etsy takes a few minutes to process each):")
        for r in done:
            print(f"  {r['garment']:11} {r['design']:26} https://www.etsy.com/listing/{r['listing']}")

    # A design × garment needs help when none of its rows ended uploaded/done.
    settled = {(r["design"], r["garment"]) for r in rows if r["status"] == "done"} \
            | {(r["design"], r["garment"]) for r in done} \
            | ({(r["design"], r["garment"]) for r in ready} if args.dry_run else set())
    stuck = [(d, g) for d in designs for g in GARMENTS if (d, g) not in settled]
    if stuck:
        print("\n!! NEEDS YOUR HELP — nothing was uploaded for these !!")
        for d, g in stuck:
            whys = [f"{r['listing']}: {r['why']}" for r in rows
                    if r["design"] == d and r["garment"] == g and r["status"] == "no"]
            print(f"  ✗ {d:26} {g:11} {' | '.join(whys)}")
        print("  Found the listing yourself? Re-run with --use DESIGN=LISTING_ID")


if __name__ == "__main__":
    main()
