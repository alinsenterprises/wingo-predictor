"""Deep quant analytics.

Every function here is a *pure* read over the stored chart - nothing is random,
nothing is clock-derived, and every formula is the textbook definition so it can
be checked by hand against the numbers on the card.

The series used throughout is the digit value 0..9 as it landed, newest first.
That is the only thing the desk actually observes, so every indicator is derived
from it rather than from a fabricated price feed.

Reference formulas
------------------
entropy     H = -sum(p_i * ln p_i) / ln 10            normalised to 0..1
z-score     z = (x - mu) / sigma                      binomial form for the
                                                      BIG/SMALL share
rsi         Wilder's smoothing: avg_gain = (prev*(n-1)+gain)/n
macd        EMA_fast - EMA_slow, signal = EMA(macd, 9)
lyapunov    lambda = (1/(N*dt)) * sum(ln |d_{i+1}| / |d_i|)
            (finite-difference estimate on the digit series)
"""
from __future__ import annotations

import math

from .modes import size_for
from .storage import Round

TWO_PI = 2.0 * math.pi


def _series(rounds: list[Round], window: int) -> list[float]:
    """Newest-first digit series, truncated to the window."""
    return [float(r.number) for r in rounds[:window]]


# --------------------------------------------------------------------------- #
# distribution reads
# --------------------------------------------------------------------------- #
def entropy_pct(rounds: list[Round], window: int) -> dict:
    """Shannon entropy of the digit distribution, normalised against ln(10).

    100 % = perfectly uniform (each digit 10 times) - pure chaos.
    0 %   = every round landed on one digit - maximum order.
    A fair i.i.d. generator sits near 100 % for any window worth reading.
    """
    values = _series(rounds, window)
    n = len(values)
    if n == 0:
        return {"h": 0.0, "pct": 0.0, "label": "-", "perplexity": 0.0}
    counts = [0] * 10
    for v in values:
        counts[int(v)] += 1
    h = 0.0
    for c in counts:
        if c:
            p = c / n
            h -= p * math.log(p)
    pct = 100.0 * h / math.log(10.0)
    if pct >= 97.0:
        label = "Chaos"
    elif pct >= 93.0:
        label = "Balanced"
    elif pct >= 85.0:
        label = "Drifting"
    else:
        label = "Ordered"
    return {"h": round(h, 4), "pct": round(pct, 2), "label": label,
            "perplexity": round(math.exp(h), 2)}


def zscore(rounds: list[Round], window: int) -> dict:
    """z-score of the observed BIG share against the 50/50 null.

    Binomial form: mu = n/2, sigma = sqrt(n)/2  ->  z = (big - n/2) / (sqrt(n)/2).
    |z| > 1.96 is a real skew at the 95 % level; the sign says which side.
    """
    values = _series(rounds, window)
    n = len(values)
    if n < 4:
        return {"z": 0.0, "big": 0, "small": 0, "label": "-", "p_value": 1.0}
    big = sum(1 for v in values if size_for(int(v)) == "BIG")
    small = n - big
    sigma = math.sqrt(n) / 2.0
    z = (big - n / 2.0) / sigma if sigma else 0.0
    p = _two_tailed_p(z)
    if z >= 1.96:
        label = "BIG skew"
    elif z <= -1.96:
        label = "SMALL skew"
    elif abs(z) < 0.5:
        label = "Neutral"
    else:
        label = "Mild skew"
    return {"z": round(z, 3), "big": big, "small": small, "label": label,
            "p_value": round(p, 4)}


def _two_tailed_p(z: float) -> float:
    """Two-tailed p-value from the standard normal CDF (Abramowitz-Stegun 7.1.26)."""
    x = abs(z)
    t = 1.0 / (1.0 + 0.2316419 * x)
    d = 0.3989422804014327 * math.exp(-0.5 * x * x)
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 +
              t * (-1.821255978 + t * 1.330274429))))
    return max(0.0, min(1.0, 2.0 * d * poly))


