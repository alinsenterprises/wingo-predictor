"""Boots the whole desk: engine loops, Telegram bot, status server, retention.

Order matters: the status server comes up first (so the preview is ready even
while the first backfill runs), then the bot, then the poll loops.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
import uuid

from . import __version__
from .api import DmFirstClient
from .bot import BotCore
from .config import Config
from .engine import Engine
from .logging_setup import setup as setup_logging
from .modes import ORDER
from .orchestrator import Orchestrator
from .snapshots import SnapshotManager
from .status import StatusServer
from .storage import Storage

log = logging.getLogger("wingo.service")


def ensure_admin_code(storage: Storage, cfg: Config) -> str:
    if cfg.admin_code:
        storage.set_meta("admin_code", cfg.admin_code)
    elif not storage.get_meta("admin_code"):
        storage.set_meta("admin_code", uuid.uuid4().hex[:10])
    return storage.get_meta("admin_code")


def backfill_all(engine: Engine) -> None:
    for key in ORDER:
        try:
            engine.backfill(key)
        except Exception as exc:  # noqa: BLE001 - backfill must never block boot
            log.warning("backfill %s failed: %s", key, exc)


async def run() -> int:
    cfg = Config.load()
    setup_logging(cfg.log_level, cfg.log_file)
    log.info("WINGO NEXUS AI v%s booting", __version__)

    missing = [name for name, value in (
        ("TELEGRAM_BOT_TOKEN", cfg.telegram_token),
        ("GEMINI_API_KEY", cfg.gemini_key),
        ("GROQ_API_KEY", cfg.groq_key),
    ) if not value]
    if missing:
        log.warning("missing keys: %s (bot keeps running and retries when they appear "
                    "after a restart)", ", ".join(missing))

    storage = Storage(cfg.db_path)
    api = DmFirstClient(cfg)
    orchestrator = Orchestrator(cfg)
    engine = Engine(cfg, storage, api, orchestrator)
    snapshots = SnapshotManager(cfg, storage)
    bot = BotCore(cfg, storage, engine, orchestrator, snapshots) if cfg.bot_ready else None
    if cfg.headless:
        log.info("headless mode: telegram polling off - engine + status only "
                 "(the production bot runs elsewhere)")

    admin_code = ensure_admin_code(storage, cfg)
    log.info("admin claim command: /admin %s", admin_code)
    log.info("ai chain: primary=%s configured=%s",
             orchestrator.primary, ",".join(orchestrator.configured) or "none")

    boot_at = time.time()

    def payload() -> dict:
        return {
            "ok": True,
            "service": "wingo-nexus-ai",
            "version": __version__,
            "uptime_s": int(time.time() - boot_at),
            "bot": {
                "configured": bool(cfg.telegram_token),
                "started": bool(bot and bot.running),
                "username": getattr(bot, "username", "") or None,
            },
            "ai": orchestrator.health(),
            "games": engine.status(),
            "users": storage.user_count(),
            "snapshots": storage.snapshot_stats(),
            "rounds_total": storage.total_rounds(),
            "predictions": storage.prediction_count(),
            "config": cfg.describe(),
        }

    status = StatusServer(payload, cfg.status_host, cfg.status_port)
    status.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError):  # pragma: no cover
            signal.signal(sig, lambda *_: stop.set())

    async def engine_main() -> None:
        try:
            await asyncio.to_thread(api.login)
        except Exception as exc:  # noqa: BLE001
            log.warning("login skipped: %s", exc)
        await asyncio.to_thread(backfill_all, engine)
        await engine.run()

    tasks = [asyncio.create_task(engine_main(), name="engine")]
    if bot is not None:
        try:
            # start() also launches the card auto-updater and the snapshot sweeper
            await bot.start()
        except BaseException:  # noqa: BLE001 - log the full traceback, keep the desk up
            log.exception("telegram bot failed to start (%r)", bot)
            bot = None

    log.info("desk ready on %s:%s (nodes: %s)",
             cfg.status_host, cfg.status_port, ", ".join(ORDER))

    await stop.wait()
    log.info("shutting down…")

    engine.running = False
    if bot is not None:
        await bot.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    status.stop()
    storage.close()
    log.info("bye")
    return 0
