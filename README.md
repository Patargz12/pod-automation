# POD Automation — Nullish

Turns raw design PNGs into Printify products and Etsy listings (plus Pinterest pins), for **t-shirts** and **sweatshirts**.

## 👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕👕 👕 T-SHIRT

Bella+Canvas 3001 · Monster Digital · Black, Dark Grey, Maroon, Navy, Brown

## Option A — Manual (fix positioning in Printify yourself)

```
Step 1:  python -m scripts.create.tshirt --clear-ledger

Step 2:  python -m scripts.create.tshirt --skip-upscale     ← if designs are in assets/designs_final/
         python -m scripts.create.tshirt                    ← if designs are in assets/designs/

Step 3:  Open Printify → fix design positioning → click Publish

Step 4:  python -m scripts.create.marketing_tshirt --clear-ledger

Step 5:  python -m scripts.create.marketing_tshirt          ← stays in Etsy Drafts, you click Publish
         python -m scripts.create.marketing_tshirt_publish  ← same, but auto-publishes on Etsy (💸 $0.20 each)
Step 6:  python -m scripts.etsy.fix_shipping --apply 
```

Result: Etsy listing with our mockups (Black_Shirt.png as main photo), renewal set to Automatic. With `marketing_tshirt` it stays a **draft** and you publish on Etsy yourself; with `marketing_tshirt_publish` it goes **live** automatically (Pinterest not included).

## Option B — One shot (fully automatic, goes LIVE)

```
Step 1:  Put ONLY the design(s) you want in  assets/designs/

Step 2:  python -m scripts.create.tshirt_automation
```

-- NOTES -- 

- If you will do one shot, make sure to fix the print location then reactivate in from Drafts in Etsy
- Make sure to only toggle "Colors, Sizes, Prices and SKU" only, we only want to fix design positioning


Result: live Etsy listing + size/print/color charts + slideshow video + Pinterest pin. 💸 $0.20 Etsy fee per new listing

```
python -m scripts.create.tshirt_automation --skip-video          ← no video
python -m scripts.create.tshirt_automation --skip-info-images    ← no size/print/color charts
python -m scripts.create.tshirt_automation --extras-only         ← add charts + video to live listings missing them
```

Only the images in `assets/designs/` go through the pipeline. Everything else in `assets/designs_final/` is ignored. Don't run `--clear-ledger` for this; it isn't needed.




## 🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥🧥 SWEATSHIRT

Gildan 18000 · Printful · DTF · Sport Grey, Black, Maroon, Sand, Navy, Dark Chocolate

## Option A — Manual (fix positioning in Printify yourself)

```
Step 1:  python -m scripts.create.sweatshirt --clear-ledger

Step 2:  python -m scripts.create.sweatshirt --skip-upscale  ← if designs are in assets/designs_final/
         python -m scripts.create.sweatshirt                 ← if designs are in assets/designs/

Step 3:  Open Printify → fix design positioning → click Publish

Step 4:  python -m scripts.create.marketing_sweatshirt --clear-ledger

Step 5:  python -m scripts.create.marketing_sweatshirt          ← stays in Etsy Drafts, you click Publish
         python -m scripts.create.marketing_sweatshirt_publish  ← same, but auto-publishes on Etsy (💸 $0.20 each)
Step 6:  python -m scripts.etsy.fix_shipping --apply 

```

Result: Etsy listing with our mockups (Black_Sweatshirt.png as main photo), renewal set to Automatic. With `marketing_sweatshirt` it stays a **draft** and you publish on Etsy yourself; with `marketing_sweatshirt_publish` it goes **live** automatically (Pinterest not included).

## Option B — One shot (fully automatic, goes LIVE)

```
python -m scripts.create.sweatshirt_automation
```

Result: live Etsy listing + size/print/color charts + slideshow video + Pinterest pin. 💸 $0.20 Etsy fee per new listing.

