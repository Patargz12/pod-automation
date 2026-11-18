# Automation Phase 3 — One-Command Pipeline (Designs → Etsy → Pinterest)

**Status:** ✅ implemented — untested against live APIs
**Command:** `python -m scripts.create.sweatshirt_automation`

> Implemented in [sweatshirt_automation.py](sweatshirt_automation.py),
> [services/pinterest_service.py](services/pinterest_service.py) and
> [pinterest_auth.py](pinterest_auth.py), plus additions to `ai_content`,
> `etsy_service` and `printify_service`. Decisions taken where §7 left them open:
> **two boards** with AI-vs-programmer classification (board names overridable in
> `.env`, auto-created in sandbox); **Etsy-hosted image URL** for the pin with a
> local-file base64 fallback; **existing square renders** — no vertical pass yet.
> The `ai_used` placeholder guard from §8 is implemented and on by default.

---

## 1. Objective

Collapse the current four-step manual workflow into a single unattended command that
takes raw designs all the way to a **live Etsy listing** and a **published Pinterest
pin** that links back to it.

Today's flow (from [README.md](README.md)):

```
python -m scripts.create.sweatshirt --clear-ledger
python -m scripts.create.sweatshirt --skip-upscale
  → MANUAL: check Printify, fix design positioning, click Publish
python -m scripts.create.marketing_sweatshirt --clear-ledger
python -m scripts.create.marketing_sweatshirt
  → MANUAL: review each Etsy draft, click Publish
```

Target flow:

```
python -m scripts.create.sweatshirt_automation
```

Both manual gates are removed by explicit decision — the default Printify positioning
is now correct for the integrated product, so per-design positioning review is no
longer needed.

---

## 2. Expected behavior — stage by stage

