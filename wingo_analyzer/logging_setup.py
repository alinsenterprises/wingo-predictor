"""Console + rotating-file logging (5 MB x 5)."""
from __future__ import annotations

import logging
import logging.handlers
import os

FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup(level: str = "info", log_file: str = "logs/analyzer.log") -> logging.Logger:
    numeric = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(FORMAT, DATEFMT))
    root.addHandler(console)

    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            rotating = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            rotating.setFormatter(logging.Formatter(FORMAT, DATEFMT))
            root.addHandler(rotating)
        except OSError as exc:  # never let logging kill the process
            logging.warning("cannot open log file %s: %s", log_file, exc)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return logging.getLogger("wingo")
