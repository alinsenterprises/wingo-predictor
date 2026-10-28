"""Command line entry point.

    python -m wingo_analyzer run            # bot + poller + status server
    python -m wingo_analyzer check          # validate the signed API + config
    python -m wingo_analyzer backfill       # seed every game (>500 rounds)
    python -m wingo_analyzer stats          # window analysis of stored history
    python -m wingo_analyzer predict        # next-round pick
    python -m wingo_analyzer last -n 10     # latest stored rounds
    python -m wingo_analyzer admin-code     # print/rotate the admin unlock code
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from . import __version__, analyzer, service
from .api import DmFirstClient
from .config import Config
from .engine import Engine
from .logging_setup import setup as setup_logging
from .modes import MODES, ORDER, parse_mode_arg
from .orchestrator import Orchestrator
from .storage import Storage


def _boot(log_level: str | None = None):
    cfg = Config.load()
    setup_logging(log_level or cfg.log_level, cfg.log_file)
    return cfg


def _mode(args, fallback: str = "1min"):
    raw = getattr(args, "mode", None)
    mode = parse_mode_arg(raw) if raw else None
    return mode or MODES[fallback]


def cmd_check(args) -> int:
    cfg = _boot()
    print(f"wingo-analyzer {__version__} · check")
    print("  config:", cfg.describe())
    if cfg.gemini_key or cfg.groq_key:
        orch = Orchestrator(cfg)
        print(f"  ai chain: primary={orch.primary} configured={','.join(orch.configured)}")
    else:
        print("  ai chain: no keys configured (Gemini primary / Groq fallback)")

    client = DmFirstClient(cfg)
    client.login()
    failures = 0
    modes = [parse_mode_arg(args.mode)] if args.mode else [MODES[k] for k in ORDER]
    modes = [m for m in modes if m]
    for mode in modes:
        try:
            issue = client.game_issue(mode.type_id)
            rows = client.history(mode.type_id, 1, 5)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {mode.key:6} {exc}")
            failures += 1
            continue
        sample = rows[0] if rows else {}
        print(f"  ✓ {mode.key:6} issue={issue.get('issueNumber')} "
              f"window={issue.get('startTime')}→{issue.get('endTime')} "
              f"rows={len(rows)} latest={sample.get('issueNumber')}→{sample.get('number')}")
    return 1 if failures else 0


def cmd_backfill(args) -> int:
    cfg = _boot()
    if args.target:
        cfg.backfill_target = min(args.target, cfg.max_records)
    storage = Storage(cfg.db_path)
    client = DmFirstClient(cfg)
    engine = Engine(cfg, storage, client, Orchestrator(cfg))
    modes = [parse_mode_arg(args.mode)] if args.mode else [MODES[k] for k in ORDER]
    for mode in [m for m in modes if m]:
        count = engine.backfill(mode.key)
        print(f"  ✓ {mode.key}: {count} rounds stored")
    storage.close()
    return 0


def cmd_stats(args) -> int:
    cfg = _boot()
    storage = Storage(cfg.db_path)
    mode = _mode(args)
    rounds = storage.rounds(mode.key, cfg.analysis_window)
    window = analyzer.window_stats(rounds, cfg.analysis_window)
    score = storage.scorecard(mode.key, 30)
    print(f"{mode.node_label()} · WINGO {mode.label}")
    print(f"  rounds stored : {storage.round_count(mode.key)}")
    print(f"  window        : {window['window']}")
    print(f"  hot digits    : {window['hot']}")
    print(f"  cold digits   : {window['cold']}")
    print(f"  due digits    : {window['due']}")
    print(f"  colours       : {window['colours']}")
    print(f"  sizes         : {window['sizes']}")
    print(f"  colour run    : {window['colour_run']}x {window['colour_run_value']}")
    print(f"  scorecard     : colour {score['colour_rate']}% · size {score['size_rate']}% "
          f"· exact {score['exact_rate']}% over {score['total']} calls")
    print(f"  streak        : {score['streak_kind']}{score['streak_len']}")
    storage.close()
    return 0


def cmd_predict(args) -> int:
    cfg = _boot()
    storage = Storage(cfg.db_path)
    mode = _mode(args)
    rounds = storage.rounds(mode.key, cfg.analysis_window)
    live = storage.latest_prediction(mode.key)
    print(f"{mode.node_label()} · WINGO {mode.label}")
    if live and not live.evaluated_at:
        print(f"  live period   : {live.period_label}")
        print(f"  pick          : {live.number} ({'/'.join(live.colours)}, {live.size}, "
              f"{live.parity}) conviction {live.conviction}%")
    else:
        pred = analyzer.predict(mode, rounds, cfg.analysis_window, "next")
        if pred is None:
            print("  pick          : no chart data yet - run backfill first")
        else:
            print(f"  pick          : {pred.number} ({'/'.join(pred.colours)}, {pred.size}, "
                  f"{pred.parity}) conviction {pred.conviction}%")
    storage.close()
    return 0


def cmd_last(args) -> int:
    cfg = _boot()
    storage = Storage(cfg.db_path)
    mode = _mode(args)
    for rnd in storage.rounds(mode.key, args.count):
        print(f"  {rnd.period}  {rnd.number}  {rnd.colour}")
    storage.close()
    return 0


def cmd_admin_code(args) -> int:
    cfg = _boot()
    storage = Storage(cfg.db_path)
    if args.rotate or not storage.get_meta("admin_code"):
        storage.set_meta("admin_code", uuid.uuid4().hex[:10])
    print(f"admin code: /admin {storage.get_meta('admin_code')}")
    storage.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wingo_analyzer", description=__doc__)
    parser.add_argument("--log-level", default=None, help="debug|info|warning|error")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="bot + poller + status server (default)")

    check = sub.add_parser("check", help="validate config and the signed API")
    check.add_argument("--mode", default=None)

    backfill = sub.add_parser("backfill", help="seed stored history")
    backfill.add_argument("--mode", default=None)
    backfill.add_argument("--target", type=int, default=None)

    stats = sub.add_parser("stats", help="window analysis")
    stats.add_argument("--mode", default=None)

    predict = sub.add_parser("predict", help="next-round pick")
    predict.add_argument("--mode", default=None)

    last = sub.add_parser("last", help="latest stored rounds")
    last.add_argument("-n", "--count", type=int, default=10)
    last.add_argument("--mode", default=None)

    admin = sub.add_parser("admin-code", help="print or rotate the admin unlock code")
    admin.add_argument("--rotate", action="store_true")

    args = parser.parse_args(argv)
    command = args.command or "run"

    if command == "run":
        return asyncio.run(service.run())
    if command == "check":
        return cmd_check(args)
    if command == "backfill":
        return cmd_backfill(args)
    if command == "stats":
        return cmd_stats(args)
    if command == "predict":
        return cmd_predict(args)
    if command == "last":
        return cmd_last(args)
    if command == "admin-code":
        return cmd_admin_code(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
