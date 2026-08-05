#!/usr/bin/env python3
"""Compatibility launcher — forwards to the official frontend module.

Prefer:
  python3 frontend/run.py
"""

from __future__ import annotations

import runpy
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "frontend" / "match-board" / "server" / "app.py"

if __name__ == "__main__":
    runpy.run_path(str(APP), run_name="__main__")