The orchestrator runs these in order, per design, isolating failures so one bad design
never sinks the batch (same convention as [printify_service.py:942](services/printify_service.py#L942)).

### Stage 0 — Preflight
Verify every credential and folder up front, before any billable action:
`PRINTIFY_API_TOKEN`, `ETSY_KEYSTRING` + `ETSY_SHARED_SECRET`,
`caching/.etsy_tokens.json`, `caching/.pinterest_tokens.json`, at least one
`GEMINI_API_KEY_*`, and the presence of designs. Abort loudly if anything is missing.

### Stage 1 — Render mockups
Reuse the existing `sweatshirt.py` path with `--skip-upscale` semantics: read
`assets/designs_final/`, render every color template into `output/sweatshirt/{design}/`, and
refresh `output/final_thumbnail/{design}_Black_Sweatshirt.png` via
`collect_final_thumbnails()` ([sweatshirt.py:197](sweatshirt.py#L197)).

> `output/final_thumbnail/` is currently empty — it self-populates here, before Pinterest
> ever reads from it.

### Stage 2 — Printify: create + publish
Gemini generates the SEO title, description and tags from the design image
(`generate_listing_content()`, [ai_content.py:282](services/ai_content.py#L282)).
The product is created and then published with `publish_product()`
([printify_service.py:376](services/printify_service.py#L376)) — equivalent to today's
`--publish` flag. Products already in `caching/published_sweatshirt.json` are skipped.

### Stage 3 — Wait for the Etsy listing to exist
Printify → Etsy sync is **asynchronous**. Poll `GET /products/{id}.json` until
`external.id` is populated — that value *is* the Etsy listing id. The listing arrives
in **Draft** state.

Polling needs a backoff and a hard timeout. On timeout the design is recorded as
`awaiting_etsy` and picked up on the next run rather than failing outright.

### Stage 4 — Swap in our mockups
Existing `marketing_sweatshirt.py` logic: delete Printify's default images, upload our
renders from `output/sweatshirt/{design}/` with `Black_Sweatshirt.png` at rank 1, and
flip renewal to Automatic.

**Must happen before activation** — otherwise the listing goes live carrying
Printify's default mockups.

### Stage 5 — Align tags (see §3)

### Stage 6 — Activate the Etsy listing
`PATCH /application/shops/{shop}/listings/{listing}` with `state=active`, using the
same form-encoded pattern as `set_auto_renew()`
([etsy_service.py:241](services/etsy_service.py#L241)). The existing `listings_w`
scope already covers this.

> **This charges Etsy's $0.20 listing fee, per listing, unattended.**

### Stage 7 — Confirm the link is live
Poll `get_listing()` until `state == "active"`, then construct
`https://www.etsy.com/listing/{listing_id}`. Pinterest is not touched until this
succeeds — pinning a draft listing produces a dead link.

### Stage 8 — Pinterest SEO agent
A new Gemini call (a sibling of `generate_listing_content()`) looks at the design and
produces Pinterest-shaped copy:

| Field | Constraint | Notes |
|---|---|---|
| `title` | ≤ 100 chars | Pinterest truncates hard |
| `description` | ≤ 800 chars | keywords live in prose — Pinterest has no tag field |
| `alt_text` | ≤ 500 chars | accessibility + SEO |
| `board` | classification | AI humor vs. programming humor |

The same call classifies the design so it lands on the correct board.

### Stage 9 — Create the pin
`POST /v5/pins` with the board id, the copy from Stage 8, the image, and
`link` = the live Etsy URL from Stage 7.

### Stage 10 — Ledger
Record the design in `caching/pinned_sweatshirt.json` (mirroring
`marketed_sweatshirt.json`) so re-runs skip it. Written immediately per design, not
batched at the end.

---

## 3. Tag alignment — Printify ↔ Etsy

### The problem
Today you manually toggle **Tags** in Printify before publishing so the tags carry
over to Etsy. That toggle is a human step, and an unattended run cannot depend on it.

Worth noting: `publish_product()` **already sends `"tags": True`**
([printify_service.py:379](services/printify_service.py#L379)). The API side is
correct. But Printify's Etsy tag sync is gated by shop-level publishing settings and
is not reliable enough to be the source of truth for an unattended pipeline.

### The approach: don't trust the sync — verify and repair

After the Etsy listing exists (Stage 5), and **before** activation:

1. `GET /products/{id}.json` from Printify → read the product's `tags` array.
2. `GET /application/listings/{id}` from Etsy → read the listing's `tags`.
3. Compare as **sets** (order is not meaningful on either platform).
4. If they differ, `PATCH` the Etsy listing with the Printify list. Same
   form-encoded `updateListing` endpoint used by `set_auto_renew()`.
5. Log whether a repair was needed, so you can see how often the sync actually fails.

This makes the manual Printify toggle irrelevant. Even when the sync silently drops
tags, the listing ends up correct.

### Why read back from Printify instead of regenerating

Regenerating with Gemini would produce a *different* tag set — the two platforms would
drift apart, which is exactly the failure we're preventing. The Printify product is the
only durable record: the ledger currently stores only `design`, `product_id`,
`shop_id`, `published`, `created_at`
([printify_service.py:931-937](services/printify_service.py#L931-L937)) — **no tags**.

### Etsy tag rules are already satisfied

`_sanitize_tags()` ([ai_content.py:246](services/ai_content.py#L246)) already enforces
Etsy's limits before tags ever reach Printify: max 13 tags, max 20 chars each,
lowercased, deduped, `#` stripped
([ai_content.py:39-40](services/ai_content.py#L39-L40)). No new validation needed —
the tags on the Printify product are Etsy-legal by construction.

### Optional hardening
Persist the tag list into the ledger at creation time. Costs nothing, and gives a
local source of truth if a Printify product is ever deleted.

---

## 4. Pinterest specifics

### Access tier reality
The app is on **Trial access**. Pins created under Trial are **sandbox entities visible
only to you** — the API returns a real pin id, nothing errors, and the public cannot
see the pin. This does not block implementation: a working integration is required to
record the demo video that Standard access review demands.

Environment is switchable via `PINTEREST_ENV`:

| Value | Base URL |
|---|---|
| `sandbox` | `https://api-sandbox.pinterest.com/v5/` |
| `production` | `https://api.pinterest.com/v5/` |

### Auth
One-time browser grant via a new `pinterest_auth.py`, mirroring
[etsy_auth.py](etsy_auth.py). Differences from the Etsy flow, all of them real
footguns:

- Scopes are **comma-separated**, not space-separated like
  [etsy_auth.py:78](etsy_auth.py#L78).
- Token exchange uses **HTTP Basic auth** (`base64(app_id:app_secret)`), not Etsy's
  secret-less PKCE.
- Access token lives **30 days**, refresh token **60 days rolling**. Refresh on
  near-expiry, not on every construction the way
  [etsy_service.py:149](services/etsy_service.py#L149) does.
- Redirect URI `http://localhost:8081/callback` — deliberately **not** 8080, which
  `etsy_auth.py` already owns.

Scopes requested: `boards:read`, `boards:write`, `pins:read`, `pins:write`,
`user_accounts:read`. Scopes are fixed at grant time; changing them later requires
re-consent.

### Pin image
Two options, decision pending (§7):

- **Etsy-hosted URL** — by Stage 9 the listing is live, so Etsy's CDN URL is
  available. No upload, and the pin matches the listing exactly. *Preferred.*
- **`image_base64`** — send `output/final_thumbnail/{design}_Black_Sweatshirt.png` directly.
  No hosting dependency, larger payloads against a tight Trial rate limit.

---

## 5. New and modified files

| File | Change |
|---|---|
| `sweatshirt_automation.py` | **new** — the orchestrator |
| `pinterest_auth.py` | **new** — one-time OAuth grant |
| `services/pinterest_service.py` | **new** — v5 client (boards, pins, token refresh) |
| `services/ai_content.py` | add `generate_pin_content()` + board classifier |
| `services/etsy_service.py` | add `activate_listing()`, `update_tags()`, `get_tags()` |
| `services/printify_service.py` | add a product-read helper for tag readback |
| `caching/.pinterest_tokens.json` | **new** — gitignored via existing `caching/` rule |
| `caching/pinned_sweatshirt.json` | **new** — pin ledger |

`.gitignore` needs no change — `caching/` is already excluded wholesale.

### New `.env` keys

```
PINTEREST_APP_ID=
PINTEREST_APP_SECRET=
PINTEREST_AUTH_PORT=8081
PINTEREST_ENV=sandbox
PINTEREST_BOARD_AI_HUMOR=
PINTEREST_BOARD_PROGRAMMING_HUMOR=
```

---

## 6. Failure handling & resumability

The pipeline is **idempotent and resumable**. Every stage is guarded by a ledger, so
re-running after a crash resumes rather than duplicating.

| Failure | Behavior |
|---|---|
| Gemini all keys exhausted | Falls back to placeholder content, continues (existing behavior) |
| Printify → Etsy sync times out | Design marked `awaiting_etsy`, retried next run, no charge incurred |
| Etsy activation fails | Listing stays draft; Pinterest is skipped for that design |
| Pinterest API fails | Etsy listing is already live and correct; pin retried next run |
| One design throws | Logged, skipped, batch continues |

Proposed flags: `--dry-run` (full trace, zero writes, zero charges), `--skip-pinterest`,
`--only "design.png"`, `--clear-ledger`.

---

## 7. Open decisions

1. **Boards — one or two?** The spec above assumes two boards with AI-vs-programmer
   classification. If there is genuinely only one board, the classifier is dropped and
   everything pins there.
2. **Pin image source** — Etsy-hosted URL (preferred) vs. `image_base64`.
3. **Pin dimensions** — ship the existing square-ish sweatshirt renders, or add a 2:3
   vertical (1000×1500) render pass? Square works but underperforms on Pinterest.

---

## 8. Accepted risks

Explicitly agreed, recorded so they are not a surprise later:

- **No positioning review.** Justified by the corrected default positioning on the
  integrated product. A regression in the template would publish bad products at scale.
- **No Etsy draft review.** Listings go live without a human look.
- **$0.20 × N charged unattended** at Stage 6. A 20-design batch is $4.00.
- **Printify republish overwrites our mockups.** Unchanged from the existing gotcha in
  [marketing_sweatshirt.py:56](marketing_sweatshirt.py#L56) — running
  `sweatshirt.py --update-listings` after this pipeline pushes Printify's default
  images back over ours.
