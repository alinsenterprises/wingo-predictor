#!/usr/bin/env python3
"""Convenience launcher: ``python run.py [command]``.

Equivalent to ``python -m wingo_analyzer``; kept so deployment scripts and
readers have an obvious place to start.
"""

from wingo_analyzer.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
