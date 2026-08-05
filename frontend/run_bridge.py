#!/usr/bin/env python3
"""Launch the Match Bridge Board frontend module."""

from __future__ import annotations

import runpy
from pathlib import Path

APP = Path(__file__).resolve().parent / "bridge-board" / "server" / "app.py"


def main() -> int:
    runpy.run_path(str(APP), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