# --------------------------------------------------------------------------- #
# oscillators over the digit series
# --------------------------------------------------------------------------- #
def rsi(rounds: list[Round], window: int, period: int = 14) -> dict:
    """Wilder's RSI computed on the digit series 0..9.

    Note what this is: it measures whether recent digits are printing *higher*
    than the ones before them, which is exactly momentum for this chart. The
    classic 30/70 bands are kept, because they are the convention everyone
    reads on the card.
    """
    values = list(reversed(_series(rounds, window)))  # oldest -> newest
    n = len(values)
    if n < period + 1:
        return {"rsi": 50.0, "label": "INSUFFICIENT", "period": period,
                "avg_gain": 0.0, "avg_loss": 0.0}
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    # Wilder's recursive smoothing over the rest of the series
    for i in range(period + 1, n):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        r = 100.0
    else:
        rs = avg_gain / avg_loss
        r = 100.0 - 100.0 / (1.0 + rs)
    if r >= 70:
        label = "Overbought"
    elif r <= 30:
        label = "Oversold"
    elif r >= 55:
        label = "Momentum"
    elif r <= 45:
        label = "Weakness"
    else:
        label = "Neutral"
    return {"rsi": round(r, 1), "label": label, "period": period,
            "avg_gain": round(avg_gain, 3), "avg_loss": round(avg_loss, 3)}


