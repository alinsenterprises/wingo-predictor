"""Command line entry point.

Kept as a thin shim so ``python run.py <command>``, the Docker image and the
deploy scripts all keep working: they import ``wingo_analyzer.cli:main``.

    python run.py run            # bot + poller + status server (default)
    python run.py check          # validate the signed API + config
    python run.py backfill       # seed every game (500+ rounds)
    python run.py stats          # window analysis of stored history
    python run.py predict        # next-round pick
    python run.py last -n 10     # latest stored rounds
    python run.py admin-code     # print/rotate the admin unlock code
"""
from .__main__ import main  # noqa: F401

__all__ = ["main"]
