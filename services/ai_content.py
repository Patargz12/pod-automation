"""
ai_content.py — AI-generated SEO titles, descriptions & tags (Gemini 2.5 Flash)
===============================================================================
Looks at the actual design image and writes an Etsy-optimized title,
description, and tag set for the product. Used by printify_service so every
product gets a unique, search-friendly listing instead of placeholder
lorem-ipsum.

Requires GEMINI_API_KEY_1 … GEMINI_API_KEY_5 in the environment (see .env).
Keys come from Google AI Studio (aistudio.google.com → Get API key). The keys
are used as a rotating pool: when one key hits its quota/rate limit (HTTP 429)
or is invalid, the next key takes over automatically — the batch never halts.
The "current key" is sticky across designs within a run, so once key 1 is
exhausted, all later designs start straight on key 2.

If every key fails, generate_listing_content() returns the provided fallbacks
so the pipeline never breaks.
"""

from __future__ import annotations

import io
import json
import re
import os
from pathlib import Path

from PIL import Image

from services.brand_guard import BRAND_NAMES_FOR_PROMPT, scrub_text, warn_if_scrubbed

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv not installed — env vars must be set manually
    pass

# ── Config ──────────────────────────────────────────────────────────────────
# gemini-3.6-flash is the approved default (signed off 11 Sept 2026). The old
# gemini-2.5-flash is gone for this shop: keys 1, 2 and 4 answer 429
# RESOURCE_EXHAUSTED on it, and keys 3 and 5 answer 404 "no longer available to
# new users — use models/gemini-3.6-flash". All five keys work on 3.6-flash.
# Every Gemini call in the repo (listings, redescribe, Pinterest copy) reads
# this one constant. Override with GEMINI_MODEL in .env to pin a different one.
MODEL          = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
KEY_ENV_NAMES  = [f"GEMINI_API_KEY_{i}" for i in range(1, 6)]
MAX_IMAGE_EDGE = 1024     # downscale the design before sending (cheaper, faster)
ETSY_TITLE_MAX = 140      # Etsy hard limit on listing title length
ETSY_TAG_MAX   = 13       # Etsy hard limit on number of tags
ETSY_TAG_LEN   = 20       # Etsy hard limit on characters per tag
PIN_TITLE_MAX  = 100      # Pinterest hard limit on pin title
PIN_DESC_MAX   = 800      # Pinterest hard limit on pin description
PIN_ALT_MAX    = 500      # Pinterest hard limit on alt text

# ── Tag saturation control ───────────────────────────────────────────────────
# Every listing used to come back with the same handful of broad topic tags
# ("computer science" landed on 55 of 107 listings), so the whole catalogue
# competed against itself for one slot in Etsy search. Two guards now stop that:
#
#   1. HARD_BANNED_TAGS — phrases too broad or low-intent to ever rank.
#      Stripped before the tags are saved. Developer phrases live in DEV_TAGS
#      and are NOT stripped: they are correct on a developer design.
#   2. TAG_USAGE_FILE — a running count of how often each tag has been used in
#      this shop. Anything at or above TAG_SATURATION_LIMIT is fed back into the
#      prompt as "already saturated, pick something else", so the catalogue
#      spreads across long-tail phrases instead of piling onto a dozen.
TAG_USAGE_FILE        = Path("caching") / "tag_usage.json"
TAG_SATURATION_LIMIT  = 5    # a tag may appear on at most this many listings
TAG_SATURATION_SHOWN  = 40   # cap on how many saturated tags we list in the prompt

# Lowercased exact matches, in the two tiers the shop's tag policy actually has.
#
# DEV_TAGS are NOT stripped. They are correct on a design that is genuinely a
# developer joke and wrong on an everyday-AI one, and that is a judgement about
# the artwork, which only the model looking at the image can make — so the
# targeting call lives in SYSTEM_PROMPT, and audit.py catches a dev tag that
# lands on a non-dev design. Stripping them here instead would silently return
# fewer than 13 tags on every developer listing.
DEV_TAGS = frozenset({
    "computer science", "software engineer", "software developer", "developer gift",
    "developer shirt", "dev humor", "web developer", "programmer", "programmer gift",
    "programmer shirt", "coding", "coding shirt", "coding humor", "coder gift",
    "machine learning", "data scientist", "data scientist gift", "prompt engineer",
    "ai engineer gift", "ml engineer", "devops gift", "debugging shirt",
    "it professional", "sysadmin gift", "linux shirt", "git humor",
})

# Never allowed, whoever the design is for: pure topic labels and words too
# generic to rank, which waste one of the 13 slots for every audience.
HARD_BANNED_TAGS = frozenset({
    # ── topic labels, not shopping queries ──
    "ai", "artificial intel", "technology", "tech", "computer", "robot", "chatbot",
    "neural network", "algorithm", "automation", "innovation", "future",
    # ── too generic to rank, waste a slot ──
    "shirt", "tee", "t shirt", "tshirt", "sweatshirt", "crewneck", "hoodie",
    "clothing", "apparel", "gift", "gifts", "funny", "humor", "cute", "cool",
    "unisex", "graphic tee", "trendy", "aesthetic", "quality", "high quality",
})

# The union, kept under the original name because audit.py and retag.py import
# it — audit.py derives its own HARD_BANNED as BANNED_TAGS - DEV_TAGS, which
# stays correct only while this remains the whole set.
BANNED_TAGS = HARD_BANNED_TAGS | DEV_TAGS

# The two boards a design can land on: AI-user humor or developer humor. The
# board NAMES they map to live in sweatshirt_automation.DEFAULT_BOARD_NAMES.
BOARD_AI  = "ai_humor"
BOARD_DEV = "programming"
BOARD_KEYS = (BOARD_AI, BOARD_DEV)

# Brand names are trademarks; using them to sell merch is what Etsy IP reports
# act on. Every prompt carries this rule, and brand_guard.scrub_* enforces it on
# the output anyway, because the model does not always listen.
_BRAND_RULE = (
    "NEVER NAME AN AI COMPANY OR PRODUCT — not in the title, tags, description, "
    f"pin copy or anywhere else: {BRAND_NAMES_FOR_PROMPT}, or any other AI brand. "
    "They are registered trademarks and using them on merch gets listings taken "
    "down. Say \"AI\", \"the chatbot\", \"your AI assistant\" or \"the robot\" "
    "instead. If the design's own artwork shows a brand name, still write \"AI\" "
    "— never copy the brand into the listing."
)

