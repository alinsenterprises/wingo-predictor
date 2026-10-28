#!/usr/bin/env python3
"""Read WINDOWS env keys for the telegram bot - names only, never values."""
from __future__ import annotations
import os
import textwrap

KEYS = [
    "WINGO_HEADLESS",
    "WINGO_BIND",
    "SERF_WINGO_BIND",
]

for k in KEYS:
    print(f"{k} = {os.environ.get(k)!r}")

print("TELEGRAM_BOT_TOKEN present:", bool(os.environ.get("TELEGRAM_BOT_TOKEN")))
print("OS name:", os.name)
print("PATH has .venv python:", any("venv" in p or ".venv" in p for p in os.environ.get("PATH","").split(os.pathsep)))