```
python -m scripts.create.sweatshirt_automation --skip-video          ← no video
python -m scripts.create.sweatshirt_automation --skip-info-images    ← no size/print/color charts
python -m scripts.create.sweatshirt_automation --extras-only         ← add charts + video to live listings missing them
```






## 🚀 Quick start — new designs to live listings

```
1. Drop your design images into  assets/designs/

2. Run ONE of these from the project folder:
     python -m scripts.create.tshirt_automation        ← t-shirts
     python -m scripts.create.sweatshirt_automation    ← sweatshirts

3. Done: upscaled → mockups → Printify → live on Etsy → pinned on Pinterest
```

Add `--dry-run` to either command to preview everything without creating or paying for anything.

> **How to run scripts:** always from the project folder, as `python -m scripts.<folder>.<script>` (no `.py` at the end). The old style `python tshirt_automation.py` no longer works.

Both garments share the same input folders:

- `assets/designs/` → drop **raw** designs here (they get upscaled 4x)
- `assets/designs_final/` → upscaled designs land here automatically (or drop already-upscaled ones here and use `--skip-upscale` / `--no-upscale`)

---

.

## All t-shirt commands

**`tshirt`** — mockups + create product on Printify (not published)

| Command | What it does |
| --- | --- |
| `python -m scripts.create.tshirt` | Upscale → mockups → create on Printify |
| `python -m scripts.create.tshirt --skip-upscale` | Use `assets/designs_final/` as-is |
| `python -m scripts.create.tshirt --no-publish` | Mockups only, skip Printify |
| `python -m scripts.create.tshirt --publish` | Also publish to Etsy Drafts |
| `python -m scripts.create.tshirt --dry-run` | Preview the Printify step, create nothing |
| `python -m scripts.create.tshirt --reupscale` | Force re-upscale |
| `python -m scripts.create.tshirt --setup` | Only run the corner picker |
| `python -m scripts.create.tshirt --reset` | Clear t-shirt corners |
| `python -m scripts.create.tshirt --clear-ledger` | Wipe ALL t-shirt ledgers (published, marketed, pinned) |
| `python -m scripts.create.tshirt --help` | Full help |

**`marketing_tshirt`** — swap Printify's images for ours on Etsy drafts

| Command | What it does |
| --- | --- |
| `python -m scripts.create.marketing_tshirt` | Swap mockups on every new t-shirt draft |
| `python -m scripts.create.marketing_tshirt --dry-run` | Preview, change nothing |
| `python -m scripts.create.marketing_tshirt --forget "name.png"` | Redo that design next run |
| `python -m scripts.create.marketing_tshirt --clear-ledger` | Wipe `marketed_tshirt.json` |
| `python -m scripts.create.marketing_tshirt --help` | Full help |

**`marketing_tshirt_publish`** — same mockup swap, then publishes the drafts on Etsy

| Command | What it does |
| --- | --- |
| `python -m scripts.create.marketing_tshirt_publish` | Swap mockups + take every marketed draft live (💸 $0.20 each) |
| `python -m scripts.create.marketing_tshirt_publish --dry-run` | Preview, change nothing |
| `python -m scripts.create.marketing_tshirt_publish --allow-placeholder` | Also publish designs with placeholder AI copy |

**`tshirt_automation`** — one shot: Printify → Etsy (live) → Pinterest

| Command | What it does |
| --- | --- |
| `python -m scripts.create.tshirt_automation` | The whole thing |
| `python -m scripts.create.tshirt_automation --dry-run` | Trace everything, change nothing |
| `python -m scripts.create.tshirt_automation --only "name.png"` | Only this design |
| `python -m scripts.create.tshirt_automation --skip-pinterest` | Stop after the listing is live |
| `python -m scripts.create.tshirt_automation --no-upscale` | Use the file in `assets/designs/` as-is (already high-res) |
| `python -m scripts.create.tshirt_automation --forget "name.png"` | Redo stages 4–8 for that design |
| `python -m scripts.create.tshirt_automation --clear-ledger` | Wipe `pinned_tshirt.json` |
| `python -m scripts.create.tshirt_automation --allow-placeholder` | Publish even without AI copy |
| `python -m scripts.create.tshirt_automation --all` | Also finish every unfinished design already in the ledger |
| `python -m scripts.create.tshirt_automation --help` | Full help |