SYSTEM_PROMPT = (
    "You are an expert Etsy SEO copywriter for Nullish, a print-on-demand apparel "
    "shop.\n\n"
    "WHO NULLISH SELLS TO — this governs every word you write. Nullish sells AI "
    "humor. The core audience is the people who USE AI every day: students writing "
    "essays and cramming for exams, office workers drafting emails and surviving "
    "meetings, teachers, nurses, marketers, writers, small-business owners, and "
    "anyone whose daily routine now runs through an AI chatbot. That joke is about "
    "LIVING with AI — leaning on it, arguing with it, hiding how much you use it, "
    "pretending you didn't.\n\n"
    "Developers are a real second audience, not a forbidden one. Some designs are "
    "written for the people who build and ship software — increasingly the people "
    "who now code alongside AI: pair-programming with an assistant, reviewing what "
    "it generated, shipping what it wrote. When a design is a developer joke, "
    "write it as one — developer voice, developer vocabulary, developer buyers.\n\n"
    "MATCH THE COPY TO THE DESIGN. This is the rule, and it runs in BOTH "
    "directions. Read the artwork and write for the buyer it is actually for. A "
    "developer design gets developer language. An everyday-AI design must NOT get "
    "developer language — someone hiding their chat history is not shipping code, "
    "and words like 'programmer' or 'debugging' on that design chase off the buyer "
    "who wanted it. Never stretch a design toward an audience its artwork does not "
    "honestly reach, in either direction.\n\n"
    "AI stays the shop's centre of gravity for both audiences. Where a developer "
    "design genuinely touches AI, lead with that angle — it is where the shop is "
    "growing. Where it does not, write the developer joke straight rather than "
    "forcing AI into it.\n\n"
    "Remember who is holding the credit card. Most Etsy apparel is bought as a "
    "GIFT — a girlfriend buying for her boyfriend, a mum for a student, a coworker "
    "for a Secret Santa. Write so that person recognises the recipient in your "
    "words.\n\n"
    "You write naturally — never keyword-spam, never ALL CAPS, never emojis, and "
    "never filler phrases like 'high quality' or 'premium'. Your descriptions "
    "follow the requested block structure exactly, in a warm, conversational voice "
    "that leads with the joke and only then talks about the garment.\n\n"
    + _BRAND_RULE
)

# Shared across the tee and sweatshirt prompts — the title and tag rules are the
# shop's SEO strategy and must not drift apart between the two garments.
_TITLE_RULES = """\
TITLE rules:
- 125–140 characters. Comma-separated keyword phrases, readable and natural.
- THE FIRST 40 CHARACTERS DECIDE WHETHER THIS LISTING IS EVER SEEN. Etsy weights
  the opening of the title far above the rest, so it must be the phrase a real
  shopper types — never the design's punchline. Write the title in this order:

    1. Primary search phrase — what a buyer types. Lead with the audience or the
       occasion plus the garment, e.g. "Funny AI Sweatshirt", "AI Study
       Humor Shirt", "Gift for Overworked Coworker".
    2. Secondary search phrase — a different way to search for the same thing.
    3. The design's actual joke or slogan, in Title Case.
    4. A gift phrase naming who receives it, e.g. "Gift for College Student",
       "Secret Santa Gift for Coworker", "Teacher Gift".
    5. The garment words last: "Unisex Tee" / "Unisex Crewneck Sweatshirt".

- NEVER open with the joke. NEVER include "High Quality", "Premium", "Limited
  Edition" or any other filler — it has no search value and reads as padding.
- Use Title Case. NEVER write whole words in ALL CAPS, even if the design's text
  is all-caps (Printify rejects titles with excessive caps). Short acronyms like
  AI are fine.
- Match the title's vocabulary to the DESIGN. On an everyday-AI design use
  everyday language — "AI", "chatbot" and "prompt" are fine; "coding",
  "debugging", "software engineer", "machine learning" are not. On a genuine
  developer design those same words are CORRECT and should be used, because
  they are what that buyer actually types.
- NEVER put an AI brand or company name in the title (ChatGPT, GPT, OpenAI,
  Claude, Anthropic, Gemini, Copilot, Grok, …) — trademark risk. Use "AI".\
"""

_TAG_RULES = """\
TAGS rules — Etsy gives 13 tags and they are the single biggest ranking lever,
so treat them as a portfolio, not a word cloud:
- Return 16 tags: the 13 described below, in that order, then 3 BACKUP tags
  (any slot type) that stand in if one of the 13 is rejected. Each at most 20
  characters INCLUDING spaces (hard limit) — count them; a longer tag is
  thrown away, not shortened.
- Lowercase multi-word phrases a buyer would actually type. A tag that is one
  broad noun ("ai", "technology", "funny") is a wasted slot — always pair a
  subject with an audience, occasion or product word.
- No duplicates, no '#', no ALL CAPS.

- Fill the 13 slots to THIS distribution, so no two listings in the shop end up
  with the same tag set:
    • 4 tags SPECIFIC TO THIS DESIGN — built from the actual words, joke or
      imagery on the garment. These are what make this listing findable as
      itself rather than as one of a hundred lookalikes.
    • 3 AUDIENCE tags naming who wears it, e.g. "college student gift",
      "gift for coworker", "teacher gift idea", "grad student gift".
    • 3 OCCASION or GIFT tags, e.g. "secret santa gift", "back to school gift",
      "birthday gift for her", "finals week gift".
    • 3 CATEGORY tags placing the product, e.g. "funny ai sweatshirt",
      "ai chatbot shirt", "study humor tee", "office humor shirt".

- NO BRAND NAMES — never put an AI company or product name in a tag
  (chatgpt, gpt, openai, claude, anthropic, gemini, copilot, grok, …).
  Write "ai" or "chatbot" instead, e.g. "ai user gift", not "chatgpt user gift".

- HARD BANNED — never emit these, whoever the design is for. They are pure topic
  labels or words too broad to rank, and they waste a slot every time: ai, tech,
  technology, computer, robot, chatbot, neural network, algorithm, automation,
  funny, humor, gift, gifts, shirt, tee, tshirt, sweatshirt, crewneck, hoodie,
  clothing, apparel, unisex, cute, cool, aesthetic, trendy, quality,
  high quality.

- DEVELOPER TAGS — computer science, software engineer, developer gift,
  programmer gift, coding humor, coding shirt, machine learning, data scientist,
  prompt engineer, web developer, dev humor, debugging shirt, git humor,
  linux shirt, sysadmin gift and the like. These are ALLOWED, but ONLY on a
  design that is genuinely a developer joke. On an everyday-AI design they are a
  defect — they target a buyer who will scroll straight past. If you are unsure
  which kind of design you are looking at, do not use them.\
"""

