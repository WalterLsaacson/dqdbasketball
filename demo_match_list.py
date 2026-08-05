#!/usr/bin/env python3
"""Thin demo wrapper — prefer the skill CLI.

  python3 .cursor/skills/dongqiudi-basketball/scripts/dqd_match.py list --tab hot --json
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent / ".cursor/skills/dongqiudi-basketball/scripts"
sys.path.insert(0, str(_SCRIPTS))

from dqd_match import main  # noqa: E402

if __name__ == "__main__":
    # Default to human-friendly multi-tab text via list --tab all
    argv = sys.argv[1:]
    if not argv:
        argv = ["list", "--tab", "all", "--json"]
    elif argv[0] not in ("list", "watch"):
        argv = ["list", *argv]
    raise SystemExit(main(argv))
