# Pinterest Autopilot

Posts 3 pins a day to Pinterest on its own, for free, using GitHub Actions.

- **Your PC (manual, in batches):** `prepare` puts each design on your shirt photo, writes the pin text and adds it to `queue.json`.
- **GitHub (automatic):** 3 times a day the workflow posts the next pin in the queue.

## Folder

| Path | What it is |
|---|---|
| `Designs_To_Use/` | design PNGs to pin (transparent background) |
| `Mockup/` | your own shirt photos. Each new photo asks you to draw the print area once. |
| `ready/` | rendered pin images. Commit these, because the cloud uploads them. |
| `queue.json` | pins waiting, posted or failed. The cloud updates it after every post. |
| `mockup_corners.json` | saved print area for each mockup photo |
| `listing_map.json` | manual links from design to Etsy listing, e.g. `{ "nap.png": 4601741693 }` |
| `../.github/workflows/pinterest-autopost.yml` | the schedule. GitHub only reads workflows from that folder. |

## Commands

```
python -m scripts.pinterest_marketing prepare --dry-run   # preview: render + pin text, queue nothing
python -m scripts.pinterest_marketing prepare             # add new designs to the queue
python -m scripts.pinterest_marketing status              # how many left, days of runway
python -m scripts.pinterest_marketing post --dry-run      # show the next pin, post nothing
python -m scripts.pinterest_marketing post                # post the next pin right now from your PC
```

## Going live (one time)

1. **Refresh the token locally.** Run `prepare` once, which refreshes the Pinterest token automatically.
   If it says the token is dead, run `python -m scripts.auth.pinterest_auth`.
2. **Add 4 repository secrets.** Go to GitHub → repo → Settings → Secrets and variables → Actions → *Secrets*:
   - `PINTEREST_APP_ID` and `PINTEREST_APP_SECRET`: the same values as in `.env`
   - `PINTEREST_TOKENS_JSON`: paste the whole contents of `caching/.pinterest_tokens.json`
   - `GH_PAT`: a fine-grained GitHub token for this repo only, with the permission **Secrets: Read and write**.
     The workflow uses it to save the Pinterest token whenever it refreshes (about every 27 days).
3. **Commit and push**: `Pinterest_Market/` (including `ready/`), `scripts/pinterest_marketing.py`,
   `services/`, and `.github/workflows/pinterest-autopost.yml`.
4. **Turn it on.** On the same settings page, open the *Variables* tab and add `PINTEREST_AUTOPOST` = `on`.
   To pause, change it to `off`.
5. Optional test: Actions tab → *Pinterest autopost* → *Run workflow* posts 1 pin now.

## Good to know

- **Every design in the folder gets pinned.** If a design has an exact live Etsy listing, the pin links to it.
  If not, the pin links to the shop page (`SHOP_URL` in `config.py`). To point a design at a specific listing,
  add `"design.png": <listing_id>` to `listing_map.json` before running `prepare`.
- **Runway** = designs × mockup photos ÷ 3 days. For example, 150 designs × 1 photo ≈ 50 days, and a 2nd photo doubles it.
- **Order.** Pins go out in queue order, and the same design never goes out twice in a row.
- **Failed pins** are marked `failed` with the reason (see `status`), and the next run moves on.
- **Timing.** GitHub's schedule can run 5–30 minutes late. That's normal.
- **Keep running `prepare` locally, and keep the token secret in sync.** `prepare` uses your local token, and if the
  token in the secret has been rotated, re-paste `caching/.pinterest_tokens.json` into `PINTEREST_TOKENS_JSON`
  whenever a local re-auth happens.