_DESCRIPTION_RULES = """\
DESCRIPTION rules:
- Plain text only. No markdown, no headings, no bold, no emojis. The ONLY
  special character allowed is the bullet character "•" in the list block.
- 200–260 words total, written as SIX blocks separated by blank lines, in this
  exact order:

  1. HOOK — one short standalone line that reads like the joke itself: either
     the design's own text, or a one-line setup-and-punchline riff on it.
     Conversational, human, a little cheeky. No product words here.
     Examples of the voice (do not copy the topics):
     I didn't write this essay. We wrote this essay.
     My boss thinks I'm gifted. I have three tabs open.

  2. WHAT IT IS — 2–3 sentences. Open with "A funny and relatable graphic
     {garment} for ..." (or a close variant) naming 2–3 buyer groups drawn from
     the everyday-AI audience: students, teachers, office workers, remote
     workers, writers, marketers, nurses, anyone who runs their day through
     an AI chatbot. On a developer design, name developer buyer groups instead —
     software developers, engineers, dev teams, anyone shipping code with an AI
     assistant open in the next window. Then say what the design combines (e.g. study-stress with quiet
     confidence, office life with dry sarcasm, everyday life with a robot
     sidekick) and why it works as a conversation starter, naming 2–3 real
     occasions (lecture halls, library all-nighters, the morning stand-up,
     casual Fridays, everyday wear).

  3. WHO IT'S FOR — 2 sentences. Start with "Designed for ..." describing the
     mindset of the person who gets the joke. Then a gifting sentence:
     "Whether you're shopping for yourself or looking for a unique gift for
     ..." listing 3–5 specific recipients (e.g. a college student, a
     hard-working coworker, your favourite teacher, a boyfriend or girlfriend,
     a best friend), and close on the aesthetic ("clean and stylish",
     "minimalist", etc.).

  4. BULLET LIST — the literal line "Perfect for:" followed by exactly 6
     bullets, each on its own line starting with "• ". Each bullet is a short
     noun phrase, no ending punctuation. Mix audiences, humor angles, style
     and gifting, e.g.:
     • College students and grad students
     • Office workers and remote teams
     • Everyday AI users
     • Secret Santa and birthday gifts

  5. STYLING — one sentence on comfort and how to wear it ({styling}) for an
     easy everyday look.

  6. GARMENT — {garment_block}

- Weave relevant search terms in naturally across blocks 2–4; never keyword-spam.
- Reflect the ACTUAL text, joke and theme visible in the design — no generic
  filler that would fit any shirt.
- Never use the words "high quality" or "premium" anywhere in the description.
- Address the audience the DESIGN belongs to. On an everyday-AI design, never
  address programmers or developers and never use coding vocabulary. On a
  genuine developer design, write to developers directly and use their
  vocabulary — that is the right buyer, not a mistake.
- NEVER name an AI company or product anywhere in the description, including
  the hook line (ChatGPT, GPT, OpenAI, Claude, Anthropic, Gemini, Copilot,
  Grok, …). Say "AI", "the chatbot" or "your AI assistant". If the artwork
  itself shows a brand name, paraphrase it as "AI" — do not copy it.\
"""

_TEE_GARMENT_BLOCK = """3–4 sentences on the shirt itself. It is a unisex
     Bella+Canvas 3001 jersey tee: lightweight and breathable, soft Airlume
     combed and ring-spun cotton, retail fit crew neck, side-seamed for a tidy
     shape that layers well. The artwork is a bold FRONT print (never say
     "back print"). Tie the fabric description back to the design's attitude
     and end on when to wear it."""

_SWEATSHIRT_GARMENT_BLOCK = """3–4 sentences on the sweatshirt itself. It is a
     unisex Gildan 18000 Heavy Blend crewneck: warm and midweight, a soft
     cotton-polyester blend with air-jet spun yarn for a smoother feel,
     double-needle stitching, and ribbed collar, cuffs and waistband that hold
     their shape. The artwork is a bold FRONT print (never say "back print").
     Tie the fabric description back to the design's attitude and end on when
     to wear it."""

USER_PROMPT = f"""\
This design is printed on the FRONT (centered) of a unisex Bella+Canvas 3001 t-shirt.
Look at the design image and write an Etsy listing for it.

Write it for Nullish's audience: the people who USE AI every day — students,
office workers, teachers, creatives, everyday AI users — and for the person
buying it as a gift for one of them. If this design is a genuine DEVELOPER joke,
write it for developers instead — they are a supported audience — and lead with
the AI angle wherever the artwork honestly has one.

{_TITLE_RULES}

{_DESCRIPTION_RULES.format(
    garment="tee",
    styling="jeans, shorts, joggers, sneakers",
    garment_block=_TEE_GARMENT_BLOCK,
)}

{_TAG_RULES}
"""

# Same rules, sweatshirt garment — used by the sweatshirt pipeline (sweatshirt.py).
SWEATSHIRT_USER_PROMPT = f"""\
This design is DTF-printed on the FRONT (centered) of a unisex Gildan 18000
Heavy Blend crewneck sweatshirt.
Look at the design image and write an Etsy listing for it.

Write it for Nullish's audience: the people who USE AI every day — students,
office workers, teachers, creatives, everyday AI users — and for the person
buying it as a gift for one of them. If this design is a genuine DEVELOPER joke,
write it for developers instead — they are a supported audience — and lead with
the AI angle wherever the artwork honestly has one.

{_TITLE_RULES}

{_DESCRIPTION_RULES.format(
    garment="sweatshirt",
    styling="jeans, joggers, leggings, sneakers",
    garment_block=_SWEATSHIRT_GARMENT_BLOCK,
)}

{_TAG_RULES}
"""