**T-shirt files**

- Templates: `assets/thumbnails/Shirts/` (one image per color)
- Mockups: `output/tshirt/<design>/<Color>_Shirt.png`
- Pin image: `output/final_thumbnail/<design>_Black_Shirt.png`
- Ledgers: `caching/published.json`, `caching/marketed_tshirt.json`, `caching/pinned_tshirt.json`

---


## All sweatshirt commands

**`sweatshirt`** — mockups + create product on Printify (not published)

| Command | What it does |
| --- | --- |
| `python -m scripts.create.sweatshirt` | Upscale → mockups → create on Printify |
| `python -m scripts.create.sweatshirt --skip-upscale` | Use `assets/designs_final/` as-is |
| `python -m scripts.create.sweatshirt --no-publish` | Mockups only, skip Printify |
| `python -m scripts.create.sweatshirt --publish` | Also publish to Etsy Drafts |
| `python -m scripts.create.sweatshirt --dry-run` | Preview the Printify step, create nothing |
| `python -m scripts.create.sweatshirt --reupscale` | Force re-upscale |
| `python -m scripts.create.sweatshirt --setup` | Only run the corner picker |
| `python -m scripts.create.sweatshirt --reset` | Clear sweatshirt corners |
| `python -m scripts.create.sweatshirt --update-listings` | Regenerate AI title/description/tags and republish |
| `python -m scripts.create.sweatshirt --reconcile` | Forget ledger entries deleted on Printify |
| `python -m scripts.create.sweatshirt --clear-ledger` | Wipe `published_sweatshirt.json` |
| `python -m scripts.create.sweatshirt --forget "name.png"` | Forget that design |
| `python -m scripts.create.sweatshirt --help` | Full help |

**`marketing_sweatshirt`** — swap Printify's images for ours on Etsy drafts

| Command | What it does |
| --- | --- |
| `python -m scripts.create.marketing_sweatshirt` | Swap mockups on every new sweatshirt draft |
| `python -m scripts.create.marketing_sweatshirt --dry-run` | Preview, change nothing |
| `python -m scripts.create.marketing_sweatshirt --forget "name.png"` | Redo that design next run |
| `python -m scripts.create.marketing_sweatshirt --reconcile` | Drop entries whose Etsy listing is gone |
| `python -m scripts.create.marketing_sweatshirt --clear-ledger` | Wipe `marketed_sweatshirt.json` |
| `python -m scripts.create.marketing_sweatshirt --help` | Full help |

**`marketing_sweatshirt_publish`** — same mockup swap, then publishes the drafts on Etsy

| Command | What it does |
| --- | --- |
| `python -m scripts.create.marketing_sweatshirt_publish` | Swap mockups + take every marketed draft live (💸 $0.20 each) |
| `python -m scripts.create.marketing_sweatshirt_publish --dry-run` | Preview, change nothing |
| `python -m scripts.create.marketing_sweatshirt_publish --allow-placeholder` | Also publish designs with placeholder AI copy |

**`sweatshirt_automation`** — one shot: Printify → Etsy (live) → Pinterest

| Command | What it does |
| --- | --- |
| `python -m scripts.create.sweatshirt_automation` | The whole thing |
| `python -m scripts.create.sweatshirt_automation --dry-run` | Trace everything, change nothing |
| `python -m scripts.create.sweatshirt_automation --only "name.png"` | Only this design |
| `python -m scripts.create.sweatshirt_automation --skip-pinterest` | Stop after the listing is live |
| `python -m scripts.create.sweatshirt_automation --connect-pinterest` | Pinterest login + "Give access" in the browser first, then run (for the API review video) |
| `python -m scripts.create.sweatshirt_automation --no-upscale` | Use `assets/designs_final/` as-is |
| `python -m scripts.create.sweatshirt_automation --forget "name.png"` | Redo stages 4–8 for that design |
| `python -m scripts.create.sweatshirt_automation --clear-ledger` | Wipe `pinned_sweatshirt.json` |
| `python -m scripts.create.sweatshirt_automation --allow-placeholder` | Publish even without AI copy |
| `python -m scripts.create.sweatshirt_automation --all` | Ignore folder scope, use whole ledger |
| `python -m scripts.create.sweatshirt_automation --help` | Full help |

