import asyncio
import sys
import os

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'wingo_analyzer')
VENV_SITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'wingo-analyzer', '.venv', 'lib', 'python3.10', 'site-packages')
if os.path.isdir(VENV_SITE):
    sys.path.insert(0, VENV_SITE)
sys.path.insert(0, APP_DIR)

from wingo_analyzer.config import Config
from wingo_analyzer.storage import Storage
from wingo_analyzer.orchestrator import Orchestrator
from wingo_analyzer.api import DmFirstClient
from wingo_analyzer.engine import Engine
from wingo_analyzer.snapshots import SnapshotManager
from wingo_analyzer.bot import BotCore


async def main():
    cfg = Config()
    storage = Storage(cfg.db_path)
    orch = Orchestrator(cfg)
    api = DmFirstClient(cfg)
    engine = Engine(cfg, storage, api, orch)
    snap = SnapshotManager(cfg, storage)
    c = BotCore(cfg, storage, engine, orch, snap)

    print('init ok', flush=True)
    print('token=', bool(cfg.telegram_token), flush=True)
    print('headless=', cfg.headless, flush=True)
    print('bot_ready=', cfg.bot_ready, flush=True)
    print('_reopen defined=', '_reopen_live_cards' in dir(c), flush=True)
    print('_reopen callable=', callable(getattr(c, '_reopen_live_cards', None)), flush=True)
    print('tasks pre-start:', [t.get_name() for t in asyncio.all_tasks() if t.get_name()], flush=True)

    await c.start()
    print('after start running=', c.running, 'username=', c.username, flush=True)
    print('reopen-containing tasks post-start:', [t.get_name() for t in asyncio.all_tasks() if 'reopen' in (t.get_name() or '')], flush=True)

    await asyncio.sleep(6)
    print('post-wait reopen tasks:', [t.get_name() for t in asyncio.all_tasks() if 'reopen' in (t.get_name() or '')], flush=True)

    c.stop()
    await asyncio.sleep(1)
    print('done', flush=True)


asyncio.run(main())
