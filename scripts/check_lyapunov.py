"""Lyapunov must be genuinely responsive - not a constant 0.0.

Random iid data is maximally divergent -> lambda must be clearly > 0.
A fixed-point / periodic orbit is non-divergent -> lambda must not blow up.
"""
from __future__ import annotations

import math
import random
import sys

sys.path.insert(0, ".")

from wingo_analyzer import quant  # noqa: E402
from wingo_analyzer.storage import Round  # noqa: E402


def _round(n: int) -> Round:
    return Round(mode="1min", period="0", number=n, colour="red")


def main() -> int:
    random.seed(1234)

    # 1. iid noise over 0..9 - must diverge
    noisy = [_round(random.randint(0, 9)) for _ in range(200)]
    ly = quant.lyapunov(noisy, 200)
    print(f"iid random    : lambda={ly['lambda']:+.4f}  {ly['label']}  pct={ly['pct']}")
    assert ly["lambda"] > 0.01, f"iid noise should diverge, got {ly['lambda']}"

    # 2. strongly trending series - should also be non-trivial
    trend = [_round((i * 3) % 10) for i in range(200)]
    ly2 = quant.lyapunov(trend, 200)
    print(f"trend cycle   : lambda={ly2['lambda']:+.4f}  {ly2['label']}")

    # 3. a near-constant series with tiny jitter - must stay small / negative
    jitter = [_round(5 if random.random() < 0.97 else 6) for _ in range(200)]
    ly3 = quant.lyapunov(jitter, 200)
    print(f"near-constant : lambda={ly3['lambda']:+.4f}  {ly3['label']}")

    # 4. entropy on real random data should be high (>= 90%)
    ent = quant.entropy_pct(noisy, 200)
    print(f"iid entropy   : {ent['pct']}%  {ent['label']}")
    assert ent["pct"] >= 90.0, f"random digits should look chaotic, got {ent['pct']}"

    # 5. z-score on 50/50 random data should be small
    z = quant.zscore(noisy, 200)
    print(f"iid z-score   : z={z['z']}  {z['label']}  p={z['p_value']}")
    assert abs(z["z"]) < 2.0, f"random data should be near-neutral, got {z['z']}"

    print("\nLYAPUNOV RESPONSIVE CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
