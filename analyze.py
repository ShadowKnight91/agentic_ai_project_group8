#!/usr/bin/env python3
"""Summarize a results directory: python analyze.py results/grid [results/hardening ...]"""

import sys

from redteam.analysis import summarize

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    for directory in sys.argv[1:]:
        path = summarize(directory)
        print(f"\nWrote {path}")
