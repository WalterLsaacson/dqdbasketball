#!/usr/bin/env python3
"""Backward-compatible alias for the renamed basketball CLI."""

from pm_basket import data_dir, main, write_json

__all__ = ("data_dir", "main", "write_json")

if __name__ == "__main__":
    raise SystemExit(main())
