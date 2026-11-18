"""
brand_guard.py — Keep AI company / product names out of listing copy
=====================================================================
Nullish sells AI humor, so the model's natural shorthand for "an AI" is a brand
name: "Funny ChatGPT Sweatshirt", "thank you chatgpt", "Claude wrote my essay".
Those are registered trademarks of OpenAI, Anthropic, Google and others. Using
them in a title, tag or description to sell merch is exactly what Etsy's
intellectual-property reports act on — one strike can pull a listing, enough of
them can close the shop.

The prompts in ai_content.py now forbid brand names, but a prompt is a request,
not a guarantee. This module is the guarantee: every write path (Gemini output,
EtsyClient title/tag/description updates, Printify product bodies, Pinterest
pins) runs its copy through scrub_* before it leaves the machine, so a brand
name cannot reach a live listing whichever script produced it.

Every brand is replaced with the generic "AI", which keeps the sentence and the
search intent ("funny chatgpt shirt" -> "funny ai shirt") without the mark.

No third-party imports — etsy_service, printify_service and pinterest_service
all import this, and it must never be the reason one of them fails to load.
"""

from __future__ import annotations

import re

# Brand / product names that must never appear in listing copy. Matched as whole
# words, case-insensitively. Add to this list rather than special-casing a
# script — every write path reads it.
#
# Deliberately NOT here: ordinary words that happen to be product names in some
# context ("bard", "llama", "perplexity", "mistral") — stripping those would
# mangle real copy. Gemini and Copilot ARE here: on an AI-humor shop they only
# ever mean the Google / Microsoft products.
BRAND_TERMS = (
    r"chat\s*-?\s*gpt",
    r"gpt[-\s]?\d+(?:\.\d+)?[a-z]?",   # GPT-4, GPT 4o, GPT-5.1
    r"gpt",
    r"open\s*ai",
    r"claude",
    r"anthropic",
    r"gemini",
    r"google\s+bard",
    r"copilot",
    r"grok",
    r"midjourney",
    r"deepseek",
)

# Shown to the model in the prompts, so it knows what it may not write.
BRAND_NAMES_FOR_PROMPT = (
    "ChatGPT, GPT, OpenAI, Claude, Anthropic, Gemini, Bard, Copilot, Grok, "
    "Midjourney, DeepSeek"
)

_BRAND_RE = re.compile(r"\b(?:" + "|".join(BRAND_TERMS) + r")\b", re.IGNORECASE)
# "ChatGPT AI Shirt" -> "AI AI Shirt" -> "AI Shirt"
_DOUBLE_AI_RE = re.compile(r"\bAI(?:[\s-]+AI)+\b", re.IGNORECASE)


def find_brands(text: str) -> list[str]:
    """Every brand name in `text`, as written, in order. [] when clean."""
    return [m.group(0) for m in _BRAND_RE.finditer(text or "")]


def scrub_text(text: str) -> str:
    """Replace every brand name with "AI" and collapse any "AI AI" it leaves."""
    if not text or not _BRAND_RE.search(text):
        return text
    out = _BRAND_RE.sub("AI", text)
    out = _DOUBLE_AI_RE.sub("AI", out)
    # "A AI" reads wrong once the brand is gone ("a ChatGPT user" -> "an AI user").
    out = re.sub(r"\b([Aa])\s+AI\b", r"\1n AI", out)
    return out


def scrub_tags(tags: list[str]) -> list[str]:
    """Scrub each tag, lowercase it, and drop any that become a duplicate or a
    bare "ai" ("chatgpt" -> "ai" is a wasted slot, not a tag)."""
    out, seen = [], set()
    for t in tags or []:
        t = " ".join(scrub_text(str(t)).lower().split())
        if not t or t == "ai" or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def warn_if_scrubbed(where: str, before: str | list[str]) -> None:
    """Print which brand names were removed, so a run log shows it happened."""
    text = " ".join(before) if isinstance(before, list) else (before or "")
    hits = find_brands(text)
    if hits:
        names = ", ".join(sorted({h for h in hits}, key=str.lower))
        print(f"     🛡️  Removed brand name(s) from {where}: {names} → \"AI\"")