# ── Pinterest ────────────────────────────────────────────────────────────────
# Pinterest is a visual search engine, not a marketplace: there is no tag field,
# so every keyword has to earn its place inside the description prose.
PIN_SYSTEM_PROMPT = (
    "You are an expert Pinterest SEO strategist for Nullish, a print-on-demand "
    "apparel shop.\n\n"
    "WHO NULLISH SELLS TO: the people who USE AI every day — students writing "
    "essays and cramming for exams, office workers drafting emails and surviving "
    "meetings, teachers, nurses, marketers, writers, and anyone whose daily "
    "routine now runs through an AI chatbot. Developers are a second audience: when "
    "the design is a programming joke, write to developers in their own "
    "vocabulary. Otherwise never use developer vocabulary — no code, git, "
    "debugging, machine learning or terminal jokes.\n\n"
    "Pinterest skews heavily toward women, students and gift-shoppers, which is "
    "exactly this audience — write to her, and to the person she is buying for.\n\n"
    "You write pin titles and descriptions that surface in Pinterest search and "
    "drive clicks through to the product listing. Pinterest has no tag field, so "
    "you weave keywords naturally into the description prose. You never "
    "keyword-spam, never write in ALL CAPS, and never use hashtags — Pinterest "
    "deprioritizes them. You write warmly and lead with the joke, not the garment.\n\n"
    + _BRAND_RULE
)

PIN_USER_PROMPT = """\
This design is printed on the front of a unisex crewneck sweatshirt that is for
sale on Etsy. Look at the design image and write a Pinterest pin for it, then
classify which board it belongs on.

TITLE rules:
- Max 100 characters. Aim for 60–95.
- Front-load the searched keyword phrase, then the hook.
- Title Case, natural and readable. No ALL CAPS words, no hashtags, no emojis.
- Must reflect the actual text / joke visible in the design.

DESCRIPTION rules:
- Plain text, 200–400 characters. No markdown, no emojis, no hashtags.
- Two or three sentences. Open with the joke or the feeling it lands on, then
  say who would wear it and when, then close with a soft call to action such as
  "Tap to see it on Etsy."
- Weave in 3–5 search phrases buyers would actually type (e.g. "funny AI
  sweatshirt", "gift for college student", "office humor crewneck", "study
  motivation outfit") so they read as part of the sentence rather than a
  keyword list.

ALT_TEXT rules:
- Max 500 characters, usually 1 sentence.
- Plainly describe what is VISUALLY on the sweatshirt for a screen-reader user:
  the garment color, and the design's text and imagery. No marketing language.

BOARD rules — choose EXACTLY one, by WHO the joke is for:
- "programming" if the joke is about WRITING software: code, bugs, debugging,
  git, terminals, compilers, syntax, stack traces, localhost, devops, or
  coding alongside AI. A non-programmer would not fully get it.
- "ai_humor" for everything else: using or living with AI as a normal person —
  chatbots, prompts, an AI chatbot as a study buddy or coworker, school, office life.
- "AI" on its own is not programming. If unsure, choose "ai_humor". Never
  invent a third value.
"""


# ══════════════════════════════════════════════════════════════════════════════
#  Key pool — sticky rotation across designs within a run
# ══════════════════════════════════════════════════════════════════════════════

_key_index = 0  # index into the key pool; advances permanently when a key dies

# One genai.Client per API key, reused for the whole run.
#
# These must NOT be created per call. The SDK's clients share an underlying
# transport, and when a discarded one is garbage-collected it closes that
# transport out from under the clients still in use — every later request then
# fails with "Cannot send a request, as the client has been closed". That is not
# a quota error, so the key pool reads it as five dead keys and gives up on the
# whole run within seconds.
_clients: dict[str, object] = {}


def _client_for(key: str, fresh: bool = False):
    """The cached client for this key, rebuilding it if its transport died."""
    from google import genai
    if fresh:
        _clients.pop(key, None)
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key)
    return _clients[key]


def _api_keys() -> list[str]:
    """All non-blank GEMINI_API_KEY_1..5 values, in order."""
    return [k for k in (os.environ.get(n, "").strip() for n in KEY_ENV_NAMES) if k]


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

# Backdrops the design is flattened onto before it is shown to Gemini. Dark is
# near-black rather than pure black so a design with black outlines still reads.
_FLATTEN_LIGHT = (255, 255, 255, 255)
_FLATTEN_DARK  = (32, 32, 32, 255)


def _backdrop_for(im: Image.Image) -> tuple[int, int, int, int]:
    """Pick the backdrop that keeps this artwork VISIBLE once flattened.

    These designs are mostly light text printed on dark garments, and the
    transparent PNGs were previously flattened onto white — which turned white
    artwork into a blank white square. Gemini then wrote the listing from an
    image it could not actually see. Flattening onto the opposite of the
    artwork's own brightness keeps the design legible either way, and matches
    the garment it will really be printed on.
    """
    alpha = im.getchannel("A")
    if not alpha.getbbox():                       # fully transparent → nothing to judge
        return _FLATTEN_LIGHT
    # Mean brightness of the pixels that are actually drawn (alpha as the weight).
    grey = im.convert("RGB").convert("L")
    total_a = sum(alpha.histogram()[i] * i for i in range(256))
    if total_a == 0:
        return _FLATTEN_LIGHT
    weighted = sum(p * a for p, a in zip(grey.getdata(), alpha.getdata()))
    return _FLATTEN_DARK if (weighted / total_a) > 140 else _FLATTEN_LIGHT


def _design_to_jpeg_bytes(path: Path) -> bytes:
    """Downscale the design and flatten transparency onto a contrasting
    backdrop → JPEG bytes."""
    with Image.open(path) as im:
        im = im.convert("RGBA")
        im.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
        bg = Image.new("RGBA", im.size, _backdrop_for(im))
        im = Image.alpha_composite(bg, im).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# Acronyms allowed to stay ALL CAPS in a title. Anything else written fully in
# caps is title-cased: the old "3+ letters" rule let "I TOLD MY AI TO WRITE
# THIS SHIRT" through as "I Told MY AI TO Write…" and Printify still rejected it.
_TITLE_ACRONYMS = {"AI", "PC", "USA", "UK", "CEO", "HR", "DIY", "XL", "UX", "UI"}
_TITLE_MAX_CAPS_RATIO = 0.35


