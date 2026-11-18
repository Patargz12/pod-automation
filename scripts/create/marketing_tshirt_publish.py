"""
marketing_tshirt_publish.py — marketing_tshirt.py + auto-publish on Etsy.

The t-shirt twin of marketing_sweatshirt_publish.py. Same as
`python -m scripts.create.marketing_tshirt` (swap Printify's mockups for ours, renewal →
Automatic), then it ALSO clicks "Publish" on Etsy for you:

  1. Runs the normal mockup swap (marketing_tshirt.run).
  2. Every design in caching/marketed_tshirt.json whose Etsy listing is
     still a DRAFT gets its tags copied from Printify (Printify's own Etsy tag
     sync is unreliable), then is activated (taken live).  💸 $0.20 Etsy fee each.
     You get a 10s Ctrl+C window before anything is charged.
  3. Designs whose AI copy fell back to placeholder text stay in drafts
     (pass --allow-placeholder to publish them anyway).

Pinterest is NOT touched here.

USAGE:
  python -m scripts.create.marketing_tshirt_publish                      # Swap mockups + publish
  python -m scripts.create.marketing_tshirt_publish --dry-run            # Preview, change nothing
  python -m scripts.create.marketing_tshirt_publish --allow-placeholder  # Publish even placeholder copy
  python -m scripts.create.marketing_tshirt_publish --help
"""

import sys

from scripts.create import marketing_tshirt
from scripts.create.marketing_sweatshirt_publish import publish_marketed
from services.printify_service import TEE_PROFILE


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    dry = "--dry-run" in args
    marketing_tshirt.run(dry_run=dry)
    publish_marketed(dry_run=dry, allow_placeholder="--allow-placeholder" in args,
                     profile=TEE_PROFILE, ledger_path=marketing_tshirt.MARKETED_LEDGER)