**Sweatshirt files**

- Templates: `assets/thumbnails/Sweatshirts/` (one image per color)
- Mockups: `output/sweatshirt/<design>/<Color>_Sweatshirt.png`
- Pin image: `output/final_thumbnail/<design>_Black_Sweatshirt.png`
- Ledgers: `caching/published_sweatshirt.json`, `caching/marketed_sweatshirt.json`, `caching/pinned_sweatshirt.json`

---

# 🛒 ETSY — fixing listings that are already live

All of these are in `scripts/etsy/`. **They are a dry run by default** — they only show what they would change. Add `--apply` to actually change Etsy.

| Command | What it does |
| --- | --- |
| `python -m scripts.etsy.audit --new` | Check new live listings for title/tag/price/section problems (start here) |
| `python -m scripts.etsy.audit --accept` | Mark every clean listing as reviewed |
| `python -m scripts.etsy.retag --apply` | Fix tags |
| `python -m scripts.etsy.retitle --apply` | Fix titles |
| `python -m scripts.etsy.redescribe --apply` | Rewrite descriptions with AI (10 at a time, `--limit N` to change) |
| `python -m scripts.etsy.section --apply` | Put listings into shop sections |
| `python -m scripts.etsy.renew --apply` | Turn auto-renew ON for every live listing |
| `python -m scripts.etsy.set_price --listing ID --apply` | Price a listing to the net-profit target |
| `python -m scripts.etsy.add_guide --image Shirt_Guide.png --apply` | Add a size/print/color chart from `assets/listing_extras/` to every live listing |
| `python -m scripts.etsy.classify_dev --apply` | Mark which designs are developer jokes (from the artwork) |
| `python -m scripts.etsy.thumbnail_check` | Show how listing thumbnails look at Etsy search size |

**Shipping fixes** (rarely needed — each makes a backup and has `--restore BACKUP.json` to undo):

| Command | What it does |
| --- | --- |
| `python -m scripts.etsy.set_profile --from ID --to ID --apply` | Move listings from one shipping profile to another |
| `python -m scripts.etsy.fix_shipping --apply` | Convert shipping rates from USD figures into real pesos |
| `python -m scripts.etsy.fix_destinations --apply` | Raise shipping rows that charge less than the real postage |
| `python -m scripts.etsy.unfree_shipping --apply` | Take listings off the ₱0.00 free-shipping profile |
| `python -m scripts.etsy.fix_profile --apply` | Move the one orphaned tee onto its siblings' profile |

Every script has `--help` for the full option list.

---

# 🖨️ PRINTIFY

| Command | What it does |
| --- | --- |
| `python -m scripts.printify.reprice --garment tee` | Preview pushing the price ladder onto existing products |
| `python -m scripts.printify.reprice --garment tee --apply` | Do it (`--garment sweatshirt` for sweatshirts) |

⚠️ Don't add `--publish` without checking first — republishing a hidden Printify product can switch its Etsy listing off.

---

# 📌 PINTEREST

**Pin listings that are already live on Etsy:**

```
1. Put the images in  assets/to_pin/   (name them like  <design>_Black_Shirt.png  or  <design>_Black_Sweatshirt.png)
2. python -m scripts.pinterest.pin_folder --dry-run    ← preview
3. python -m scripts.pinterest.pin_folder              ← create the pins
```

Pin just one image with `--only <file name>`. Already-pinned images are skipped (`caching/pinned_folder.json`).

