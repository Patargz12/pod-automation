"""
pinterest_marketing.py — Pinterest autopilot entry point
=========================================================
    python -m scripts.pinterest_marketing prepare              # build/extend the queue (your PC)
    python -m scripts.pinterest_marketing prepare --dry-run    # render + write copy, queue nothing
    python -m scripts.pinterest_marketing prepare --only nap.png
    python -m scripts.pinterest_marketing prepare --repick     # re-draw the print area on every mockup

    python -m scripts.pinterest_marketing post                 # post the next pin (GitHub Actions runs this)
    python -m scripts.pinterest_marketing post --dry-run       # show what would be posted, no API call
    python -m scripts.pinterest_marketing post --count 3

    python -m scripts.pinterest_marketing status               # pending / posted / failed, days of runway

Code lives in Pinterest_Market/. Setup checklist: Pinterest_Market/README.md
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


def _opt(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    dry = "--dry-run" in args

    if cmd == "prepare":
        from Pinterest_Market.prepare import run
        return run(dry_run=dry, only=_opt(args, "--only"), repick="--repick" in args)
    if cmd == "post":
        from Pinterest_Market.post import run
        from Pinterest_Market.config import POSTS_PER_RUN
        return run(count=int(_opt(args, "--count") or POSTS_PER_RUN), dry_run=dry)
    if cmd == "status":
        from Pinterest_Market.post import status
        return status()

    print(f"Unknown command '{cmd}'.\n{__doc__}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