def _decap_title(title: str) -> str:
    """Title-case every fully-uppercase word that isn't a known acronym —
    Printify rejects titles with 'excessive caps' (error 61003)."""
    def fix(w: str) -> str:
        core = re.sub(r"[^A-Za-z]", "", w)
        if len(core) >= 2 and core.isupper() and core not in _TITLE_ACRONYMS:
            return w.capitalize() if w[:1].isalpha() else w[:1] + w[1:].capitalize()
        return w

    title = " ".join(fix(w) for w in title.split(" "))
    # Safety net: if capitals still dominate (e.g. "AI" repeated, or acronyms
    # stacked), lower the acronyms too except "AI", the shop's core keyword.
    letters = [c for c in title if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > _TITLE_MAX_CAPS_RATIO:
        title = " ".join(
            w.capitalize() if re.sub(r"[^A-Za-z]", "", w).isupper()
            and re.sub(r"[^A-Za-z]", "", w) != "AI" and len(w) > 1 else w
            for w in title.split(" ")
        )
    return title


# Padding phrases the model reaches for that carry no search value. Eight live
# listings opened with "High Quality" (one as "High QualityT-Shirt") before this
# existed; the prompt now forbids them and this strips any that slip through.
_FILLER_PHRASES = (
    "high quality", "premium quality", "premium", "best quality", "top quality",
    "limited edition", "brand new", "hot sale", "trending now",
)


def _strip_filler(title: str) -> str:
    """Remove filler phrases from a title and tidy the punctuation they leave."""
    for phrase in _FILLER_PHRASES:
        title = re.sub(re.escape(phrase), "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title)
    # Cutting a phrase out can leave doubled or adjacent separators behind
    # ("Tee | | Gift", "Shirt, | Gift"); collapse any run of them into one.
    title = re.sub(r"\s*([|,])(\s*[|,])+\s*", r" \1 ", title)
    # Re-normalise the spacing around the separators that survive.
    title = re.sub(r"\s*\|\s*", " | ", title)
    title = re.sub(r"\s*,\s*", ", ", title)
    return re.sub(r"\s+", " ", title).strip(" ,|-")


def _strip_markdown(text: str) -> str:
    """Drop emphasis markers the model slips in despite being told plain text.

    Pinterest and Etsy both render these literally, so "*gets*" ships as
    "*gets*". Only paired markers around a word are removed — a lone asterisk
    or underscore inside real copy is left alone.
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)\*(\S(?:.*?\S)?)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_(\S(?:.*?\S)?)_(?!\w)", r"\1", text)
    return text


def _fit_title(title: str, limit: int = ETSY_TITLE_MAX) -> str:
    """Trim a title to Etsy's limit without cutting a word in half.

    The model aims for 125–140 characters and regularly lands a word or two
    over, so a plain slice shipped titles ending "...Unisex Crewneck Swea".
    Prefer dropping the whole trailing comma-phrase; fall back to the last
    whole word when that would cost too much of the title.
    """
    if len(title) <= limit:
        return title.strip(" ,|-")
    cut = title[:limit]
    at_sep = max(cut.rfind(","), cut.rfind("|"))
    if at_sep >= limit - 35:          # a clean phrase boundary, close to the end
        cut = cut[:at_sep]
    elif " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip(" ,|-")


def _sanitize_tags(tags: list[str]) -> list[str]:
    """Dedupe, strip, drop empties / over-20-char / banned tags, cap at 13.

    HARD_BANNED_TAGS is enforced here rather than trusted to the prompt: the
    model will occasionally slip a bare-noun tag through, and one "ai" on every
    listing is exactly the cannibalisation this shop is digging itself out of.

    DEV_TAGS deliberately pass through. Whether a developer tag belongs depends
    on what the design IS, which this function cannot see — the prompt makes
    that call and audit.py verifies it against the design name afterwards.
    """
    clean, seen = [], set()
    for t in tags or []:
        # Brand names become "ai" ("chatgpt shirt" -> "ai shirt"); a tag that
        # was only a brand becomes bare "ai", which HARD_BANNED_TAGS drops.
        t = " ".join(scrub_text(str(t)).split()).strip().lower().lstrip("#")
        if not t or len(t) > ETSY_TAG_LEN or t in seen or t in HARD_BANNED_TAGS:
            continue
        seen.add(t)
        clean.append(t)
        if len(clean) == ETSY_TAG_MAX:
            break
    return clean


def _top_up_tags(keys: list[str], tags: list[str], design_path: Path) -> list[str]:
    """Ask Gemini for the missing tags when sanitizing left fewer than 13.

    The 3 spare tags usually cover any drops; this is the last resort, so a
    listing never goes out with 12. Returns the original list if it fails.
    """
    from google.genai import types
    from pydantic import BaseModel

    class MoreTags(BaseModel):
        tags: list[str]

    missing = ETSY_TAG_MAX - len(tags)
    prompt = (
        f"This listing already has these Etsy tags: {', '.join(tags)}.\n"
        f"Write {missing + 3} MORE tags for the same design, so it reaches 13.\n"
        "- Lowercase multi-word phrases a buyer would actually type.\n"
        "- Each at most 20 characters INCLUDING spaces — count them.\n"
        "- Do not repeat any existing tag. No '#', no single broad nouns like "
        "ai, tech, funny, gift, shirt, tee, sweatshirt.\n"
        "- Developer tags only if the design is genuinely a developer joke."
    )
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=MoreTags,
        max_output_tokens=2048,
    )
    try:
        img = types.Part.from_bytes(data=_design_to_jpeg_bytes(design_path),
                                    mime_type="image/jpeg")
    except Exception:
        return tags
    extra = _walk_key_pool(keys, [img, prompt + _saturation_note()], config)
    if extra is None:
        return tags
    return _sanitize_tags(tags + list(extra.tags or []))


# ── Tag saturation ledger ────────────────────────────────────────────────────
# caching/tag_usage.json is a flat {tag: times_used} map. It is advisory: a
# missing or corrupt file just means no tag is considered saturated yet, so a
# fresh clone still works and a bad write can never break a run.

def _load_tag_usage() -> dict[str, int]:
    try:
        data = json.loads(TAG_USAGE_FILE.read_text(encoding="utf-8"))
        return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def saturated_tags(limit: int = TAG_SATURATION_LIMIT) -> list[str]:
    """Tags already used on `limit`+ listings, most-used first."""
    usage = _load_tag_usage()
    return sorted((t for t, n in usage.items() if n >= limit),
                  key=lambda t: -usage[t])


def record_tags(tags: list[str]) -> None:
    """Add one listing's tags to the usage counts. Never raises."""
    if not tags:
        return
    try:
        usage = _load_tag_usage()
        for t in tags:
            usage[t] = usage.get(t, 0) + 1
        TAG_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        TAG_USAGE_FILE.write_text(
            json.dumps(dict(sorted(usage.items(), key=lambda kv: -kv[1])), indent=1),
            encoding="utf-8")
    except Exception as e:
        print(f"     ⚠️  Could not update {TAG_USAGE_FILE} ({type(e).__name__}: {e}); "
              f"tag saturation tracking is skipped for this design.")


def _saturation_note() -> str:
    """Prompt fragment listing tags the shop has already leaned on too hard."""
    hot = saturated_tags()[:TAG_SATURATION_SHOWN]
    if not hot:
        return ""
    return (
        "\n\nALREADY SATURATED — these tags are on "
        f"{TAG_SATURATION_LIMIT} or more listings in this shop already. Etsy "
        "rarely shows two listings from the same shop for one query, so reusing "
        "them makes this listing compete against its own siblings. Do NOT use "
        "any of them; find longer, more specific phrases instead:\n"
        + ", ".join(hot)
    )


def _is_quota_or_auth_error(e: Exception) -> bool:
    """Errors that mean THIS KEY is done for → rotate to the next key."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code in (401, 403, 429):
        return True
    msg = str(e).upper()
    return any(s in msg for s in ("RESOURCE_EXHAUSTED", "QUOTA", "RATE LIMIT",
                                  "PERMISSION_DENIED", "API_KEY_INVALID", "UNAUTHENTICATED"))


def _is_transient_error(e: Exception) -> bool:
    """Server-side hiccups worth retrying on the same key.

    "client has been closed" belongs here too: it means this process killed its
    own transport (see _client_for), not that the key is bad. Retrying rebuilds
    the client and succeeds.
    """
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if isinstance(code, int) and code >= 500:
        return True
    msg = str(e).upper()
    return any(s in msg for s in ("UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL",
                                  "OVERLOADED", "CLIENT HAS BEEN CLOSED"))


def _walk_key_pool(keys: list[str], contents: list, config) -> object | None:
    """Run one generation, rotating through the key pool on quota/auth failures.

    Returns the parsed response object, or None when every key is spent. Shared
    by generate_listing_content() and generate_pin_content() so both benefit from
    the same sticky rotation — once key 1 dies, neither goes back to it.
    """
    global _key_index
    import time

    from google import genai

    # A key is only BURNED (permanently skipped for the rest of the run) when
    # the key itself is the problem — quota, auth, a bad request. A 503
    # "model is experiencing high demand" is Google being busy and says nothing
    # about the key: burning the pool on it takes ten seconds to kill all five
    # keys and every remaining listing in the run. Those get backoff instead,
    # and the cursor is left where it was.
    i = _key_index
    while i < len(keys):
        key_no = i + 1  # 1-based, matches GEMINI_API_KEY_N
        for attempt in range(3):
            try:
                client = _client_for(keys[i], fresh=attempt > 0)
                response = client.models.generate_content(
                    model=MODEL, contents=contents, config=config)
                if response.parsed is None:
                    raise ValueError(f"empty/unparseable response: {response.text!r:.200}")
                return response.parsed

            except Exception as e:
                if _is_quota_or_auth_error(e):
                    print(f"     🔑 GEMINI_API_KEY_{key_no} exhausted/rejected "
                          f"({type(e).__name__}); rotating to next key…")
                    _key_index = i + 1  # burn it for the rest of the run
                    break
                # A cut-off / unparseable reply is the model running out of
                # room, not a bad key — retry on the same key.
                if _is_transient_error(e) or "unparseable" in str(e):
                    if attempt < 2:
                        wait = 8 * (attempt + 1)
                        print(f"     ⏳ Gemini busy ({type(e).__name__}); "
                              f"retrying on the same key in {wait}s…")
                        time.sleep(wait)
                        continue
                    print(f"     ⏳ Still busy on GEMINI_API_KEY_{key_no}; "
                          f"trying the next key (this one is NOT burned)…")
                    break
                print(f"     🔑 GEMINI_API_KEY_{key_no} failed "
                      f"({type(e).__name__}: {e}); rotating to next key…")
                _key_index = i + 1
                break
        i += 1

    print(f"     ⚠️  All {len(keys)} Gemini key(s) failed this call; nothing written.")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════════════════════

def generate_listing_content(
    design_path: Path,
    fallback_title: str,
    fallback_description: str,
    fallback_tags: list[str] | None = None,
    user_prompt: str | None = None,
) -> tuple[str, str, list[str], bool]:
    """Return (title, description, tags, ai_used).

    Looks at the design image with Gemini 2.5 Flash and generates an Etsy
    listing. Rotates through GEMINI_API_KEY_1..5 on quota/auth errors. Falls
    back to the provided placeholders (ai_used=False) if no keys are set or
    every key fails — the caller never has to handle exceptions.

    `user_prompt` overrides the default t-shirt prompt (e.g. pass
    SWEATSHIRT_USER_PROMPT for sweatshirt listings).
    """
    global _key_index
    fallback_tags = fallback_tags or []

    keys = _api_keys()
    if not keys:
        return fallback_title, fallback_description, fallback_tags, False

    try:
        from google import genai
        from google.genai import types
        from pydantic import BaseModel
    except ImportError as e:
        print(f"     ⚠️  google-genai not installed ({e}); using placeholder. "
              f"Run: pip install -r requirements.txt")
        return fallback_title, fallback_description, fallback_tags, False

    class Listing(BaseModel):
        title: str
        description: str
        tags: list[str]

    try:
        img_bytes = _design_to_jpeg_bytes(design_path)
    except Exception as e:
        print(f"     ⚠️  Could not read design image ({type(e).__name__}: {e}); using placeholder.")
        return fallback_title, fallback_description, fallback_tags, False

    contents = [
        types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
        (user_prompt or USER_PROMPT) + _saturation_note(),
    ]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=Listing,
        # Descriptions are now ~250 words of structured copy; 2.5 Flash also
        # spends output tokens on thinking, so keep plenty of headroom or the
        # response comes back truncated and unparseable (4096 was not enough).
        max_output_tokens=8192,
    )

    listing = _walk_key_pool(keys, contents, config)
    if listing is None:
        return fallback_title, fallback_description, fallback_tags, False

    raw_title = " ".join((listing.title or "").split())
    raw_description = (listing.description or "").strip()
    warn_if_scrubbed("title", raw_title)
    warn_if_scrubbed("description", raw_description)
    warn_if_scrubbed("tags", list(listing.tags or []))
    title = _fit_title(_strip_filler(_decap_title(scrub_text(raw_title))))
    description = scrub_text(raw_description)
    tags = _sanitize_tags(listing.tags)
    if tags and len(tags) < ETSY_TAG_MAX:
        print(f"     🏷️  Only {len(tags)} usable tags after cleanup — asking Gemini for more…")
        tags = _top_up_tags(keys, tags, design_path)
        if len(tags) < ETSY_TAG_MAX:
            print(f"     ⚠️  Still {len(tags)}/{ETSY_TAG_MAX} tags — publishing with what we have.")
    # All three must be real AI output. A half-filled listing (e.g. AI title
    # but placeholder description) counts as a failure, not a success.
    if not (title and description and tags):
        print(f"     ⚠️  Gemini returned an incomplete listing "
              f"(title={bool(title)}, description={bool(description)}, tags={len(tags or [])}).")
        return fallback_title, fallback_description, fallback_tags, False
    record_tags(tags)
    return title, description, tags, True


# ── Descriptions only, for listings that are already live ────────────────────
# Rewriting copy on a LIVE listing is a different job from generating a new one.
# The title and tags are already correct and audited, so nothing here may touch
# them, and the design source art is not in the repo — the only image available
# is the listing's own product photo, fetched from Etsy.
#
# This path deliberately does NOT call record_tags(). The saturation ledger must
# only ever count tags that are actually ON a listing; a description run
# publishes none, and letting it write would poison the counts the next batch is
# generated against.

_GARMENT_SPECS = {
    "tee":        ("jeans, shorts, joggers, sneakers",  _TEE_GARMENT_BLOCK),
    "sweatshirt": ("jeans, joggers, leggings, sneakers", _SWEATSHIRT_GARMENT_BLOCK),
}


def _photo_to_jpeg_bytes(data: bytes) -> bytes:
    """Downscale a fetched product photo to the same budget as a design file.

    No transparency dance here: unlike a design PNG, a mockup is already
    flattened onto the garment it prints on, which is exactly the contrast
    _backdrop_for() has to reconstruct for a bare design.
    """
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        im.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def generate_description(
    image_bytes: bytes,
    garment: str,
    listing_title: str = "",
    is_dev_design: bool = False,
    fallback_description: str = "",
) -> tuple[str, bool]:
    """Return (description, ai_used) for one live listing.

    `image_bytes` is the listing's product photo as downloaded from Etsy.
    `garment` is "tee" or "sweatshirt" — it selects the fabric block, so a
    wrong value would describe the wrong product. `is_dev_design` comes from
    the caller (the pinned ledger plus the design-name test), not from the
    model guessing off a thumbnail.

    `listing_title` is passed as CONTEXT ONLY, so the prose stays consistent
    with the keywords the listing already ranks on. It is deliberately the
    whole title rather than its first segment: since the title rewrite, that
    segment is the search phrase ("Funny Programmer Shirt"), not the joke. The
    joke is in the image, which is where the copy must come from.

    Never raises: on missing keys, a dead key pool or an unreadable image it
    returns the fallback with ai_used=False, and the caller writes nothing.
    """
    keys = _api_keys()
    if not keys:
        return fallback_description, False

    try:
        from google import genai
        from google.genai import types
        from pydantic import BaseModel
    except ImportError as e:
        print(f"     ⚠️  google-genai not installed ({e}); skipping.")
        return fallback_description, False

    styling, garment_block = _GARMENT_SPECS.get(garment, _GARMENT_SPECS["tee"])

    class Description(BaseModel):
        description: str

    try:
        img = _photo_to_jpeg_bytes(image_bytes)
    except Exception as e:
        print(f"     ⚠️  Could not read product photo ({type(e).__name__}: {e}); skipping.")
        return fallback_description, False

    audience = (
        "This IS one of the shop's DEVELOPER designs — confirmed, not a guess. "
        "Write it for developers: their voice, their vocabulary, their buyers. "
        "If the joke genuinely touches AI-assisted coding, lead with that angle."
        if is_dev_design else
        "This is an everyday-AI design. Write it for the people who USE AI daily "
        "and never address programmers or developers, unless the artwork is "
        "unmistakably a developer joke — in which case follow the artwork."
    )

    prompt = f"""\
The image is the live product photo for an Etsy listing that is already
published. The design is printed on the FRONT of a {garment}. Read the artwork
in the photo and write ONLY a replacement DESCRIPTION for it.

{f'For context, the listing already ranks under this title, so keep the copy consistent with it — but write from the ARTWORK, not from the title: "{listing_title}"' if listing_title else ''}

{audience}

Do NOT write a title. Do NOT write tags. Description only.

{_DESCRIPTION_RULES.format(
    garment=garment,
    styling=styling,
    garment_block=garment_block,
)}
"""

    contents = [types.Part.from_bytes(data=img, mime_type="image/jpeg"), prompt]
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=Description,
        max_output_tokens=4096,
    )

    result = _walk_key_pool(keys, contents, config)
    if result is None:
        return fallback_description, False

    description = _strip_markdown((result.description or "").strip())

    # The model sometimes returns the JSON escape sequence as LITERAL text —
    # a backslash and an n — instead of a real line break, which publishes as
    # "\n\n" in the middle of the shopper's description. Only whole-token
    # matches are converted, so a legitimate backslash in prose survives.
    if "\\n" in description:
        description = description.replace("\\r\\n", "\n").replace("\\n", "\n")

    # A design whose artwork is set in capitals comes back with a SHOUTED hook.
    # It is the first line the shopper reads, and the shop's voice rule is no
    # ALL CAPS. Short tokens (I, AI, PC) keep their case.
    lines = description.splitlines()
    if lines and lines[0].isupper():
        lines[0] = " ".join(w if len(w) <= 2 else w.capitalize()
                            for w in lines[0].split())
        description = "\n".join(lines)

    # The garment block tells the model the artwork is a "bold FRONT print" to
    # stop it inventing a back print. It echoes the emphasis back verbatim, so
    # the shopper reads "printed on the FRONT" — prompt formatting leaking into
    # buyer-facing copy. The instruction has to stay; the shouting does not.
    description = re.sub(r"\bFRONT\b", "front", description)
    warn_if_scrubbed("description", description)
    description = scrub_text(description)
    return (description or fallback_description), bool(description)


def classify_design(image_bytes: bytes) -> tuple[bool | None, str]:
    """Return (is_developer_design, reason) by LOOKING at the artwork.

    The title-based test in audit.py reads the design name — the first title
    segment — which the search-phrase-first title format no longer carries the
    joke in. A developer joke behind a "Funny AI Sweatshirt" title is judged
    everyday-AI, generate_description() is told so, and every correct draft the
    model then writes is thrown out by the QA gate (four generations burned on
    4546881437 before it was pinned by hand). This is the artwork test that the
    pin was standing in for.

    One small call: the photo and a yes/no question. It does not generate
    listing copy and does not touch tag usage. Returns (None, why) when no key
    answered, so the caller can fall back to the title test rather than guess.
    """
    keys = _api_keys()
    if not keys:
        return None, "no Gemini keys"
    try:
        from google.genai import types
        from pydantic import BaseModel
    except ImportError as e:
        return None, f"google-genai not installed ({e})"

    class Verdict(BaseModel):
        text_on_design: str
        is_developer_design: bool
        reason: str

    try:
        img = _photo_to_jpeg_bytes(image_bytes)
    except Exception as e:
        return None, f"could not read photo ({type(e).__name__})"

    prompt = """The image is the product photo of a printed garment. Read the artwork and
transcribe the text on it, then decide who the joke is FOR.

DEVELOPER design: the joke is about WRITING software — programming, code,
bugs/debugging, git, terminals, compilers, syntax, stack traces, localhost,
software engineering, devops, sysadmin, data science or machine learning as a
profession. A non-programmer would not fully get it, or it uses programmer
vocabulary.

EVERYDAY-AI design: the joke is about LIVING WITH or USING AI as a normal
person — chatbots, prompts, an AI chatbot as a study buddy or coworker, AI doing
your homework/emails/thinking, introverts, office life, students, burnout.
Anyone who has used a chatbot gets it.

Judge the ARTWORK only. "AI" on its own is not developer. Answer developer
only when the joke needs programmer knowledge or vocabulary. Keep the reason
to one sentence.
"""
    contents = [types.Part.from_bytes(data=img, mime_type="image/jpeg"), prompt]
    # Same budget as generate_description(): the model's thinking counts
    # against max_output_tokens, and at 512 the JSON came back cut off at
    # '{"text' — which the key pool then read as five dead keys.
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=Verdict,
        max_output_tokens=4096,
    )
    result = _walk_key_pool(keys, contents, config)
    if result is None:
        return None, "every Gemini key failed"
    reason = f"{(result.text_on_design or '').strip()[:60]!r} — {(result.reason or '').strip()}"
    return bool(result.is_developer_design), reason


def _pin_user_prompt(garment: str) -> str:
    """PIN_USER_PROMPT is written for the sweatshirt; swap the garment words for
    a tee so the pin never calls a t-shirt a crewneck."""
    if garment != "tee":
        return PIN_USER_PROMPT
    return (PIN_USER_PROMPT
            .replace("unisex crewneck sweatshirt", "unisex t-shirt")
            .replace("office humor crewneck", "office humor tee")
            .replace("sweatshirt", "t-shirt"))


def generate_pin_content(
    design_path: Path,
    fallback_title: str,
    fallback_description: str,
    garment: str = "sweatshirt",
) -> tuple[str, str, str, str, bool]:
    """Return (title, description, alt_text, board_key, ai_used).

    Looks at the design image with Gemini 2.5 Flash and writes Pinterest-shaped
    copy — shorter than Etsy's, no tag field, keywords carried in the prose — and
    classifies the design onto one of BOARD_KEYS.

    `garment` is "sweatshirt" (default) or "tee" — only the garment wording in
    the prompt changes.

    Shares the sticky key pool with generate_listing_content(), so a run whose
    Etsy copy already burned keys 1–2 starts this call on key 3.

    Never raises: falls back to the provided placeholders (ai_used=False) with
    board_key defaulting to ai_humor, the broader of the two boards.
    """
    fallback = (fallback_title[:PIN_TITLE_MAX], fallback_description[:PIN_DESC_MAX],
                "", BOARD_AI, False)

    keys = _api_keys()
    if not keys:
        return fallback

    try:
        from google.genai import types
        from pydantic import BaseModel
    except ImportError as e:
        print(f"     ⚠️  google-genai not installed ({e}); using placeholder pin copy. "
              f"Run: pip install -r requirements.txt")
        return fallback

    class Pin(BaseModel):
        title: str
        description: str
        alt_text: str
        board: str

    try:
        img_bytes = _design_to_jpeg_bytes(design_path)
    except Exception as e:
        print(f"     ⚠️  Could not read design image ({type(e).__name__}: {e}); "
              f"using placeholder pin copy.")
        return fallback

    contents = [
        types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
        _pin_user_prompt(garment),
    ]
    config = types.GenerateContentConfig(
        system_instruction=PIN_SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=Pin,
        max_output_tokens=2048,
    )

    pin = _walk_key_pool(keys, contents, config)
    if pin is None:
        return fallback

    for where, text in (("pin title", pin.title), ("pin description", pin.description),
                        ("pin alt text", pin.alt_text)):
        warn_if_scrubbed(where, text or "")
    title = _strip_markdown(_decap_title(scrub_text(" ".join((pin.title or "").split()))))
    title = title[:PIN_TITLE_MAX] or fallback_title
    description = scrub_text(_strip_markdown((pin.description or "").strip()))[:PIN_DESC_MAX] or fallback_description
    alt_text = scrub_text(_strip_markdown(" ".join((pin.alt_text or "").split())))[:PIN_ALT_MAX]

    # Never trust the model to stay inside the enum — a hallucinated board name
    # would otherwise create a junk board on Pinterest.
    board = str(pin.board or "").strip().lower().replace("-", "_").replace(" ", "_")
    if board not in BOARD_KEYS:
        print(f"     ⚠️  Gemini returned unknown board '{pin.board}' — "
              f"defaulting to {BOARD_AI}.")
        board = BOARD_AI

    return title, description, alt_text, board, True
