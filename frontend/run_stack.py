#!/usr/bin/env python3
"""Backward-compatible alias → System Main module."""

from __future__ import annotations

import runpy
from pathlib import Path

APP = Path(__file__).resolve().parent / "run_main.py"


def main() -> int:
    runpy.run_path(str(APP), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