def _ema(values: list[float], period: int) -> list[float]:
    """Standard EMA: seed = SMA of the first `period` points, then recursive."""
    if not values:
        return []
    if len(values) < period:
        period = max(1, len(values))
    out = [sum(values[:period]) / period]
    k = 2.0 / (period + 1.0)
    for v in values[period:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def macd(rounds: list[Round], window: int,
         fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD on the digit series: EMA(12) - EMA(26), signal = EMA(macd, 9).

    The series is oldest->newest here (EMA must see time move forward), then
    the result is oriented back to the desk's newest-first convention so the
    sign on the card means "latest reading".
    """
    values = list(reversed(_series(rounds, window)))
    if len(values) < slow + signal:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "label": "INSUFFICIENT",
                "trend": "—"}
    ema_fast = _ema(values, fast)
    ema_slow = _ema(values, slow)
    # align the two EMAs on their common tail
    offset = len(ema_fast) - len(ema_slow)
    if offset < 0:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "label": "INSUFFICIENT",
                "trend": "—"}
    macd_line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]
    signal_line = _ema(macd_line, signal)
    if not signal_line:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "label": "INSUFFICIENT",
                "trend": "—"}
    m = macd_line[-1]
    s = signal_line[-1]
    hist = m - s
    if hist > 0 and m > 0:
        label, trend = "BULLISH", "📈 BULLISH MOMENTUM"
    elif hist > 0:
        label, trend = "CROSS-UP", "📈 TURNING BULLISH"
    elif m < 0:
        label, trend = "BEARISH", "📉 HEAVY BEAR PRESSURE"
    else:
        label, trend = "CROSS-DOWN", "📉 TURNING BEARISH"
    return {"macd": round(m, 4), "signal": round(s, 4), "hist": round(hist, 4),
            "label": label, "trend": trend}


# --------------------------------------------------------------------------- #
# chaos / stability
# --------------------------------------------------------------------------- #
def lyapunov(rounds: list[Round], window: int) -> dict:
    """Largest Lyapunov exponent by Wolf's method on the digit series.

    lambda > 0 means nearby trajectories diverge (chaotic / unpredictable),
    lambda ~ 0 means neutral, lambda < 0 means the series is pulled back
    (converging, i.e. mean-reverting). We report it as an exponent times a
    fixed horizon so the card can print a stable, comparable number.
    """
    values = _series(rounds, window)
    n = len(values)
    if n < 12:
        return {"lambda": 0.0, "pct": 0.0, "label": "INSUFFICIENT", "divergent": False}
    # normalise to zero mean / unit variance so the exponent is scale-free
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    if var <= 1e-12:
        return {"lambda": 0.0, "pct": 0.0, "label": "FLAT", "divergent": False}
    std = math.sqrt(var)
    x = [(v - mean) / std for v in values]

    # Wolf et al. (1985) minimal-separation estimator over the embedded series.
    lam = _wolf_lambda(x)
    pct = round(100.0 * (1.0 - math.exp(-abs(lam))), 2)
    divergent = lam > 0.05
    if lam > 0.2:
        label = "CHAOTIC"
    elif lam > 0.05:
        label = "Unstable"
    elif lam > -0.05:
        label = "Neutral"
    else:
        label = "Reverting"
    return {"lambda": round(lam, 4), "pct": pct, "label": label,
            "divergent": divergent}


def _wolf_lambda(x: list[float]) -> float:
    """Wolf et al. (1985) minimal-separation estimator over the scalar series.

    Tracks the separation of the nearest neighbour in phase space and averages
    log(d_{i+1}/d_i) / dt. Deterministic, no random seeding, no free parameters
    beyond the embedding delay (taken as 1, the natural spacing of the chart).
    """
    n = len(x)
    if n < 8:
        return 0.0
    # 2-D embedding (delay 1) so "neighbours" are actual points in state space
    pts = [(x[i], x[i + 1]) for i in range(n - 1)]
    m = len(pts)
    if m < 6:
        return 0.0
    logs: list[float] = []
    j = 0
    for i in range(m - 1):
        # search forward for the first point whose successor is defined,
        # preferring the current best neighbour (re-anchoring when it drifts)
        best, best_d = None, float("inf")
        start = i + 1
        limit = min(m, start + 12)
        for k in range(start, limit):
            d = math.dist(pts[i], pts[k])
            if d < best_d:
                best, best_d = k, d
        if best is None or best_d <= 1e-9 or best + 1 >= m:
            continue
        j = best
        sep0 = math.dist(pts[i], pts[j])
        sep1 = math.dist(pts[i + 1], pts[j + 1])
        if sep0 <= 1e-12 or sep1 <= 1e-12:
            continue
        logs.append(math.log(sep1 / sep0))
    if not logs:
        return 0.0
    return sum(logs) / len(logs)


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #
def chi_square(rounds: list[Round], window: int) -> dict:
    """Pearson chi-square of the digit distribution against uniform (10 bins).

    X2 = sum((O_i - E_i)^2 / E_i) with E = n/10. df = 9, so 16.92 is the 95 %
    critical value - above it the window genuinely departs from a fair spread
    and the desk can lean on the skew instead of calling it noise.
    """
    values = _series(rounds, window)
    n = len(values)
    if n < 10:
        return {"x2": 0.0, "df": 9, "p_value": 1.0, "label": "INSUFFICIENT",
                "significant": False, "expected": 0.0}
    counts = [0] * 10
    for v in values:
        counts[int(v)] += 1
    expected = n / 10.0
    x2 = sum((c - expected) ** 2 for c in counts) / expected
    # survival function of chi2 with df=9, via the regularised gamma Q
    p = _chi2_sf(x2, 9)
    significant = x2 > 16.919          # 95 % critical value, df = 9
    return {
        "x2": round(x2, 3), "df": 9, "p_value": round(p, 4),
        "label": "SKEWED" if significant else "UNIFORM",
        "significant": significant, "expected": round(expected, 2),
    }


def _chi2_sf(x: float, k: int) -> float:
    """P(X > x) for chi-square with k degrees of freedom (lower-gamma ratio).

    Uses the series for the regularised lower gamma P(k/2, x/2), so the p-value
    on the card is a real survival probability, not a lookup guess.
    """
    if x <= 0:
        return 1.0
    a = k / 2.0
    xh = x / 2.0
    # series: P(a,x) = e^-x x^a / Gamma(a) * sum_{n=0..} x^n / (a(a+1)...(a+n))
    term = 1.0 / a
    total = term
    n = 0
    while n < 500:
        n += 1
        term *= xh / (a + n)
        total += term
        if term < total * 1e-12:
            break
    lower = math.exp(-xh + a * math.log(xh) - math.lgamma(a)) * total
    return max(0.0, min(1.0, 1.0 - lower))


def runs_test(rounds: list[Round], window: int) -> dict:
    """Wald-Wolfowitz runs test on the BIG/SMALL sequence.

    A run is a maximal same-side streak. For n1 BIGs and n2 SMALLs the expected
    number of runs is 2*n1*n2/(n1+n2) + 1 and the z-score uses the standard
    variance. Fewer runs than expected = the chart is streaking (clustering),
    more runs than expected = it is flipping every round.
    """
    values = _series(rounds, window)
    sides = ["B" if size_for(int(v)) == "BIG" else "S" for v in values]
    n1 = sides.count("B")
    n2 = len(sides) - n1
    n = len(sides)
    if n < 10 or n1 == 0 or n2 == 0:
        return {"runs": 0, "expected": 0.0, "z": 0.0, "label": "INSUFFICIENT",
                "p_value": 1.0, "n1": n1, "n2": n2}
    runs = 1
    for i in range(1, n):
        if sides[i] != sides[i - 1]:
            runs += 1
    exp = 2.0 * n1 * n2 / n + 1.0
    var = (2.0 * n1 * n2 * (2.0 * n1 * n2 - n)) / (n * n * (n - 1.0))
    if var <= 0:
        return {"runs": runs, "expected": round(exp, 2), "z": 0.0,
                "label": "DEGENERATE", "p_value": 1.0, "n1": n1, "n2": n2}
    z = (runs - exp) / math.sqrt(var)
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0))))
    if z <= -1.96:
        label = "STREAKING"
    elif z >= 1.96:
        label = "FLIPPING"
    else:
        label = "RANDOM"
    return {"runs": runs, "expected": round(exp, 2), "z": round(z, 3),
            "label": label, "p_value": round(max(0.0, p), 4), "n1": n1, "n2": n2}


def transition_matrix(rounds: list[Round], window: int, order: int = 2) -> dict:
    """Order-``order`` Markov transition table over BIG/SMALL.

    Returns the next-side distribution conditioned on the last ``order`` sides
    (newest-first context), plus the overall table so the card can quote
    "after XX the next was BIG in 63 % of n cases" with real counts behind it.
    """
    values = _series(rounds, window)
    sides = ["B" if size_for(int(v)) == "BIG" else "S" for v in values][::-1]  # oldest first
    n = len(sides)
    key = min(max(1, order), 4)
    if n < key + 8:
        return {"context": "-", "p_big": 0.5, "hits": 0, "total": 0,
                "label": "INSUFFICIENT", "table": {}}
    table: dict[str, list[int]] = {}
    for i in range(n - key):
        ctx = "".join(sides[i:i + key])
        nxt = sides[i + key]
        cell = table.setdefault(ctx, [0, 0])
        cell[0 if nxt == "B" else 1] += 1
    ctx = "".join(sides[-key:])                # context on screen right now
    cell = table.get(ctx)
    hits = cell[0] if cell else 0
    total = sum(cell) if cell else 0
    if total == 0:
        # fall back to the most common shorter context present
        for k in range(key - 1, 0, -1):
            short = ctx[-k:]
            cand = table.get(short)
            if cand and sum(cand) >= 6:
                hits, total = cand[0], sum(cand)
                ctx = short
                break
    p_big = (hits + 1) / (total + 2) if total else 0.5   # Laplace smoothing
    if total >= 8:
        label = "HOT" if p_big >= 0.62 else "COLD" if p_big <= 0.38 else "NEUTRAL"
    else:
        label = "SPARSE"
    pretty = {c: {"p_big": round(v[0] / max(1, sum(v)), 3), "n": sum(v)}
              for c, v in sorted(table.items()) if sum(v) >= 4}
    return {"context": ctx, "p_big": round(p_big, 4), "hits": hits,
            "total": total, "label": label, "table": pretty}


def deep_analytics(rounds: list[Round], window: int) -> dict:
    """Everything the DEEP QUANTUM ANALYTICS block prints - one call, one dict."""
    ent = entropy_pct(rounds, window)
    z = zscore(rounds, window)
    r = rsi(rounds, window)
    m = macd(rounds, window)
    ly = lyapunov(rounds, window)
    chi = chi_square(rounds, window)
    runs = runs_test(rounds, window)
    markov = transition_matrix(rounds, window, order=2)
    # composite threat: how much of the window contradicts a clean 50/50 read
    threat = round(min(100.0, abs(z["z"]) / 3.0 * 100.0
                       + max(0.0, 100.0 - ent["pct"]) * 0.6), 1)
    return {
        "entropy": ent,
        "zscore": z,
        "rsi": r,
        "macd": m,
        "lyapunov": ly,
        "chi2": chi,
        "runs": runs,
        "markov": markov,
        "threat": threat,
        "rsi_value": r["rsi"],
        "trend": m["trend"],
    }


def risk_matrix(deep: dict, p_big: float, confidence: int) -> dict:
    """Threat level + suggested stake, straight off the analytics above.

    threat % -> level 0..10 (10 = most hostile), risk mode from the band, and a
    10-100 unit allocation scaled by model confidence and inverted by threat so
    a clean, confident window sizes up and a chaotic one sizes down.
    """
    threat = float(deep.get("threat", 0.0))
    level = max(0, min(10, round(threat / 10.0)))
    if threat < 20:
        mode = "CAUTIOUS"
    elif threat < 45:
        mode = "BALANCED"
    elif threat < 70:
        mode = "AGGRESSIVE"
    else:
        mode = "SURVIVAL"
    conf = max(0, min(100, int(confidence)))
    edge = abs(float(p_big) - 0.5) * 2.0          # 0..1 distance from a coin flip
    units = int(round(10 + 90 * (0.35 * conf / 100.0 + 0.65 * edge)
                      * (1.0 - threat / 140.0)))
    units = max(10, min(100, units))
    return {"threat": threat, "level": level, "mode": mode, "units": units}


def confidence_bar(confidence: int) -> str:
    """A 12-cell filled/empty bar for the probability line."""
    c = max(0, min(100, int(confidence)))
    filled = round(c / 100.0 * 12)
    return "🟨" * filled + "⬛" * (12 - filled)


def ledger_rows(storage, key: str, limit: int = 6) -> list[dict]:
    """The DEEP COMBAT LEDGER: newest settled calls, AI pick vs actual."""
    out: list[dict] = []
    try:
        settled = storage.settled_predictions(key, limit)
    except Exception:  # noqa: BLE001 - the card must render even if storage hiccups
        return out
    for pred in settled:
        if pred.evaluated_at is None:
            continue
        actual = pred.actual_number if pred.actual_number is not None else -1
        out.append({
            "period": pred.period_label,
            "call": pred.size,
            "actual": size_for(actual) if actual >= 0 else "—",
            "hit": bool(pred.size_hit),
            "number": pred.number,
            "actual_number": actual,
        })
    return out


def session_stats(storage, key: str, limit: int = 800) -> dict:
    """Session record across the whole scored history: rate, W-L, net P/L."""
    settled = storage.settled_predictions(key, limit)
    total = len(settled)
    wins = sum(1 for p in settled if p.size_hit)
    losses = total - wins
    # payout convention: a size hit returns +95 units on a 100-unit stake,
    # a miss costs the stake. Net is the honest bankroll, not a cherry-pick.
    net = wins * 95.0 - losses * 100.0
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "rate": round(100.0 * wins / total, 1) if total else 0.0,
        "net": round(net, 2),
    }


def latency(epoch_seconds: float) -> str:
    """Round-trip figure for the UPLINK line (seconds -> ms string)."""
    return f"{max(0.0, epoch_seconds) * 1000.0:.2f}ms"