Check the pins really exist: `python -m scripts.auth.pinterest_verify` (add `--boards` to list your boards).

---

# ⚙️ SHARED — setup & reference

## One-time setup

1. Install dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

2. Download Real-ESRGAN-ncnn-vulkan from https://github.com/xinntao/Real-ESRGAN/releases and extract it into `bin/realesrgan/` (the `.exe` plus a `models/` folder next to it).

3. Copy `.env.example` → `.env` and fill in: `PRINTIFY_API_TOKEN`, `ETSY_KEYSTRING`, `ETSY_SHARED_SECRET`, `GEMINI_API_KEY_1..5`, `PINTEREST_APP_ID`, `PINTEREST_APP_SECRET`.

4. Log in once:

   ```bash
   python -m scripts.auth.etsy_auth        # → caching/.etsy_tokens.json
   python -m scripts.auth.pinterest_auth   # → caching/.pinterest_tokens.json
   ```

Check your remaining Gemini quota any time: `python -m scripts.auth.check_gemini`

## Corner picker (first run only)

Each garment stores ONE print-area config in `config.json`, shared by all its colors. If none exists, a window opens — click the 4 corners of the print area:

```
1. Top-Left  →  2. Top-Right  →  3. Bottom-Right  →  4. Bottom-Left
```

| Key | Action |
| --- | --- |
| Left click | Place corner |
| `Ctrl+Z` / `Backspace` / `U` | Undo last point |
| `R` | Reset all points |
| `Enter` | Confirm (requires 4 points) |
| `Esc` | Skip |

Re-pick with `python -m scripts.create.tshirt --reset` or `python -m scripts.create.sweatshirt --reset`.

## Folder structure

```
pod-automation/
├── assets/                   # IMAGES YOU PROVIDE
│   ├── designs/              #   ← YOU drop raw designs here
│   ├── designs_final/        #   4x upscaled designs (auto-filled)
│   ├── thumbnails/
│   │   ├── Shirts/           #   t-shirt color templates
│   │   └── Sweatshirts/      #   sweatshirt color templates
│   ├── listing_extras/       #   size / print / color charts for add_guide
│   ├── to_pin/               #   images for pin_folder (Pinterest)
│   └── mockups/              #   spare mockup images
├── output/                   # IMAGES THE SCRIPTS MAKE
│   ├── tshirt/               #   t-shirt mockups, one folder per design
│   ├── sweatshirt/           #   sweatshirt mockups, one folder per design
│   └── final_thumbnail/      #   black hero renders, used as Pinterest pin images
├── scripts/                  # EVERYTHING YOU RUN
│   ├── create/               #   new products: tshirt, sweatshirt, *_automation, marketing_*
│   ├── etsy/                 #   fix live listings: audit, retag, retitle, redescribe, ...
│   ├── printify/             #   reprice
│   ├── pinterest/            #   pin_folder
│   └── auth/                 #   logins + checks: etsy_auth, pinterest_auth, check_gemini
├── services/                 # the code that talks to Etsy, Printify, Pinterest, Gemini
├── caching/                  # ledgers, tokens, backups (gitignored — don't delete)
├── docs/                     # notes
├── bin/realesrgan/           # Real-ESRGAN upscaler
└── config.json               # saved corner coordinates (don't edit by hand)
```

## How the mockups are made

- **Upscale** — Real-ESRGAN 4x (`realesr-general-x4v3`, same as Upscayl Lite). Designs already in `assets/designs_final/` are skipped.
- **Warp** — the design is padded to the print area's aspect ratio (never stretched) and perspective-mapped onto your 4 corners.
- **Blend** — dark shirt → screen blend (vivid colors); lighter shirt → multiply blend (ink-on-fabric look).
- Supported inputs: `.png`, `.jpg`, `.jpeg`, `.webp`. Outputs are always `.png`.


## Old t-shirt script

`scripts/create/execute.py` is the original t-shirt pipeline. Don't run it — use `python -m scripts.create.tshirt` instead. It stays because the other scripts reuse its helpers.
