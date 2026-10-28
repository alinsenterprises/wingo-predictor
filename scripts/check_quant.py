"""Verify the quant formulas against hand-computed textbook values.

Run:  .venv/bin/python scripts/check_quant.py
Every assertion is a value a human can reproduce with a calculator.
"""
from __future__ import annotations

import math
import sys

sys.path.insert(0, ".")

from wingo_analyzer import quant  # noqa: E402
from wingo_analyzer.storage import Round  # noqa: E402


def _round(number: int) -> Round:
    """Minimal Round: the quant module only reads ``number``."""
    return Round(mode="1min", period="0", number=number, colour="red")


def check(label: str, got, want, tol: float = 5e-3) -> None:
    # NOTE: quant returns display-rounded values (2-4 dp), so the default
    # tolerance is 5e-3 - wide enough for a rounded return, tight enough to
    # catch a genuinely wrong formula.
    ok = abs(float(got) - float(want)) <= tol if isinstance(want, (int, float)) \
        else got == want
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}: got={got!r} want={want!r}")
    if not ok:
        raise SystemExit(f"assertion failed: {label}")


def main() -> int:
    # ---- entropy -------------------------------------------------------
    # perfectly uniform: 10 of each digit -> H = ln(10) -> 100 %
    uniform = [_round(d) for d in range(10)] * 6
    ent = quant.entropy_pct(uniform, 60)
    check("entropy uniform pct", ent["pct"], 100.0, tol=5e-3)

    # one digit only -> H = 0
    flat = [_round(7) for _ in range(60)]
    check("entropy flat pct", quant.entropy_pct(flat, 60)["pct"], 0.0, tol=1e-9)

    # two digits 50/50 -> H = ln(2), normalised = ln2/ln10 = 0.30103*100
    half = [_round(d) for d in ([0, 1] * 30)]
    check("entropy 50/50 pct", quant.entropy_pct(half, 60)["pct"],
          100.0 * math.log(2) / math.log(10), tol=5e-3)

    # ---- z-score -------------------------------------------------------
    # 60 rounds, 40 BIG / 20 SMALL -> z = (40 - 30) / (sqrt(60)/2)
    vals = [6] * 40 + [3] * 20
    rounds = [_round(v) for v in vals]
    z = quant.zscore(rounds, 60)
    want_z = (40 - 30) / (math.sqrt(60) / 2)
    check("z-score 40/20", z["z"], want_z, tol=5e-3)
    check("z-score counts", (z["big"], z["small"]), (40, 20))

    # balanced -> z = 0
    bal = [_round(d) for d in ([6, 3] * 30)]
    check("z-score balanced", quant.zscore(bal, 60)["z"], 0.0, tol=1e-9)

    # two-tailed p for z=1.96 must be ~0.05
    check("p(z=1.96)", quant._two_tailed_p(1.96), 0.05, tol=0.005)

    # ---- RSI -----------------------------------------------------------
    # IMPORTANT: the chart convention everywhere in this project is NEWEST
    # FIRST, and quant.rsi reverses it internally to get oldest->newest. So the
    # fixtures below are written newest-first: a series that *rises* over time
    # is therefore written descending in this list.
    rising = [_round(min(9, 29 - i)) for i in range(30)]   # oldest->newest: 0..29
    check("RSI all-up", quant.rsi(rising, 30)["rsi"], 100.0, tol=5e-3)

    # strictly falling over time -> every delta negative -> RSI = 0
    falling = [_round(min(9, i)) for i in range(30)]       # oldest->newest: descends
    check("RSI all-down", quant.rsi(falling, 30)["rsi"], 0.0, tol=5e-3)

    # constant series -> avg_gain = avg_loss = 0; must not crash
    const = [_round(5) for _ in range(30)]
    quant.rsi(const, 30)

    # hand-check period-14 seed: 14 rising deltas -> avg_loss = 0 -> RSI 100
    # written newest-first (descending as you move left)
    seq = list(reversed([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9, 9, 9, 9, 9]))
    check("RSI seed hand-check", quant.rsi([_round(v) for v in seq], 15, period=14)["rsi"],
          100.0, tol=5e-3)

    # ---- EMA / MACD ----------------------------------------------------
    # constant series -> EMA = that constant, MACD = 0, hist = 0
    flat5 = [_round(5) for _ in range(80)]
    m = quant.macd(flat5, 80)
    check("MACD constant -> 0", m["macd"], 0.0, tol=1e-9)
    check("MACD hist -> 0", m["hist"], 0.0, tol=1e-9)

    # hand-computed EMA: values 1..5, period 2 -> seed = (1+2)/2 = 1.5,
    # k = 2/3; e3 = 3*2/3 + 1.5*1/3 = 2.5; e4 = 4*2/3+2.5/3 = 3.5; e5 = 4.5
    ema = quant._ema([1.0, 2.0, 3.0, 4.0, 5.0], 2)
    check("EMA seed", ema[0], 1.5)
    check("EMA step 1", ema[1], 2.5, tol=5e-3)
    check("EMA step 2", ema[2], 3.5, tol=5e-3)
    check("EMA step 3", ema[3], 4.5, tol=5e-3)

    # rising series -> positive MACD trend label
    rising80 = [_round(min(9, i % 10)) for i in range(80)]
    m2 = quant.macd(rising80, 80)
    assert m2["label"] != "INSUFFICIENT", "MACD needs enough samples"
    print(f"[PASS] MACD on mixed series: {m2['label']} macd={m2['macd']}")

    # ---- Lyapunov ------------------------------------------------------
    # constant series -> zero variance -> FLAT guard, no crash
    check("lyapunov flat guard", quant.lyapunov(const, 30)["label"], "FLAT")
    # pure alternating series is deterministic -> lambda <= 0 (reverting/neutral)
    alt = [_round(d) for d in ([1, 8] * 30)]
    ly_alt = quant.lyapunov(alt, 60)
    print(f"[PASS] lyapunov alternating: lambda={ly_alt['lambda']} {ly_alt['label']}")
    # random-looking balanced series
    ly_mix = quant.lyapunov(rising80, 80)
    print(f"[PASS] lyapunov mixed: lambda={ly_mix['lambda']} {ly_mix['label']}")

    # ---- risk matrix ---------------------------------------------------
    deep = {"threat": 10.0}
    low = quant.risk_matrix(deep, 0.60, 70)
    deep2 = {"threat": 80.0}
    high = quant.risk_matrix(deep2, 0.60, 70)
    check("risk units monotone down", high["units"] < low["units"], True)
    check("risk units in range", 10 <= low["units"] <= 100, True)
    check("risk mode low threat", low["mode"], "CAUTIOUS")
    check("risk mode high threat", high["mode"], "SURVIVAL")
    check("threat level clamp", 0 <= high["level"] <= 10, True)

    # ---- confidence bar ------------------------------------------------
    check("bar 0", quant.confidence_bar(0), "⬛" * 12)
    check("bar 100", quant.confidence_bar(100), "🟨" * 12)
    check("bar 50", len(quant.confidence_bar(50)), 12)
    check("bar clamps", len(quant.confidence_bar(999)), 12)

    print("\nALL QUANT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
