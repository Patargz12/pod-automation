"""
marketing_tshirt.py — swap Printify's mockups for ours on the T-SHIRT Etsy drafts
================================================================================
The t-shirt twin of marketing_sweatshirt.py. Run it AFTER you fixed positioning
in Printify and clicked "Publish" there. For every t-shirt draft on Etsy it:

  1. Finds the Etsy listing via caching/published.json → Printify → external.id
  2. Switches renewal from Manual to Automatic
  3. Replaces Printify's images with output/tshirt/<design>/ renders,
     Black_Shirt.png first (the main thumbnail)
  4. LEAVES THE LISTING IN DRAFTS — you publish on Etsy yourself
  5. Records it in caching/marketed_tshirt.json so re-runs skip it

USAGE:
  python -m scripts.create.marketing_tshirt                  # Swap mockups on every new t-shirt draft
  python -m scripts.create.marketing_tshirt --dry-run        # Preview, change nothing
  python -m scripts.create.marketing_tshirt --forget "name.png" [more...]  # Redo design(s) next run
  python -m scripts.create.marketing_tshirt --clear-ledger   # Wipe marketed_tshirt.json
  python -m scripts.create.marketing_tshirt --help
"""

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from scripts.create import marketing_sweatshirt
from services.printify_service import CACHE_DIR, TEE_PROFILE, _load_json, _save_json

OUTPUT_DIR      = marketing_sweatshirt.Path("output/tshirt")
MARKETED_LEDGER = CACHE_DIR / "marketed_tshirt.json"
THUMBNAIL_NAME  = "Black_Shirt.png"


def clear_marketed() -> int:
    n = len(_load_json(MARKETED_LEDGER, {"products": []}).get("products", []))
    _save_json(MARKETED_LEDGER, {"products": []})
    print(f"🧹  Cleared {n} entr{'y' if n == 1 else 'ies'} from {MARKETED_LEDGER.name}. "
          f"Every t-shirt draft will be re-checked on the next run.")
    return n


def run(dry_run: bool = False, only: set[str] | None = None):
    marketing_sweatshirt.run(
        dry_run=dry_run,
        only=only,
        profile=TEE_PROFILE,
        output_dir=OUTPUT_DIR,
        ledger_path=MARKETED_LEDGER,
        thumbnail_name=THUMBNAIL_NAME,
    )


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    if "--clear-ledger" in args:
        clear_marketed()
        sys.exit(0)

    if "--forget" in args:
        names = [a for a in args[args.index("--forget") + 1:] if not a.startswith("--")]
        if not names:
            print('⚠️  --forget needs a design file name, e.g. --forget "ctrl_alt.png"')
        else:
            marketing_sweatshirt.forget_marketed(names, ledger_path=MARKETED_LEDGER)
        sys.exit(0)

    run(dry_run="--dry-run" in args)
