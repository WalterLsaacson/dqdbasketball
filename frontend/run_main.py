#!/usr/bin/env python3
"""Launch System Main — the only process you should start.

Boots hub (:8890) + basketball boards + pm_quote watch, which owns the
in-process match-bridge (memory event queue → quote).
Do not start boards or pm_quote separately; a second run_main fails if :8890 is
already taken. Set MAIN_BRIDGE_INPROC=0 to use bridge-board as skill host.

Examples:
  python3 frontend/run_main.py
  python3 frontend/run_main.py --take-depth walk --max-usdc 5
  python3 frontend/run_main.py --live --max-usdc 2
  python3 frontend/run_main.py --score-mode dry --prop-mode live --ft-mode live --max-usdc 1
  python3 frontend/run_main.py --no-trade
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent / "main" / "server" / "app.py"


def main() -> int:
    # Preserve CLI flags for app.main() argparse (runpy keeps sys.argv).
    try:
        runpy.run_path(str(APP), run_name="__main__")
    except SystemExit as e:
        code = e.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
