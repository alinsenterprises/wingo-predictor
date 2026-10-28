"""Render smoke test: build a synthetic ModeState and print the combat card.

Run: python3 scripts/check_card.py
Fails (exit 1) if render.card raises or the box frame is malformed.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from wingo_analyzer import quant  # noqa: E402
from wingo_analyzer.engine import ModeState  # noqa: E402
from wingo_analyzer.modes import MODES, colours_for, size_for, parity_for  # noqa: E402
from wingo_analyzer.render import card  # noqa: E402
from wingo_analyzer.storage import Prediction, Round, User  # noqa: E402


def make_round(n: int, period: str) -> Round:
    return Round(mode="1min", period=period, number=n,
                 colour=",".join(colours_for(n)), recorded_at=0)


def main() -> int:
    rounds = [make_round((i * 7 + 3) % 10, f"P{1000 - i}") for i in range(60)]
    deep = quant.deep_analytics(rounds, 40)
    mode = MODES["1min"]
    size_info = {"p_big": 0.56, "confidence": 60, "balance": 0.56,
                 "mass": 0.6, "momentum": 0.55, "run": 2, "run_side": "BIG",
                 "decoder": {"label": "STREAKY", "context": "BSB",
                             "p": 0.61, "hits": 17, "total": 28,
                             "lag": 1, "corr": 0.12, "runs": 2.1,
                             "run": 2, "run_side": "BIG"}}
    factors = {
        "window": 40, "size": size_info,
        "pattern": {"label": "ALTERNATING"},
        "deep": deep,
        "risk": quant.risk_matrix(deep, 0.56, 60),
        "bar": quant.confidence_bar(56),
    }
    pred = Prediction(
        id=1, mode="1min", period_label="20260423100011421", number=7,
        colours=colours_for(7), size=size_for(7), parity=parity_for(7),
        conviction=56, factors=factors,
        ai_comment="CALL: BIG - momentum holding", ai_provider="openrouter",
        ai_model="nvidia/nemotron-3-ultra-550b-a55b:free", created_at=0,
    )
    state = ModeState(
        mode=mode, issue="20260423100011420",
        start="2026-04-23 10:00:11", end="2026-04-23 10:01:11",
        server_now="2026-04-23 10:00:20", interval_m=1, remaining=51.0,
        synced_at=0.0, synced_wall=0.0, last_round=rounds[0],
        prediction=pred, scorecard={"total": 10, "size_rate": 60},
        rounds_available=600, ready=True, latency_ms=5,
        last_target="BIG", last_target_colour="RED", streak=2,
        streak_side="BIG",
        ledger=[{"period": "1420", "call": "BIG", "actual": "BIG",
                 "hit": True, "pnl": 45.6},
                {"period": "1419", "call": "SMALL", "actual": "BIG",
                 "hit": False, "pnl": -100.0}],
        session={"total": 789, "wins": 400, "losses": 389,
                 "rate": 50.7, "pnl": -1616.10},
        uptime_s=57251.0,
    )
    # freeze the countdown so the render is deterministic
    state.remaining = 51.0
    state.synced_at = __import__("time").monotonic()

    text = card(User(telegram_id=1, name="Test", stage="active"), state)
    print(text)
    print()

    problems = []
    if not text.startswith("┏"):
        problems.append("card does not start with the top frame")
    if "┗" not in text:
        problems.append("card missing the bottom frame")
    for needle in ("UPLINK NODE", "MATRIX SNAPSHOT", "QUANTUM AI PREDICTION",
                   "DEEP QUANTUM ANALYTICS", "AEGIS RISK", "DEEP COMBAT LEDGER",
                   "SESSION ANALYTICS"):
        if needle not in text:
            problems.append(f"missing section: {needle}")
    # math sanity: win rate and net P/L must match the record
    if "50.7%" not in text:
        problems.append("win rate not rendered")
    if "-1616.10" not in text:
        problems.append("net P/L not rendered")
    # colour emoji must resolve the API's uppercase colours
    if "🔴" not in text:
        problems.append("LAST COLOR emoji did not resolve for RED")
    if "B → S → B" not in text:
        problems.append("pattern context not rendered as sides")
    # ledger math: +45.6 and -100 must show with correct signs
    if "(+45.6U)" not in text:
        problems.append("ledger win row not rendered")
    if "(-100.0U)" not in text:
        problems.append("ledger loss row not rendered")

    if problems:
        print("FAIL:")
        for p in problems:
            print(" -", p)
        return 1
    print("CARD OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
